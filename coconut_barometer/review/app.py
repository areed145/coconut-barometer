"""The review tool's logic: one photo at a time from the queue, your decisions, and a
background worker that makes the Flickr (and vision) calls. No web code here; see
server.py for the HTTP side and static/ for the page."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import deque

from .. import activity
from ..apply import apply_changes, outcomes, unfinished
from ..changes import META_ACTIONS, TAG_ACTIONS, Change, split_tags
from ..config import FLICKR_CALLS_PER_SECOND, VISION_CONCURRENCY, WEATHERFLOW_CALLS_PER_SECOND
from ..planner import photo_page

# A photo is in review from being sent there until it's applied or ignored (see store/queue.py):
# "new" (waiting to be processed), "pending" (ready), "applying". Shown as one list.
STATUSES = ("new", "pending", "applying")


JOB_NAMES = {"apply": "apply approved changes", "process": "process photo", "sync": "Flickr sync", "wx": "WeatherFlow sync"}
PHOTO_JOBS = ("apply", "process")      # the other jobs are not about a photo: "sync" and "wx" (WeatherFlow)


class ReviewApp:
    """The review tool's logic. send(), decide() and process() only queue a job; a single
    background worker makes the Flickr (and vision) calls in order, so the rate limit
    holds, and records the outcome. Photos left "applying" when the tool stopped resume,
    and with review.when_queued: process, ones still waiting to be processed are processed.

    cfg/provider are needed only to process photos and sync; provider is a zero-argument
    factory so the vision SDK loads on first use.

    Each thread gets its own connection to the database, so this can serve a threaded web server."""

    def __init__(self, store, client, log=lambda *a: None, background: bool = True,
                 cfg: dict | None = None, provider=None, process_kw: dict | None = None):
        self._store_type, self._store_path = type(store), store.path
        self._local = threading.local()
        self._local.store = store
        self.client, self.log = client, log
        self.background = background
        self.cfg, self._provider_factory, self._provider = cfg, provider, None
        self.process_kw = process_kw or {}
        self.jobs: queue.Queue[tuple[str, str]] = queue.Queue()   # ("apply"|"process", photo_id) or ("sync", mode)
        self.syncing = False
        self.weather = None                             # the web app's WeatherService, for ("wx", "") jobs
        self.processing: set[str] = set()
        self.tag_modes: dict[str, str] = {}             # photo -> "add" while its processing waits
        self.fresh: set[str] = set()                    # asked for again: re-read from Flickr, re-classify
        self.recent: deque[dict] = deque(maxlen=50)   # finished jobs, newest last
        self._seq = 0
        self.running: dict | None = None                # the job the worker is on: {kind, photo_id, started}
        self.vision_usage = {"photos": 0, "input_tokens": 0, "output_tokens": 0}   # since start
        self.licenses = self._licenses()
        if background:
            threading.Thread(target=self._worker, name="review-worker", daemon=True).start()
            for pid in reversed(store.queue_ids(["applying"])):   # oldest first
                self.jobs.put(("apply", pid))
            if self._process_on_arrival():
                for pid in reversed(store.queue_ids(["new"])):
                    try:
                        self.process(pid, if_new=True)
                    except KeyError:      # gone from the library since
                        store.queue_remove(pid)

    @property
    def store(self):
        s = getattr(self._local, "store", None)
        if s is None:
            s = self._local.store = self._store_type(self._store_path)
        return s

    def sync(self, mode: str | None = None) -> dict:
        """Queue a sync (whatever's due, or mode: full | photo_list | incremental). Returns at once."""
        if self.cfg is None:
            raise RuntimeError("syncing isn't available in this session")
        if self.client is None:
            return {"status": "no_flickr"}          # no API keys yet
        if self.syncing or any(kind == "sync" for kind, _ in list(self.jobs.queue)):
            return {"status": "busy"}
        self.jobs.put(("sync", mode or ""))
        return {"status": "queued"}

    def wx_sync(self) -> dict:
        """Queue a WeatherFlow sync (stations, then every Tempest's missing history). Returns at once."""
        if self.weather is None or self.weather.client is None:
            return {"status": "no_token"}
        if (self.running or {}).get("kind") == "wx" or any(kind == "wx" for kind, _ in list(self.jobs.queue)):
            return {"status": "busy"}
        self.jobs.put(("wx", ""))
        return {"status": "queued"}

    def summary(self) -> dict:
        counts = self.store.queue_counts()
        out = {s: counts.get(s, 0) for s in STATUSES}
        out["to_review"] = sum(out.values())
        return out

    def queue(self, sort: str = "newest") -> list[str]:
        """Every photo in review."""
        return self.store.queue_ids(STATUSES, sort)

    def _process_on_arrival(self) -> bool:
        """review.when_queued: process (and a way to): photos sent to review are processed at once."""
        return bool(self.cfg and self.cfg["review"]["when_queued"] == "process" and self._provider_factory)

    def _licenses(self) -> dict[str, str]:
        """License id -> name, from Flickr once, then cached in the database."""
        cached = self.store.get_meta("licenses")
        if cached:
            return json.loads(cached)
        try:
            names = {str(l["id"]): l["name"] for l in
                     self.client.call("flickr.photos.licenses.getInfo")["licenses"]["license"]}
        except Exception:
            return {}
        self.store.set_meta("licenses", json.dumps(names))
        return names

    def find(self, text: str) -> str | None:
        """A photo id, or a Flickr photo URL (…/photos/<user>/<id>…), if it's in the library."""
        m = re.search(r"/photos/[^/]+/(\d+)", text) or re.fullmatch(r"\s*(\d+)\s*", text)
        pid = m.group(1) if m else None
        return pid if pid and self.store.photo(pid) else None

    def item(self, photo_id: str) -> dict | None:
        q, p = self.store.queue_item(photo_id), self.store.photo(photo_id)
        if not p:
            return None
        if not q:
            q = {"status": "not_queued", "changes": [], "result": None}
        if photo_id in self.processing:
            q = {**q, "status": "processing"}
        titles = {a["id"]: a["title"] for a in self.store.albums()}
        in_albums = sorted(((titles.get(a, a), a) for a in self.store.memberships().get(photo_id, set())),
                           key=lambda t: t[0].lower())
        albums = [t for t, _ in in_albums]
        large = (f"https://live.staticflickr.com/{p['server']}/{photo_id}_{p['secret']}_b.jpg"
                 if p["server"] and p["secret"] else p["thumb_url"])
        result = q["result"] if q["status"] != "applying" else None
        return {
            "photo_id": photo_id, "status": q["status"], "result": result,
            "changes": [Change.from_dict(c).to_dict() for c in q["changes"]],
            "photo": {"title": p["title"], "description": p["description"], "tags": (p["tags"] or "").split(),
                      "albums": albums, "album_links": [{"title": t, "id": a} for t, a in in_albums], "date_taken": p["date_taken"], "image": large, "thumb": p["thumb_url"],
                      "url": photo_page(self.store.get_meta("nsid", "me"), photo_id),
                      "camera": self.store.cameras().get(photo_id, ""), "media": p["media"]},
            "stats": {"views": p["views"], "faves": p["faves"], "comments": p["comments"],
                      "license": self.licenses.get(str(p["license"]), p["license"]) if p["license"] is not None else None,
                      "width": p["width"], "height": p["height"], "uploaded": p["date_upload"],
                      **self.store.engagement(photo_id)},
        }

    def decide(self, photo_id: str, choices: list[dict]) -> dict:
        """choices: one per queued change, in order: {"approve": bool, "value": ...} where value
        is your edited title/description, or the list of tags you kept. Returns at once."""
        q = self.store.queue_item(photo_id)
        if not q:
            raise KeyError(photo_id)
        if q["status"] == "applying" or photo_id in self.processing:
            return {"status": q["status"]}
        self.store.queue_finish(photo_id, "applying", {"choices": choices})   # survives a restart
        if not self.background:
            return self._apply(self.store, photo_id)
        self.jobs.put(("apply", photo_id))
        return {"status": "applying"}

    def send(self, photo_id: str) -> dict:
        """Into review (from the gallery): processed now or when opened, by review.when_queued."""
        if not self.store.photo(photo_id):
            raise KeyError(photo_id)
        if not self.store.queue_add(photo_id):
            return {"status": "busy" if photo_id in self.processing else self.store.queue_item(photo_id)["status"]}
        return self.process(photo_id, if_new=True) if self._process_on_arrival() else {"status": "new"}

    def ignore(self, photo_id: str) -> dict:
        """Out of review, changing nothing (and remembering nothing: it can be sent again)."""
        q = self.store.queue_item(photo_id)
        if not q:
            raise KeyError(photo_id)
        if q["status"] == "applying" or photo_id in self.processing:
            return {"status": "busy"}
        self.store.queue_remove(photo_id)
        return {"status": "ignored"}

    def process(self, photo_id: str, if_new: bool = False, tags: str = "replace") -> dict:
        """(Re-)process one photo or video now: EXIF, classify, plan; it stays in review. Returns at once.
        if_new: only if it's waiting to be processed (on arrival, or the reviewer asking ahead).
        tags: "replace" its tags (default; you still approve them), or "add" to them."""
        if tags not in ("add", "replace"):
            raise ValueError(tags)
        if not self.store.photo(photo_id):
            raise KeyError(photo_id)
        q = self.store.queue_item(photo_id)
        if if_new and not (q and q["status"] == "new"):
            return {"status": q["status"] if q else "not_queued"}
        if (q and q["status"] == "applying") or photo_id in self.processing:
            return {"status": "busy"}
        if self.cfg is None:
            raise RuntimeError("processing isn't available in this session")
        self.processing.add(photo_id)
        if tags == "add":
            self.tag_modes[photo_id] = tags
        if not if_new:                                  # you asked: the latest image, and a new look at it
            self.fresh.add(photo_id)
        if not self.background:
            return self._process(self.store, photo_id)
        self.jobs.put(("process", photo_id))
        return {"status": "processing"}

    def jobs_status(self, since: int = 0) -> dict:
        return {"applying": self.store.queue_counts().get("applying", 0), "processing": len(self.processing),
                "syncing": self.syncing, "finished": [j for j in self.recent if j["seq"] > since], "seq": self._seq}

    def queue_view(self) -> dict:
        """The Queue page's line: the job running now and every job waiting, in the one order the worker takes
        them. Each gets an estimated start, from how long recent jobs of its kind took (their waits for the
        rate limit included); none once a kind with no history is ahead. Plus each service's limits and use."""
        waiting = list(self.jobs.queue)                       # a snapshot; the worker may move on
        shown = waiting[:200]
        ids = [pid for kind, pid in shown if kind in PHOTO_JOBS]
        if self.running and self.running["kind"] in PHOTO_JOBS:
            ids.append(self.running["photo_id"])
        cards = self.store.cards(ids)
        took: dict[str, list[float]] = {}
        for j in self.recent:
            took.setdefault(j["kind"], []).append(j["seconds"])
        took = {k: sum(v) / len(v) for k, v in took.items()}

        def job(kind, pid, **extra):
            c = cards.get(pid) if kind in PHOTO_JOBS else None
            return {"kind": kind, "photo_id": pid if kind in PHOTO_JOBS else None, "mode": (pid or "due") if kind == "sync" else None,
                    "title": {"sync": "Sync", "wx": "WeatherFlow"}.get(kind) or ((c or {}).get("title") or pid),
                    "thumb": ((c or {}).get("sizes") or {}).get("q", ""), **extra}

        eta: float | None = 0.0
        running = None
        if self.running:
            elapsed = time.time() - self.running["started"]
            k = self.running["kind"]
            running = job(k, self.running["photo_id"], started=self.running["started"], typical=took.get(k))
            eta = max(0.0, took[k] - elapsed) if k in took else None
        line = []
        for i, (kind, pid) in enumerate(waiting, 1):
            if i <= len(shown):
                line.append(job(kind, pid, position=i, starts_in=None if eta is None else round(eta)))
            eta = None if eta is None or kind not in took else eta + took[kind]
        by_kind: dict[str, int] = {}
        for kind, _ in waiting:
            by_kind[kind] = by_kind.get(kind, 0) + 1
        wx = self.weather.client if self.weather else None
        cfg = self.cfg or {}
        limiter = getattr(self.client, "limiter", None)
        return {
            "running": running, "waiting": len(waiting), "line": line, "by_kind": by_kind,
            "took": {k: round(v, 1) for k, v in took.items()}, "done_in": None if eta is None else round(eta),
            "services": {
                "flickr": {"configured": self.client is not None, "calls": getattr(self.client, "call_count", 0),
                           "calls_per_second": FLICKR_CALLS_PER_SECOND,
                           "next_slot": limiter.next_free() if hasattr(limiter, "next_free") else None},
                "weatherflow": {"configured": wx is not None, "calls": wx.call_count if wx else 0,
                                "calls_per_second": WEATHERFLOW_CALLS_PER_SECOND},
                "vision": {"model": cfg.get("vision", {}).get("model"), "provider": cfg.get("vision", {}).get("provider"),
                           "concurrency": VISION_CONCURRENCY, **self.vision_usage},
            },
            "recent": list(reversed(self.recent)),
        }

    def wait(self) -> None:
        """Block until every queued job is done (tests, shutdown)."""
        self.jobs.join()

    def _worker(self) -> None:
        store = self.store   # this thread's own connection
        while True:
            kind, pid = self.jobs.get()
            started, calls0 = time.time(), getattr(self.client, "call_count", 0)
            self.running = {"kind": kind, "photo_id": pid, "started": started}
            try:
                if kind == "sync":
                    out = self._sync(store, pid or None)
                elif kind == "wx":
                    out = self.weather.sync(store, log=self.log)
                else:
                    out = self._apply(store, pid) if kind == "apply" else self._process(store, pid)
            except Exception as e:  # network trouble etc.
                if kind == "apply":   # back in review, to apply again
                    store.queue_finish(pid, "pending", {"error": str(e), "failed_at": int(time.time())})
                if kind == "sync":
                    store.set_meta("last_sync_result", json.dumps({"error": str(e), "at": int(time.time())}))
                self.processing.discard(pid)
                self.syncing = False
                out = {"status": "failed", "error": str(e)}
            try:   # recorded before task_done(), so wait() returns with the job listed
                photo = store.photo(pid) if kind in PHOTO_JOBS else None
                self._seq += 1
                self.recent.append({"seq": self._seq, "kind": kind, "photo_id": pid, "status": out["status"],
                                    "title": {"sync": "sync", "wx": "WeatherFlow"}.get(kind) or (photo["title"] if photo else "") or pid,
                                    "rows": out.get("rows"), "calls": out.get("calls"),
                                    "flickr_calls": getattr(self.client, "call_count", 0) - calls0,
                                    "applied": out.get("stats", {}).get("applied", 0),
                                    "failed": out.get("stats", {}).get("failed", 0), "error": out.get("error", ""),
                                    "started": started, "seconds": round(time.time() - started, 1),
                                    "input_tokens": out.get("input_tokens", 0),
                                    "output_tokens": out.get("output_tokens", 0)})
                self._log_job(self.recent[-1])
            finally:
                self.running = None
                self.jobs.task_done()

    @staticmethod
    def _log_job(j: dict) -> None:
        """A finished job in the activity log: what it was, what came of it, the calls it made."""
        bits = [j["title"] if j["kind"] in PHOTO_JOBS else ""]
        if j["kind"] == "apply" and (j["applied"] or j["failed"]):
            bits.append(f"{j['applied']} applied" + (f", {j['failed']} failed" if j["failed"] else ""))
        if j.get("flickr_calls"):
            bits.append(f"{j['flickr_calls']} Flickr calls")
        if j["kind"] == "wx" and j["rows"] is not None:
            bits.append(f"{j['rows']} rows, {j['calls']} calls")
        if j["input_tokens"]:
            bits.append(f"{j['input_tokens'] + j['output_tokens']} tokens")
        if j["error"]:
            bits.append(j["error"][:200])
        activity.log.record("jobs", JOB_NAMES.get(j["kind"], j["kind"]), " · ".join(b for b in bits if b), j["status"],
                            error=j["status"] == "failed", kind="job", ms=int(j["seconds"] * 1000))

    def _sync(self, store, mode: str | None) -> dict:
        from ..pipeline import process
        from ..schedule import run_due

        self.syncing = True
        try:
            before = store.latest_upload()
            out = run_due(self.cfg, self.client, store, force=mode, log=self.log)
            out["new_photos"] = self._queue_new(store, before)
            store.set_meta("last_sync_result", json.dumps({**out, "at": int(time.time())}, default=str))
        finally:
            self.syncing = False
        return {"status": "done", **out}

    def _queue_new(self, store, before: int | None) -> int:
        """Photos uploaded since the last sync go into review: processed now (a job each) or when
        opened, by review.when_queued. Any that fail to process wait to be processed when opened.
        The first sync of an empty library queues nothing: that's the backlog (Photos → Not processed)."""
        mark = store.get_meta("new_photos_after")
        mark = int(mark) if mark is not None else before
        latest = store.latest_upload()
        if latest is not None:
            store.set_meta("new_photos_after", max(latest, mark or 0))
        if mark is None:
            return 0
        new = store.queue_new_photos(mark)
        if new and self._process_on_arrival():
            try:
                self._vision()
            except RuntimeError:          # no vision key yet: they wait in review
                return len(new)
            for pid in reversed(new):     # oldest first
                self.process(pid, if_new=True)
        return len(new)

    def _count_vision(self, stats: dict) -> dict:
        self.vision_usage["photos"] += stats.get("queued", 0) + stats.get("nothing_to_change", 0)
        self.vision_usage["input_tokens"] += stats.get("input_tokens", 0)
        self.vision_usage["output_tokens"] += stats.get("output_tokens", 0)
        return stats

    def _vision(self):
        """The vision provider, made on first use. A missing key is a RuntimeError here, not
        the CLI's SystemExit (which would stop this worker thread for good)."""
        if self._provider is None and self._provider_factory:
            try:
                self._provider = self._provider_factory()
            except SystemExit as e:
                raise RuntimeError(f"{e.code} Set it in Settings → Connections.")
        return self._provider

    def _process(self, store, photo_id: str) -> dict:
        from ..pipeline import process

        fresh = photo_id in self.fresh
        try:
            stats = process(self.cfg, store, self.client, self._vision(), photo_ids=[photo_id], rebuild=True,
                            refresh_each=fresh, reclassify=fresh, tag_mode=self.tag_modes.get(photo_id, "replace"),
                            log=lambda *a: None, **self.process_kw)
        finally:
            self.processing.discard(photo_id)
            self.tag_modes.pop(photo_id, None)
            self.fresh.discard(photo_id)
        self._count_vision(stats)
        tokens = {"input_tokens": stats["input_tokens"], "output_tokens": stats["output_tokens"]}
        if stats["retry_later"]:
            return {"status": "failed", "error": "EXIF or classification failed; try again", **tokens}
        return {"status": store.queue_item(photo_id)["status"], **tokens}   # pending

    def _apply(self, store, photo_id: str) -> dict:
        """Your choices, applied; anything that fails is tried once more. Done: out of review.
        Still failing: back in review with just those changes, and why."""
        q = store.queue_item(photo_id)
        choices = (q["result"] or {}).get("choices", [])
        proposals = q["changes"][:len(choices)]
        changes = [_with_choice(Change.from_dict(d), choice) for d, choice in zip(proposals, choices)]
        started, todo, errors = int(time.time()), changes, []
        stats = {"applied": 0, "failed": 0}
        for _attempt in range(2):
            since = int(time.time())
            try:   # (apply_changes re-reads the photo from Flickr before any title/description write.)
                stats["applied"] += apply_changes(self.client, store, todo, log=self.log)["applied"]
            except Exception as e:      # network trouble: whatever it didn't reach is retried
                errors.append(str(e))
            todo = unfinished(store, todo, since)
            if not todo:
                break
        if not todo:
            store.queue_remove(photo_id)
            return {"status": "applied", "stats": stats}
        stats["failed"] = len(todo)
        at = {id(c): i for i, c in enumerate(changes)}
        left = [proposals[at[id(c)]] for c in todo]
        why = "; ".join(dict.fromkeys(errors + store.failures_since(photo_id, started))) or "failed twice"
        store.queue_put(photo_id, "pending", left)
        result = {"error": why, "failed_at": int(time.time()), "changes": outcomes(store, todo)}
        store.queue_finish(photo_id, "pending", result)
        return {"status": "failed", "error": why, "stats": stats}


def _with_choice(c: Change, choice: dict) -> Change:
    """Your decision on one change from the page: {"approve": bool, "value": text or kept tags}.
    (Photos queued for applying before this format sent "target"; that still works.)"""
    c.approved = bool(choice.get("approve"))
    value = choice.get("value", choice.get("target"))
    if c.action in TAG_ACTIONS:
        kept = split_tags(value if value is not None else c.new)
        c.edited = kept if kept != c.tags else None
        c.approved = c.approved and bool(kept)
    elif value is not None and c.action in META_ACTIONS and str(value).strip() != c.new:
        c.edited = str(value).strip()
    return c
