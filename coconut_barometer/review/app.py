"""The review tool's logic: the photos in review and their proposed changes, your decisions (one photo
at a time, or one kind of change across many), undoing an apply, and a background worker that makes
the Flickr (and vision) calls. No web code here; see web/api/review.py for the HTTP side and
web/static/js/review*.js for the page."""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from collections import deque

from .. import activity
from ..apply import apply_changes, group_hold, outcomes, unfinished
from ..changes import META_ACTIONS, TAG_ACTIONS, Change, split_tags
from ..config import FLICKR_CALLS_PER_SECOND, VISION_CONCURRENCY, WEATHERFLOW_CALLS_PER_SECOND
from ..errors import caught
from ..flickr_client import FlickrCallError
from ..planner import photo_page
from ..util import norm_tag

# A photo is in review from being sent there until it's applied or ignored (see store/queue.py):
# "new" (waiting to be processed), "pending" (ready), "applying". Shown as one list.
STATUSES = ("new", "pending", "applying")


JOB_NAMES = {"apply": "apply approved changes", "process": "process photo", "undo": "undo an apply", "sync": "Flickr sync",
             "wx": "WeatherFlow sync", "replan": "re-plan review"}
logger = logging.getLogger(__name__)
PHOTO_NOT_FOUND = 1      # flickr.photos.getInfo's "Photo not found" (other methods number their errors their own way)
PHOTO_JOBS = ("apply", "process", "undo")   # the others aren't about a photo: "sync", "wx" (WeatherFlow), "replan"
OTHER_JOBS = {"sync": "Sync", "wx": "WeatherFlow", "replan": "Re-plan"}


class JobQueue(queue.Queue):
    """The worker's line, in the order it takes jobs (`.queue`). put_front: ahead of everything waiting, but
    behind any other job put there before it, so jobs you asked for go first, in the order you asked."""

    def _init(self, maxsize):
        super()._init(maxsize)
        self.urgent = 0                     # how many at the front were put there

    def _get(self):
        self.urgent = max(0, self.urgent - 1)
        return super()._get()

    def put_front(self, item) -> None:
        with self.not_full:
            self.queue.insert(self.urgent, item)
            self.urgent += 1
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def to_front(self, item) -> bool:
        """A job already waiting, moved up as if put_front now (False if it isn't waiting, or is up there already)."""
        with self.mutex:
            try:
                at = self.queue.index(item)
            except ValueError:
                return False
            if at < self.urgent:
                return False
            del self.queue[at]
            self.queue.insert(self.urgent, item)
            self.urgent += 1
            return True


# Bulk review: the kinds of change it lists, by action.
BULK_KINDS = {"location": "set_location", "tags": "set_tags", "title": "set_title", "description": "set_description",
              "album": "add_to_album", "group": "add_to_group"}


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
        self.jobs = JobQueue()   # ("apply"|"process", photo_id) or ("sync", mode); what you ask for by hand goes first
        self.syncing = False
        self.weather = None                             # the web app's WeatherService, for ("wx", "") jobs
        self.processing: set[str] = set()
        self.fresh: set[str] = set()                    # asked for again: re-read from Flickr, re-classify
        self.recent: deque[dict] = deque(maxlen=50)   # finished jobs, newest last
        self._seq = 0
        self.running: dict | None = None                # the job the worker is on: {kind, photo_id, started}
        self.vision_usage = {"photos": 0, "input_tokens": 0, "output_tokens": 0}   # since start
        self.licenses = self._licenses()
        pruned = store.queue_prune()     # photos deleted on Flickr before their review row went with them (#76)
        if pruned:
            logger.warning("removed %d photo(s) from review that are no longer in the library", pruned)
        if background:
            for pid in reversed(store.queue_ids(["applying"])):   # oldest first
                self.jobs.put(("apply", pid))
            if self._process_on_arrival():
                for pid in reversed(store.queue_ids(["new"])):
                    try:
                        self.process(pid, if_new=True)
                    except KeyError:      # gone from the library since
                        store.queue_remove(pid)
            self._resume_requested(store)
            # Started once the line is rebuilt, so it takes the jobs in the line's order.
            threading.Thread(target=self._worker, name="review-worker", daemon=True).start()

    def _resume_requested(self, store) -> None:
        """The re-processes, undos and re-plans you asked for that hadn't finished when the app stopped, queued
        again the way they were first (a re-process at the front), oldest first."""
        for kind, pid in store.jobs_requested():
            try:
                out = (self.process(pid) if kind == "process" else self.undo(pid) if kind == "undo"
                       else self.replan() if kind == "replan" else {"status": "unknown"})
            except (KeyError, RuntimeError):     # gone since, or this session can't do it
                out = {"status": "gone"}
            if out["status"] not in ("processing", "undoing", "queued"):
                store.job_done(kind, pid)

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
            "duplicate": (q["result"] or {}).get("duplicate"),
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
        if any(c.get("action") == "set_location" for c in q["changes"]):   # not sent again by the daily sweep
            self.store.record(photo_id, "set_location", "", "rejected")
        self.store.queue_remove(photo_id)
        return {"status": "ignored"}

    def duplicate_decision(self, photo_id: str, action: str) -> dict:
        """Keep a suspected re-upload for normal review, or delete that new Flickr upload."""
        q = self.store.queue_item(photo_id)
        if not q or not (q.get("result") or {}).get("duplicate"):
            raise KeyError(photo_id)
        if q["status"] != "pending":
            return {"status": "busy"}
        if action == "keep":
            self.store.queue_finish(photo_id, "pending", {"new_upload": True, "duplicate_action": "keep"})
            return self.process(photo_id)
        if action != "delete":
            raise ValueError("action must be keep or delete")
        if self.client is None:
            return {"status": "failed", "error": "Flickr is not connected"}
        try:
            self.client.call("flickr.photos.delete", photo_id=photo_id)
        except Exception as e:      # a token from before delete access was asked for: Flickr says 99
            return {"status": "failed", "error": f"{e}. Reconnect Flickr (Settings → Connections) to allow deleting"}
        self.store.delete_photos([photo_id])
        return {"status": "deleted"}

    def _gone_on_flickr(self, photo_id: str) -> bool:
        """Whether Flickr says the photo no longer exists. Asked only when an apply failed: one call."""
        try:
            self.client.call("flickr.photos.getInfo", photo_id=photo_id)
        except FlickrCallError as e:
            return e.code == PHOTO_NOT_FOUND
        except Exception:     # noqa: BLE001 - can't tell (the network): it stays in review with its error
            return False
        return False

    def applied(self, limit: int = 50) -> list[dict]:
        """The latest applies, newest first, for the Applied list: what each wrote, and whether it can be undone."""
        from ..undo import describe

        rows = self.store.recent_applied(limit)
        cards = self.store.cards([r["photo_id"] for r in rows])
        latest = {}
        for r in rows:                       # only a photo's latest apply still standing can be undone
            if r["undone_at"] is None:
                latest.setdefault(r["photo_id"], r["id"])
        undoing = {pid for kind, pid in list(self.jobs.queue) if kind == "undo"}
        return [{"id": r["id"], "photo_id": r["photo_id"], "at": r["at"], "undone_at": r["undone_at"],
                 "result": r["result"], "what": [describe(e) for e in r["changes"]],
                 "title": (cards.get(r["photo_id"]) or {}).get("title") or r["photo_id"],
                 "thumb": ((cards.get(r["photo_id"]) or {}).get("sizes") or {}).get("q", ""),
                 "can_undo": latest.get(r["photo_id"]) == r["id"] and r["photo_id"] not in undoing,
                 "undoing": r["photo_id"] in undoing} for r in rows]

    # ---- bulk review --------------------------------------------------------------------
    def bulk(self, kind: str, value: str = "") -> dict:
        """Every ready photo's proposed change of one kind (BULK_KINDS), for approving many at once; value
        narrows it: a location's confidence (high, medium, low, none) or an album's or group's name.
        counts: how many of each kind are waiting (and per confidence, album or group); busy: photos in review
        not listed yet, still being processed or applied (the page refreshes until they're done); held: groups
        whose posting limit is used up, and when they take more."""
        action = BULK_KINDS[kind]
        busy = self.processing | set(self.store.queue_ids(["applying"]))
        counts: dict[str, int] = {}
        sub: dict[str, int] = {}
        group_ids: dict[str, str] = {}
        found = []
        for pid in self.store.queue_ids(["pending"]):
            if pid in busy:
                continue
            for i, c in enumerate(self.store.queue_item(pid)["changes"]):
                k = next((k for k, a in BULK_KINDS.items() if a == c.get("action")), None)
                if not k:
                    continue
                counts[k] = counts.get(k, 0) + 1
                if k != kind:
                    continue
                group = _bulk_group(c)
                sub[group] = sub.get(group, 0) + 1
                if c.get("action") == "add_to_group":
                    group_ids[group] = c.get("album_id") or ""
                if not value or group == value:
                    found.append((pid, i, c))
        cards = self.store.cards([pid for pid, _, _ in found[:500]])
        items = [{"photo_id": pid, "index": i, "change": Change.from_dict(c).to_dict(),
                  "title": (cards.get(pid) or {}).get("title") or "", "tags": (self.store.photo(pid)["tags"] or "").split(),
                  "thumb": ((cards.get(pid) or {}).get("sizes") or {}).get("q", "")} for pid, i, c in found[:500]]
        waiting = (set(self.store.queue_ids(["new"])) | busy) & set(self.store.queue_ids(["new", "pending", "applying"]))
        held = {name: h["why"] for name, gid in group_ids.items() if (h := group_hold(self.store, gid))}
        return {"counts": counts, "groups": dict(sorted(sub.items(), key=lambda kv: -kv[1])), "items": items,
                "total": len(found), "busy": len(waiting), "held": held}

    def bulk_decide(self, verb: str, picks: list[dict]) -> dict:
        """picks: [{photo_id, index, action}]. verb "apply": those changes, as proposed (each photo's others
        stay in review); "drop": those proposals, gone (a photo with none left leaves review)."""
        by_photo: dict[str, list[int]] = {}
        for p in picks:
            by_photo.setdefault(p["photo_id"], []).append(int(p["index"]))
        actions = {(p["photo_id"], int(p["index"])): p["action"] for p in picks}
        done, skipped = 0, 0
        for pid, idx in by_photo.items():
            q = self.store.queue_item(pid)
            if not q or q["status"] != "pending" or pid in self.processing:
                skipped += len(idx)
                continue
            # Only the ones still as listed: the photo may have been re-planned since.
            idx = [i for i in sorted(set(idx)) if i < len(q["changes"]) and q["changes"][i].get("action") == actions[(pid, i)]]
            skipped += len(by_photo[pid]) - len(idx)
            if not idx:
                continue
            chosen = [q["changes"][i] for i in idx]
            rest = [c for i, c in enumerate(q["changes"]) if i not in idx]
            done += len(idx)
            if verb == "drop":
                if any(c.get("action") == "set_location" for c in chosen):   # the daily sweep leaves it out
                    self.store.record(pid, "set_location", "", "rejected")
                if rest:
                    self.store.queue_put(pid, "pending", rest)
                else:
                    self.store.queue_remove(pid)
                continue
            self.store.queue_put(pid, "pending", chosen + rest)        # the chosen first: _apply takes those
            self.decide(pid, [{"approve": True} for _ in chosen])
        return {"status": "applying" if verb == "apply" else "dropped", "changes": done, "skipped": skipped}

    def replan(self) -> dict:
        """Every ready photo in review planned again with the current settings, from what's already known
        (cached vision results, EXIF): no vision or Flickr calls. Returns at once."""
        if any(kind == "replan" for kind, _ in list(self.jobs.queue)):
            return {"status": "busy"}
        if not self.background:
            return self._replan(self.store)
        self.store.job_requested("replan")
        self.jobs.put(("replan", ""))
        return {"status": "queued"}

    def _replan(self, store) -> dict:
        from ..planner import build_plan
        from ..vision import provider_key

        ids = [pid for pid in store.queue_ids(["pending"]) if pid not in self.processing
               and not (store.queue_item(pid)["result"] or {}).get("duplicate")]    # held: keep or delete first
        if not ids:
            return {"status": "done", "rows": 0}
        rows = build_plan(self.cfg, store, provider_key(self.cfg["vision"]), photo_ids=set(ids),
                          geocoder=self.process_kw.get("geocoder"), any_vision=True)
        changes: dict[str, list[dict]] = {}
        for r in rows:
            changes.setdefault(r.photo_id, []).append(r.to_dict())
        for pid in ids:
            q = store.queue_item(pid)
            if q and q["status"] == "pending":          # not applied meanwhile
                store.queue_put(pid, "pending", changes.get(pid, []))
        return {"status": "done", "rows": len(ids)}

    def undo(self, photo_id: str) -> dict:
        """Undo the photo's latest apply (see undo.py). Returns at once."""
        if not self.store.last_applied(photo_id):
            raise KeyError(photo_id)
        q = self.store.queue_item(photo_id)
        if (q and q["status"] == "applying") or photo_id in self.processing:
            return {"status": "busy"}
        if not self.background:
            return self._undo(self.store, photo_id)
        self.store.job_requested("undo", photo_id)
        self.jobs.put(("undo", photo_id))
        return {"status": "undoing"}

    def process(self, photo_id: str, if_new: bool = False) -> dict:
        """(Re-)process one photo or video now: EXIF, classify, plan; it stays in review. Returns at once.
        if_new: only if it's waiting to be processed (on arrival, or the reviewer asking ahead).
        Without it (you asked for it) the job goes to the front of the worker's line, ahead of syncs and
        photos waiting their turn; one already waiting in line is moved up, and given the fresh look.
        Its tags are proposed as one whole list, replacing the ones it has (you approve them)."""
        if not self.store.photo(photo_id):
            raise KeyError(photo_id)
        q = self.store.queue_item(photo_id)
        if if_new and not (q and q["status"] == "new"):
            return {"status": q["status"] if q else "not_queued"}
        if not if_new and photo_id in self.processing and self.jobs.to_front(("process", photo_id)):
            self.fresh.add(photo_id)
            self.store.job_requested("process", photo_id)
            return {"status": "processing"}
        if (q and q["status"] == "applying") or photo_id in self.processing:
            return {"status": "busy"}
        if self.cfg is None:
            raise RuntimeError("processing isn't available in this session")
        self.processing.add(photo_id)
        if not if_new:                                  # you asked: the latest image, and a new look at it
            self.fresh.add(photo_id)
        if not self.background:
            return self._process(self.store, photo_id)
        if not if_new:
            self.store.job_requested("process", photo_id)
        (self.jobs.put if if_new else self.jobs.put_front)(("process", photo_id))
        return {"status": "processing"}

    def jobs_status(self, since: int = 0) -> dict:
        """queued: every job on the worker, the one running and those waiting (the Status tab's dot pulses)."""
        return {"applying": self.store.queue_counts().get("applying", 0), "processing": len(self.processing),
                "syncing": self.syncing, "queued": self.jobs.qsize() + (1 if self.running else 0),
                "finished": [j for j in self.recent if j["seq"] > since], "seq": self._seq}

    def jobs_view(self) -> dict:
        """The Status page's jobs: the job running now and every job waiting, in the one order the worker takes
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
                    "title": OTHER_JOBS.get(kind) or ((c or {}).get("title") or pid),
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
                elif kind == "undo":
                    out = self._undo(store, pid)
                elif kind == "replan":
                    out = self._replan(store)
                else:
                    out = self._apply(store, pid) if kind == "apply" else self._process(store, pid)
            except Exception as e:  # network trouble etc.
                caught(e, f"{kind} job")
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
                                    "title": OTHER_JOBS.get(kind) or (photo["title"] if photo else "") or pid,
                                    "rows": out.get("rows"), "calls": out.get("calls"),
                                    "flickr_calls": getattr(self.client, "call_count", 0) - calls0,
                                    "applied": out.get("stats", {}).get("applied", 0),
                                    "failed": out.get("stats", {}).get("failed", 0), "error": out.get("error", ""),
                                    "started": started, "seconds": round(time.time() - started, 1),
                                    "input_tokens": out.get("input_tokens", 0),
                                    "output_tokens": out.get("output_tokens", 0)})
                self._log_job(self.recent[-1])
            finally:
                if kind in ("process", "undo", "replan"):     # done, or failed: either way not to be redone
                    store.job_done(kind, pid)
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
            if {"full", "photo_list"} & set(out["ran"]) and self.cfg["review"]["missing_location"]:
                out["missing_location"] = self._queue_missing_location(store)
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

    def _queue_missing_location(self, store) -> int:
        """review.missing_location, after each read of every photo: the ones with no geotag that aren't
        in review go in, ready, proposed from what's already known (cached vision results, EXIF): no
        vision or Flickr calls, so a backlog of hundreds costs nothing. Re-process one for a fresh look."""
        from ..planner import build_plan
        from ..vision import provider_key

        ids = store.missing_location_ids()
        if not ids:
            return 0
        rows = build_plan(self.cfg, store, provider_key(self.cfg["vision"]), photo_ids=set(ids),
                          geocoder=self.process_kw.get("geocoder"), any_vision=True)
        changes: dict[str, list[dict]] = {}
        for r in rows:
            changes.setdefault(r.photo_id, []).append(r.to_dict())
        for pid in ids:
            store.queue_put(pid, "pending", changes.get(pid, []))
        self.log(f"  {len(ids)} photo(s) without a location sent to review")
        return len(ids)

    def places(self, query: str) -> dict:
        """The reviewer's map search (see gazetteer.search)."""
        from .. import gazetteer
        return gazetteer.search(query)

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

    def _duplicate_of(self, store, photo_id: str) -> dict | None:
        """A new upload that looks like a photo already in the library (duplicates.py): held for you to keep or
        delete. None if it isn't a new upload, you kept it, it's local mode, or it can't tell (its thumbnail
        wouldn't download): then it's reviewed as usual."""
        from .. import local
        from ..duplicates import find_duplicate

        result = (store.queue_item(photo_id) or {}).get("result") or {}
        if not result.get("new_upload") or result.get("duplicate_action") == "keep":
            return None
        if local.on():          # #92: the server's job (the first check fingerprints the whole library)
            local.held("checking new uploads for duplicates")
            return None
        try:
            return find_duplicate(store, photo_id, downloader=self.process_kw.get("downloader"))
        except Exception as e:
            self.log(f"duplicate check for {photo_id} skipped: {e}")
            return None

    def _process(self, store, photo_id: str) -> dict:
        from ..pipeline import process

        match = self._duplicate_of(store, photo_id)
        if match:
            store.queue_finish(photo_id, "pending", {"new_upload": True, "duplicate": match})
            self.processing.discard(photo_id)
            self.fresh.discard(photo_id)
            return {"status": "duplicate", "match": match}

        fresh = photo_id in self.fresh
        try:
            stats = process(self.cfg, store, self.client, self._vision(), photo_ids=[photo_id], rebuild=True,
                            refresh_each=fresh, reclassify=fresh, log=lambda *a: None, **self.process_kw)
        finally:
            self.processing.discard(photo_id)
            self.fresh.discard(photo_id)
        self._count_vision(stats)
        tokens = {"input_tokens": stats["input_tokens"], "output_tokens": stats["output_tokens"]}
        if stats["retry_later"]:
            return {"status": "failed", "error": "EXIF or classification failed; try again", **tokens}
        return {"status": store.queue_item(photo_id)["status"], **tokens}   # pending

    def _undo(self, store, photo_id: str) -> dict:
        """Put back what the photo's latest apply replaced, where Flickr still has what it wrote."""
        from ..undo import undo

        entry = store.last_applied(photo_id)
        if not entry:
            return {"status": "done", "error": "nothing to undo"}
        out = undo(self.client, store, photo_id, entry["changes"], log=self.log)
        store.mark_undone(entry["id"], out)
        if out["skipped"]:
            return {"status": "done" if out["undone"] else "failed", "error": "; ".join(out["skipped"])}
        return {"status": "done"}

    def _apply(self, store, photo_id: str) -> dict:
        """Your choices, applied; anything that fails is tried once more. Done: out of review.
        Still failing: back in review with just those changes, and why."""
        from ..undo import entries

        q = store.queue_item(photo_id)
        choices = (q["result"] or {}).get("choices", [])
        proposals, rest = q["changes"][:len(choices)], q["changes"][len(choices):]   # rest: not decided, stay in review
        changes = [_with_choice(Change.from_dict(d), choice) for d, choice in zip(proposals, choices)]
        before = store.photo(photo_id)
        tags_before = (before["tags"] if before is not None else "") or ""
        started, todo, errors = int(time.time()), changes, []
        stats = {"applied": 0, "failed": 0}
        turned_down = []        # a group's limit or rules: not tried again now
        for _attempt in range(2):
            since = int(time.time())
            try:   # (apply_changes re-reads the photo from Flickr before any title/description write.)
                stats["applied"] += apply_changes(self.client, store, todo, log=self.log)["applied"]
            except Exception as e:      # network trouble: whatever it didn't reach is retried
                caught(e, f"apply {photo_id}")
                errors.append(str(e))
            todo = unfinished(store, todo, since)
            no = store.turned_down_since(photo_id, since)
            down = [c for c in todo if c.action == "add_to_group" and (c.action, c.new) in no]
            turned_down += down
            todo = [c for c in todo if c not in down]
            if not todo:
                break
        todo += turned_down
        left_out = {id(c) for c in todo}
        written = [c for c in changes if id(c) not in left_out and c.approved and _writes(c)]
        if written:
            store.record_applied(photo_id, entries(store, written, tags_before))
        if not todo:
            if rest:
                store.queue_put(photo_id, "pending", rest)
            else:
                store.queue_remove(photo_id)
            return {"status": "applied", "stats": stats}
        if self._gone_on_flickr(photo_id):         # deleted there since the last full read of the library
            store.delete_photos([photo_id])
            return {"status": "gone", "error": "deleted on Flickr: out of review and the library"}
        stats["failed"] = len(todo)
        at = {id(c): i for i, c in enumerate(changes)}
        left = [proposals[at[id(c)]] for c in todo]
        why = "; ".join(dict.fromkeys(errors + store.failures_since(photo_id, started))) or "failed twice"
        store.queue_put(photo_id, "pending", left + rest)
        result = {"error": why, "failed_at": int(time.time()), "changes": outcomes(store, todo)}
        store.queue_finish(photo_id, "pending", result)
        return {"status": "failed", "error": why, "stats": stats}


def _bulk_group(c: dict) -> str:
    """What bulk review narrows a kind by: a location's confidence, an album's or group's name."""
    if c.get("action") == "set_location":
        has = (c.get("new") or {}).get("lat") is not None
        return (c.get("confidence") or "low") if has else "none"
    if c.get("action") in ("add_to_album", "add_to_group"):
        return str(c.get("new") or "")
    return ""


def _writes(c: Change) -> bool:
    """Approved, does it write anything? (A blanked text, no tags kept, no place picked: no.)"""
    if c.action in TAG_ACTIONS:
        return bool(split_tags(c.value))
    if c.action in META_ACTIONS:
        return bool(str(c.value).strip())
    if c.action == "set_location":
        return bool(c.target)
    return True


def _with_choice(c: Change, choice: dict) -> Change:
    """Your decision on one change from the page: {"approve": bool, "value": text or kept tags}.
    (Photos queued for applying before this format sent "target"; that still works.)"""
    c.approved = bool(choice.get("approve"))
    value = choice.get("value", choice.get("target"))
    if c.action in TAG_ACTIONS:
        kept = split_tags(value if value is not None else c.new)   # set_tags: the whole list, reconciled on the page
        c.edited = kept if kept != c.tags else None
        same = c.action == "set_tags" and {norm_tag(t) for t in kept} == {norm_tag(t) for t in c.old.split()}
        c.approved = c.approved and bool(kept) and not same          # already its tags: nothing to write
    elif value is not None and c.action in META_ACTIONS and str(value).strip() != c.new:
        c.edited = str(value).strip()
    elif c.action == "set_location":
        if value is not None:
            c.edited = _location(value)
        c.approved = c.approved and bool(c.target)
    return c


def _location(value) -> dict:
    """A place picked on the map: {lat, lon, accuracy}, checked; {} if it isn't one."""
    try:
        lat, lon = float(value["lat"]), float(value["lon"])
        acc = int(value.get("accuracy") or 16)
    except (TypeError, KeyError, ValueError):
        return {}
    if not (-90 <= lat <= 90) or lon != lon:
        return {}
    lon = (lon + 180) % 360 - 180                # the map lets you scroll around the world
    return {"lat": round(lat, 6), "lon": round(lon, 6), "accuracy": min(16, max(1, acc)),
            "place": str(value.get("place") or "")[:200]}
