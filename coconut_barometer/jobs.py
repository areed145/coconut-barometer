"""The background jobs' lanes (#197): one per outside service, each with its own worker and its own line, so a slow
or rate-limited service only holds up its own work. A Flickr sync that takes hours no longer keeps Apple Music,
MusicBrainz, WeatherFlow or the vision model waiting.

    flickr       sync, apply, undo, re-plan       two workers: one takes anything, the other ("express") only
                                                   what you asked for, so an apply doesn't wait behind a sync
    vision       process (vision model; its few Flickr reads share Flickr's limit)
    weather      WeatherFlow sync
    music        Apple Music read, artist profiles
    musicbrainz  MusicBrainz lookups (1 a second)

Each lane's line is ordered by priority, then by when it was asked:

    USER (0)         you asked for it: an apply, an undo, a re-process, a re-plan
    INTERACTIVE (1)  quick and wanted soon: an Apple Music read
    SCHEDULED (2)    the scheduler's: a Flickr or WeatherFlow sync, photos processed as they arrive
    FILL (3)         filling in, never urgent: MusicBrainz lookups, artist profiles

A job waiting long gains a level each AGE_EVERY, up to INTERACTIVE (never past what you asked for), so nothing waits
forever. Jobs on the same resource never run at once, whichever lanes they're in: one photo's process, apply and undo,
and the one sync.

The worker running a job sets its priority for the thread (priority()), so a rate limiter can tell background calls
from ones someone is waiting on (ratelimit.py keeps part of Flickr's hourly budget for the latter)."""
from __future__ import annotations

import contextlib
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

USER, INTERACTIVE, SCHEDULED, FILL = 0, 1, 2, 3
PRIORITY_NAMES = {USER: "you asked", INTERACTIVE: "interactive", SCHEDULED: "scheduled", FILL: "fill-in"}
AGE_EVERY = 15 * 60          # seconds waiting per level gained

LANES = ("flickr", "vision", "weather", "music", "musicbrainz")
LANE_NAMES = {"flickr": "Flickr", "vision": "Vision", "weather": "WeatherFlow", "music": "Apple Music",
              "musicbrainz": "MusicBrainz"}
KIND_LANE = {"sync": "flickr", "apply": "flickr", "undo": "flickr", "replan": "flickr", "process": "vision",
             "wx": "weather", "music": "music", "profiles": "music", "musicbrainz": "musicbrainz"}
DEFAULT_PRIORITY = {"apply": USER, "undo": USER, "replan": USER, "process": SCHEDULED, "sync": SCHEDULED,
                    "wx": SCHEDULED, "music": INTERACTIVE, "musicbrainz": FILL, "profiles": FILL}
EXPRESS = {"flickr": INTERACTIVE}   # lane -> a second worker taking only jobs at this priority or above
PHOTO_KINDS = ("apply", "process", "undo")

_local = threading.local()


# ---- the running job's priority, for rate limiters ------------------------------------------------------------------
def current_priority() -> int:
    """This thread's job's priority; INTERACTIVE outside a job (a web request: someone's waiting on it)."""
    return getattr(_local, "priority", INTERACTIVE)


def background() -> bool:
    return current_priority() >= SCHEDULED


@contextlib.contextmanager
def priority(level: int):
    before = getattr(_local, "priority", None)
    _local.priority = level
    try:
        yield
    finally:
        if before is None:
            del _local.priority
        else:
            _local.priority = before


# ---- the lines ------------------------------------------------------------------------------------------------------
def lane_of(kind: str) -> str:
    return KIND_LANE.get(kind, "flickr")


def resource_of(kind: str, key: str) -> str:
    """What a job holds while it runs: one photo, or the job kind itself (one sync, one WeatherFlow sync...)."""
    return f"photo:{key}" if kind in PHOTO_KINDS else kind


@dataclass
class Job:
    kind: str
    key: str
    priority: int
    order: int                              # when it was asked (moved up: asked again), for first come first served
    queued_at: float
    lane: str = ""
    started: float | None = None
    worker: str = ""

    @property
    def item(self) -> tuple[str, str]:
        return (self.kind, self.key)

    def effective(self, now: float) -> int:
        """Its priority now: a level better for each AGE_EVERY waited, but not past INTERACTIVE."""
        if self.priority <= INTERACTIVE:
            return self.priority
        return max(INTERACTIVE, self.priority - int((now - self.queued_at) // AGE_EVERY))


@dataclass
class Worker:
    name: str
    lane: str
    best: int | None = None                 # takes only jobs at this priority or better (the express worker)
    job: Job | None = None
    thread: threading.Thread | None = field(default=None, repr=False)


class Lanes:
    """Every lane's line and workers, behind one lock. queue / put / put_front / to_front / qsize / join keep the
    single line's calling style (`.queue`: every waiting job, in the order they'd be taken were there one worker)."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._cv = threading.Condition()
        self._waiting: list[Job] = []
        self._busy: set[str] = set()        # resources held by running jobs
        self._unfinished = 0
        self._order = itertools.count()
        self.workers: list[Worker] = []
        for lane in LANES:
            self.workers.append(Worker(lane, lane))
            if lane in EXPRESS:
                self.workers.append(Worker(f"{lane}-express", lane, EXPRESS[lane]))

    # ---- adding -----------------------------------------------------------------------------------------------------
    def put(self, item: tuple[str, str], priority: int | None = None) -> None:
        kind, key = item
        with self._cv:
            self._waiting.append(Job(kind, key, DEFAULT_PRIORITY.get(kind, SCHEDULED) if priority is None else priority,
                                     next(self._order), self._clock(), lane_of(kind)))
            self._unfinished += 1
            self._cv.notify_all()

    def put_front(self, item: tuple[str, str]) -> None:
        """You asked for it: ahead of everything not asked for, behind what was asked for before it."""
        self.put(item, USER)

    def to_front(self, item: tuple[str, str]) -> bool:
        """A job already waiting, moved up as if put_front now (False if it isn't waiting, or is up there already)."""
        with self._cv:
            job = next((j for j in self._waiting if j.item == item), None)
            if job is None or job.priority == USER:
                return False
            job.priority, job.order = USER, next(self._order)
            self._cv.notify_all()
            return True

    # ---- looking ----------------------------------------------------------------------------------------------------
    def _sorted(self, jobs: list[Job], now: float) -> list[Job]:
        return sorted(jobs, key=lambda j: (j.effective(now), j.order))

    @property
    def queue(self) -> list[tuple[str, str]]:
        with self._cv:
            return [j.item for j in self._sorted(self._waiting, self._clock())]

    def waiting(self) -> list[Job]:
        """Every waiting job (copies), each lane's in the order it will take them, lanes in LANES order."""
        with self._cv:
            now = self._clock()
            out = self._sorted(self._waiting, now)
            out.sort(key=lambda j: LANES.index(j.lane))           # stable: keeps each lane's order
            return [Job(**{**j.__dict__}) for j in out]

    def running(self) -> list[Job]:
        with self._cv:
            return [Job(**{**w.job.__dict__}) for w in self.workers if w.job]

    def has(self, kind: str) -> bool:
        """A job of this kind running or waiting."""
        with self._cv:
            return any(j.kind == kind for j in self._waiting) or any(w.job and w.job.kind == kind for w in self.workers)

    def qsize(self) -> int:
        with self._cv:
            return len(self._waiting)

    # ---- taking -----------------------------------------------------------------------------------------------------
    def _next_for(self, w: Worker, now: float) -> Job | None:
        for j in self._sorted([j for j in self._waiting if j.lane == w.lane], now):
            if resource_of(j.kind, j.key) in self._busy:
                continue
            if w.best is not None and j.effective(now) > w.best:
                continue
            return j
        return None

    def take(self, w: Worker, timeout: float | None = None) -> Job | None:
        """The worker's next job, waiting for one; None if none came within timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while True:
                job = self._next_for(w, self._clock())
                if job:
                    self._waiting.remove(job)
                    self._busy.add(resource_of(job.kind, job.key))
                    job.started, job.worker = self._clock(), w.name
                    w.job = job
                    return job
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return None
                # Aging can make a job eligible for the express worker without a put: look again now and then.
                self._cv.wait(min(left, 60) if left is not None else 60)

    def done(self, w: Worker) -> None:
        with self._cv:
            if w.job:
                self._busy.discard(resource_of(w.job.kind, w.job.key))
            w.job = None
            self._unfinished -= 1
            self._cv.notify_all()

    def join(self) -> None:
        """Until every job put has finished."""
        with self._cv:
            while self._unfinished:
                self._cv.wait()

    def start(self, run: Callable[[Job], None]) -> None:
        """A thread per worker, each running run(job) under the job's priority."""
        for w in self.workers:
            def loop(w=w):
                while True:
                    job = self.take(w)
                    try:
                        with priority(job.priority):
                            run(job)
                    finally:
                        self.done(w)
            w.thread = threading.Thread(target=loop, name=f"jobs-{w.name}", daemon=True)
            w.thread.start()
