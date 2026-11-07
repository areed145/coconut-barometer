"""The process's memory (#197): watched against the container's limit, reported to GitHub before it gets there, and
kept steadier by doing the big pieces of work one at a time.

Render kills the service when it goes over its plan's memory (2 GB), with no chance to say why. So a watchdog
thread samples the resident size every SAMPLE_EVERY seconds against the limit (the container's cgroup limit, or
MEMORY_LIMIT_MB):

  WATCH (60%)  tracemalloc starts, so what's holding memory can be named (it sees only what's allocated after it
               starts, numpy arrays included; it stops again under 45%). From here a summary is saved to the data
               folder every SUMMARY_EVERY seconds, so if the process is killed the next start can still report what
               it looked like.
  SOFT  (80%)  memory is given back first (gc, and glibc's malloc_trim); still over, a report goes to GitHub through
               errors.reporter: the biggest allocations by line, the threads, the job running and what else
               registered a describe(). One issue, a comment at most every REPORT_EVERY while it stays high.
  HARD  (90%)  the Status page's row turns red.

A start that finds a summary saved at SOFT or above, with no clean stop since, reports that the app restarted after
memory got that high (it was probably killed for it).

heavy(): the decodes that briefly take 100 MB or more (a nationwide MRMS radar frame, a rain total) run one at a
time across the process instead of piling up, and memory is given back after each while it's above WATCH. With
MALLOC_ARENA_MAX (the Dockerfile), glibc keeps fewer per-thread heaps, so what's freed can go back to the system.

There's one watchdog per process, `watchdog` below. Off the Linux /proc filesystem (a Mac), the resident size isn't
read and the watchdog does nothing."""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import gc
import json
import logging
import os
import sys
import threading
import time
import tracemalloc
from typing import Callable

logger = logging.getLogger(__name__)

WATCH, SOFT, HARD = 0.60, 0.80, 0.90     # of the limit
SAMPLE_EVERY = 15                         # seconds
SUMMARY_EVERY = 120                       # seconds between summaries (a tracemalloc snapshot costs memory itself)
REPORT_EVERY = 3600                       # seconds between reports while it stays high (errors.COMMENT_EVERY)
UNTRACE = 0.45                            # tracing stops again once back under this
TOP = 25                                  # allocation lines in a report
NO_LIMIT = 1 << 60                        # cgroup v1 says "no limit" with a huge number
STATE_FILE = "memory-last.json"

_heavy = threading.Lock()
_heavy_what: list[str] = []               # what holds heavy() now, for a report


# ---- reading ---------------------------------------------------------------------------------------------------
def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def limit_bytes(environ=os.environ, read=_read) -> int | None:
    """MEMORY_LIMIT_MB, else the container's cgroup limit (v2, then v1), else None."""
    mb = (environ.get("MEMORY_LIMIT_MB") or "").strip()
    if mb.isdigit() and int(mb) > 0:
        return int(mb) * 1024 * 1024
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        text = read(path)
        if text and text.isdigit() and 0 < int(text) < NO_LIMIT:
            return int(text)
    return None


def rss_bytes(read=_read) -> int | None:
    """The resident size now (Linux /proc), else None."""
    text = read("/proc/self/statm")
    if not text:
        return None
    try:
        return int(text.split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (IndexError, ValueError, OSError):
        return None


def mb(n: int | float | None) -> str:
    return "?" if n is None else f"{n / 1024 / 1024:,.0f} MB"


# ---- giving memory back ------------------------------------------------------------------------------------------
_libc = None


def _malloc_trim() -> bool:
    """glibc's malloc_trim(0): free pages at the top and inside its heaps go back to the system. False off glibc."""
    global _libc
    if not sys.platform.startswith("linux"):
        return False
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        return bool(_libc.malloc_trim(0))
    except (OSError, AttributeError):
        return False


def relieve() -> None:
    """Collect the cycles and hand freed memory back to the system."""
    gc.collect()
    _malloc_trim()


@contextlib.contextmanager
def heavy(what: str):
    """One big piece of work at a time across the process (see the module's docstring). Memory is given back after it
    while the watchdog says it's above WATCH."""
    with _heavy:
        _heavy_what.append(what)
        try:
            yield
        finally:
            _heavy_what.pop()
            if watchdog.level() != "ok":
                relieve()


# ---- the watchdog --------------------------------------------------------------------------------------------------
class MemoryPressure(Exception):
    """What a memory report is sent as: errors.reporter files it under its own fingerprint, the message is the report."""

    def __init__(self, message: str, fingerprint: str):
        super().__init__(message)
        self._coconut_fingerprint = fingerprint


class Watchdog:
    def __init__(self, rss: Callable[[], int | None] = rss_bytes, limit: Callable[[], int | None] = limit_bytes,
                 clock=time.time, report=None, relieve_fn=relieve):
        self._rss, self._limit_fn, self._clock = rss, limit, clock
        self._report = report                 # (exc, where) -> None; errors.reporter.report unless given
        self._relieve = relieve_fn
        self.limit: int | None = None
        self.rss: int | None = None
        self.peak: int | None = None
        self.peak_at: float | None = None
        self.reported_at: float | None = None
        self.state_path: str | None = None
        self._summary_at = 0.0
        self._describe: dict[str, Callable[[], str]] = {}
        self._thread: threading.Thread | None = None
        self.lock = threading.Lock()

    # ---- set-up ---------------------------------------------------------------------------------------------------
    def describe(self, name: str, fn: Callable[[], str]) -> None:
        """A line for reports: what `name` holds now (a queue's length, a cache's size). Must be quick and not raise."""
        self._describe[name] = fn

    def start(self, stop: threading.Event, data_dir: str | None = None) -> None:
        """Report a restart that followed high memory, then sample until stop is set."""
        if self._thread:
            return
        self.limit = self._limit_fn()
        self.state_path = os.path.join(data_dir, STATE_FILE) if data_dir else None
        if self._rss() is None:
            logger.info("memory: the resident size can't be read here; not watched")
            return
        logger.info("memory: watching against a limit of %s", mb(self.limit) if self.limit else "none found")
        self.check_restart()

        def run():
            while not stop.is_set():
                try:
                    self.sample()
                except Exception as e:   # noqa: BLE001 - the watchdog must outlive its own mistakes
                    logger.warning("memory sample failed: %s", e)
                stop.wait(SAMPLE_EVERY)
        self._thread = threading.Thread(target=run, name="memory", daemon=True)
        self._thread.start()

    def stopped(self) -> None:
        """A clean stop: nothing to report at the next start."""
        if self.state_path:
            with contextlib.suppress(OSError):
                os.remove(self.state_path)

    # ---- sampling -------------------------------------------------------------------------------------------------
    def fraction(self) -> float | None:
        return self.rss / self.limit if self.rss and self.limit else None

    def level(self) -> str:
        f = self.fraction()
        return ("ok" if f is None or f < WATCH else "watch" if f < SOFT else "soft" if f < HARD else "hard")

    def sample(self) -> str:
        """One look: start tracing, give memory back, save and report as the levels say. Returns the level."""
        self.rss = self._rss()
        if self.rss is None:
            return "ok"
        now = self._clock()
        if self.peak is None or self.rss > self.peak:
            self.peak, self.peak_at = self.rss, now
        level = self.level()
        if level == "ok":
            if tracemalloc.is_tracing() and (self.fraction() or 0) < UNTRACE:
                tracemalloc.stop()
                logger.info("memory back to %s: tracing stopped", mb(self.rss))
            return level
        if not tracemalloc.is_tracing():
            tracemalloc.start(1)
            logger.warning("memory at %s of %s: tracing allocations", mb(self.rss), mb(self.limit))
        if level in ("soft", "hard"):
            self._relieve()
            self.rss = self._rss() or self.rss
            level = self.level()
        due = now - self._summary_at >= SUMMARY_EVERY
        if level in ("soft", "hard") and (self.reported_at is None or now - self.reported_at >= REPORT_EVERY):
            self._send(f"Memory at {mb(self.rss)} of the {mb(self.limit)} limit ({self.fraction():.0%})",
                       "memory-pressure", self.summary())
        elif due:
            self._save(self.summary())
        return level

    def summary(self) -> dict:
        """What's holding memory now, as plainly as can be had."""
        top = []
        if tracemalloc.is_tracing():
            stats = tracemalloc.take_snapshot().filter_traces((
                tracemalloc.Filter(False, tracemalloc.__file__), tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
            )).statistics("lineno")
            for s in stats[:TOP]:
                f = s.traceback[0]
                top.append({"where": f"{_short(f.filename)}:{f.lineno}", "size": s.size, "count": s.count})
            traced, traced_peak = tracemalloc.get_traced_memory()
        else:
            traced = traced_peak = None
        extra = {}
        for name, fn in list(self._describe.items()):
            try:
                extra[name] = str(fn())
            except Exception as e:   # noqa: BLE001
                extra[name] = f"? ({type(e).__name__})"
        return {"at": self._clock(), "rss": self.rss, "limit": self.limit, "peak": self.peak,
                "traced": traced, "traced_peak": traced_peak, "top": top, "heavy": list(_heavy_what),
                "threads": sorted(t.name for t in threading.enumerate()), "gc": gc.get_count(), "extra": extra}

    # ---- saving and reporting ---------------------------------------------------------------------------------------
    def _save(self, summary: dict) -> None:
        self._summary_at = self._clock()
        if not self.state_path:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(summary, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logger.warning("memory summary not saved: %s", e)

    def _send(self, title: str, fingerprint: str, summary: dict) -> None:
        self._save(summary)
        self.reported_at = self._clock()
        logger.warning("%s", title)
        report = self._report
        if report is None:
            from .errors import reporter
            report = reporter.report
        report(MemoryPressure(f"{title}\n\n{format_summary(summary)}", fingerprint), "memory watchdog")

    def check_restart(self) -> bool:
        """A summary left by a process that didn't stop cleanly, at SOFT or above: report it. True if reported."""
        if not self.state_path:
            return False
        try:
            with open(self.state_path) as f:
                last = json.load(f)
        except (OSError, ValueError):
            return False
        finally:
            with contextlib.suppress(OSError):
                os.remove(self.state_path)
        limit = last.get("limit") or self.limit
        if not limit or not last.get("rss") or last["rss"] / limit < SOFT:
            return False
        when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(last.get("at") or 0))
        title = f"Restarted after memory reached {mb(last['rss'])} of {mb(limit)} (last seen {when})"
        logger.warning("%s", title)
        report = self._report
        if report is None:
            from .errors import reporter
            report = reporter.report
        report(MemoryPressure(f"{title}\n\nThe process stopped without a clean shutdown, probably killed for going over "
                              f"the limit. What it held at its last look:\n\n{format_summary(last)}", "memory-restart"),
               "memory watchdog")
        return True

    def status(self) -> dict:
        return {"rss": self.rss, "limit": self.limit, "peak": self.peak, "peak_at": self.peak_at,
                "level": self.level(), "reported_at": self.reported_at, "watched": self._thread is not None}


def _short(path: str) -> str:
    """A file's path from its package (coconut_barometer/..., numpy/...), not the machine's."""
    parts = path.replace("\\", "/").split("/")
    for marker in ("site-packages", "lib"):
        if marker in parts:
            i = len(parts) - 1 - parts[::-1].index(marker)
            return "/".join(parts[i + 1:]) or path
    if "coconut_barometer" in parts:
        return "/".join(parts[parts.index("coconut_barometer"):])
    return parts[-1]


def format_summary(s: dict) -> str:
    """Plain text: errors.reporter puts it in a code block."""
    lines = [f"Resident: {mb(s.get('rss'))} of {mb(s.get('limit'))} (peak {mb(s.get('peak'))})"]
    if s.get("traced") is not None:
        lines.append(f"Traced since tracing started: {mb(s['traced'])} (peak {mb(s.get('traced_peak'))})")
    if s.get("heavy"):
        lines.append(f"Heavy work running: {', '.join(s['heavy'])}")
    for name, text in (s.get("extra") or {}).items():
        lines.append(f"{name}: {text}")
    lines.append(f"Threads ({len(s.get('threads') or [])}): {', '.join(s.get('threads') or [])}")
    if s.get("top"):
        lines += ["", "Biggest allocations since tracing started, by line:"]
        lines += [f"{mb(t['size']):>10}  {t['count']:>10,}  {t['where']}" for t in s["top"]]
    else:
        lines += ["", f"No allocations traced (tracing starts at {WATCH:.0%} of the limit)."]
    return "\n".join(lines)


watchdog = Watchdog()
