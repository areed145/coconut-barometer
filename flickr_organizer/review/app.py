"""The review tool's logic: one photo at a time from the queue, your decisions, and a
background worker that makes the Flickr (and vision) calls. No web code here; see
server.py for the HTTP side and static/ for the page."""
from __future__ import annotations

import json
import queue
import re
import threading
from collections import deque

from ..apply import apply_changes, outcomes
from ..changes import META_ACTIONS, Change, split_tags
from ..planner import photo_page

STATUSES = ("pending", "applying", "failed", "applied", "rejected", "none", "unprocessed")


class ReviewApp:
    """The review tool's logic. decide() and process() only queue a job; a single
    background worker makes the Flickr (and vision) calls in order, so the rate limit
    holds, and records the outcome. Photos left "applying" when the tool stopped resume.

    cfg/provider are needed only to process photos from the viewer; provider is a
    zero-argument factory so the vision SDK loads on first use."""

    def __init__(self, store, client, log=lambda *a: None, background: bool = True,
                 cfg: dict | None = None, provider=None, process_kw: dict | None = None):
        self.store, self.client, self.log = store, client, log
        self.background = background
        self.cfg, self._provider_factory, self._provider = cfg, provider, None
        self.process_kw = process_kw or {}
        self.jobs: queue.Queue[tuple[str, str]] = queue.Queue()   # ("apply" | "process", photo_id)
        self.processing: set[str] = set()
        self.recent: deque[dict] = deque(maxlen=20)   # finished jobs, newest last
        self._seq = 0
        self.licenses = self._licenses()
        if background:
            threading.Thread(target=self._worker, name="review-worker", daemon=True).start()
            for pid in reversed(store.queue_ids(["applying"])):   # oldest first
                self.jobs.put(("apply", pid))

    def summary(self) -> dict:
        counts = self.store.queue_counts()
        counts["unprocessed"] = len(self.store.unqueued_ids())
        return {s: counts.get(s, 0) for s in STATUSES}

    def queue(self, status: str = "pending", sort: str = "newest") -> list[str]:
        return self.store.unqueued_ids(sort) if status == "unprocessed" else self.store.queue_ids([status], sort)

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
            q = {"status": "unprocessed", "changes": [], "result": None}
        if photo_id in self.processing:
            q = {**q, "status": "processing"}
        titles = {a["id"]: a["title"] for a in self.store.albums()}
        albums = sorted(titles.get(a, a) for a in self.store.memberships().get(photo_id, set()))
        large = (f"https://live.staticflickr.com/{p['server']}/{photo_id}_{p['secret']}_b.jpg"
                 if p["server"] and p["secret"] else p["thumb_url"])
        result = q["result"] if q["status"] != "applying" else None
        return {
            "photo_id": photo_id, "status": q["status"], "result": result,
            "changes": [Change.from_dict(c).to_dict() for c in q["changes"]],
            "photo": {"title": p["title"], "description": p["description"], "tags": (p["tags"] or "").split(),
                      "albums": albums, "date_taken": p["date_taken"], "image": large, "thumb": p["thumb_url"],
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

    def process(self, photo_id: str) -> dict:
        """(Re-)process one photo or video now: EXIF, classify, plan. Returns at once."""
        if not self.store.photo(photo_id):
            raise KeyError(photo_id)
        q = self.store.queue_item(photo_id)
        if (q and q["status"] == "applying") or photo_id in self.processing:
            return {"status": "busy"}
        if self.cfg is None:
            raise RuntimeError("processing isn't available in this session")
        self.processing.add(photo_id)
        if not self.background:
            return self._process(self.store, photo_id)
        self.jobs.put(("process", photo_id))
        return {"status": "processing"}

    def jobs_status(self, since: int = 0) -> dict:
        return {"applying": self.store.queue_counts().get("applying", 0), "processing": len(self.processing),
                "finished": [j for j in self.recent if j["seq"] > since], "seq": self._seq}

    def wait(self) -> None:
        """Block until every queued job is done (tests, shutdown)."""
        self.jobs.join()

    def _worker(self) -> None:
        # sqlite connections belong to one thread, so the worker opens its own.
        store = type(self.store)(self.store.path)
        while True:
            kind, pid = self.jobs.get()
            try:
                out = self._apply(store, pid) if kind == "apply" else self._process(store, pid)
            except Exception as e:  # network trouble etc.
                if kind == "apply":   # keep it under Failed to retry
                    store.queue_finish(pid, "failed", {"error": str(e)})
                self.processing.discard(pid)
                out = {"status": "failed", "error": str(e)}
            finally:
                self.jobs.task_done()
            photo = store.photo(pid)
            self._seq += 1
            self.recent.append({"seq": self._seq, "kind": kind, "photo_id": pid, "status": out["status"],
                                "title": (photo["title"] if photo else "") or pid,
                                "applied": out.get("stats", {}).get("applied", 0),
                                "failed": out.get("stats", {}).get("failed", 0), "error": out.get("error", "")})

    def _process(self, store, photo_id: str) -> dict:
        from ..pipeline import process

        try:
            vcfg = self.cfg["vision"]
            if self._provider is None and self._provider_factory and vcfg.get("enabled", True) and vcfg["categories"]:
                self._provider = self._provider_factory()
            stats = process(self.cfg, store, self.client, self._provider, photo_ids=[photo_id], rebuild=True,
                            include_videos=True, log=lambda *a: None, **self.process_kw)
        finally:
            self.processing.discard(photo_id)
        if stats["retry_later"]:
            return {"status": "failed", "error": "EXIF or classification failed; try again"}
        return {"status": store.queue_item(photo_id)["status"]}   # pending | none

    def _apply(self, store, photo_id: str) -> dict:
        q = store.queue_item(photo_id)
        choices = (q["result"] or {}).get("choices", [])
        changes = [_with_choice(Change.from_dict(d), choice) for d, choice in zip(q["changes"], choices)]
        # (apply_changes re-reads the photo from Flickr before any title/description write.)
        stats = apply_changes(self.client, store, changes, log=self.log)
        result = {"stats": stats, "changes": outcomes(store, changes)}
        status = "failed" if stats["failed"] else ("applied" if stats["applied"] else "rejected")
        store.queue_finish(photo_id, status, result)
        return {"status": status, **result}


def _with_choice(c: Change, choice: dict) -> Change:
    """Your decision on one change from the page: {"approve": bool, "value": text or kept tags}.
    (Photos queued for applying before this format sent "target"; that still works.)"""
    c.approved = bool(choice.get("approve"))
    value = choice.get("value", choice.get("target"))
    if c.action == "add_tags":
        kept = split_tags(value if value is not None else c.new)
        c.edited = kept if kept != c.tags else None
        c.approved = c.approved and bool(kept)
    elif value is not None and c.action in META_ACTIONS and str(value).strip() != c.new:
        c.edited = str(value).strip()
    return c
