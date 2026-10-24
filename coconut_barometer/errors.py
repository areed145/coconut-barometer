"""Errors the app catches: logged, and the unexpected ones reported to GitHub as issues: a request that ends in a 500, a worker job or scheduler
round that fails for a reason that isn't the network, and a thread that dies.

Off until Settings → Connections has a GitHub token (a fine-grained one, for the one repository, with
Issues: read and write). Each error is fingerprinted by its type and the innermost frame of the app's own
code (file and function, not the line, so an unrelated edit doesn't make it new). An open issue with that
fingerprint gets a comment instead of a twin, at most one per COMMENT_EVERY with how many times it was
seen since; close the issue and the next one opens a new one. At most NEW_PER_HOUR new issues an hour.

Reports go out from their own thread, so a slow or unreachable GitHub never holds up a request or a job;
one that fails is dropped (and printed). Everything sent is scrubbed of the API keys and anything that
looks like one, and of the home directory.

There's one per process, `reporter` below, like activity.log. `caught(e, where)` is what an `except` calls:
it logs (a warning for the outside world failing, an error with the traceback for anything else) and reports."""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import queue
import re
import threading
import time
import traceback
from importlib import metadata
from pathlib import Path

import requests

API = "https://api.github.com"
LABEL = "auto-report"
COMMENT_EVERY = 3600        # seconds between comments on one issue
NEW_PER_HOUR = 10
TIMEOUT = 15
PACKAGE = Path(__file__).parent
# Not bugs: the outside world being down, slow or saying no. Those show on the Status page instead.
EXPECTED = (OSError, requests.RequestException)
OUTSIDE = {"anthropic", "openai", "httpx", "flickrapi", "websockets", "aprslib"}   # their errors are theirs
logger = logging.getLogger(__name__)
SECRET_PARAM = re.compile(r"(?i)\b(api_key|api_secret|token|secret|passcode|password|oauth_[a-z_]+|sig|signature)"
                          r"(['\"]?\s*[=:]\s*['\"]?)([^&\s'\",)]+)")
BEARER = re.compile(r"(?i)\b(bearer|token)\s+[a-z0-9_\-.]{12,}")


def expected(exc: BaseException) -> bool:
    from .flickr_client import FlickrCallError
    return (isinstance(exc, (*EXPECTED, FlickrCallError))
            or any(c.__module__.split(".")[0] in OUTSIDE for c in type(exc).__mro__))


def caught(exc: BaseException, where: str) -> None:
    """For an `except` that carries on: log it, and report it unless it's expected()."""
    if expected(exc):
        logger.warning("%s: %s: %s", where, type(exc).__name__, exc)
    else:
        logger.error("%s: %s: %s", where, type(exc).__name__, exc, exc_info=exc)
    reporter.report(exc, where)


def fingerprint(exc: BaseException) -> str:
    """The exception's type and the innermost frame in this package (or the innermost of all, if none is). An
    exception may carry its own (`_coconut_fingerprint`): a browser's error has no Python frames to go by."""
    custom = getattr(exc, "_coconut_fingerprint", None)
    if custom:
        return custom
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if Path(f.filename).resolve().is_relative_to(PACKAGE)]
    f = (ours or frames or [None])[-1]
    where = f"{Path(f.filename).resolve().relative_to(PACKAGE) if ours else Path(f.filename).name}:{f.name}" if f else "?"
    return hashlib.sha1(f"{type(exc).__qualname__}@{where}".encode()).hexdigest()[:12]


def scrub(text: str, secrets=()) -> str:
    for s in secrets:
        if s and len(str(s)) >= 6:
            text = text.replace(str(s), "[redacted]")
    text = SECRET_PARAM.sub(lambda m: f"{m[1]}{m[2]}[redacted]", text)
    text = BEARER.sub(lambda m: f"{m[1]} [redacted]", text)
    return text.replace(str(Path.home()), "~")


def _version() -> str:
    try:
        v = metadata.version("coconut-barometer")
    except metadata.PackageNotFoundError:
        v = "?"
    commit = os.environ.get("RENDER_GIT_COMMIT", "")      # Render sets it
    return f"{v} ({commit[:7]})" if commit else v


class Reporter:
    def __init__(self):
        self.cfg: dict | None = None
        self.lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=100)
        self._thread: threading.Thread | None = None
        self._issues: dict[str, int] = {}          # fingerprint -> open issue number, as found or opened
        self._last_comment: dict[str, float] = {}  # fingerprint -> when it was last commented on (or opened)
        self._unsent: dict[str, int] = {}          # fingerprint -> times seen since then
        self._opened: list[float] = []             # when each new issue was opened, for NEW_PER_HOUR
        self.sent = 0
        self.last_at: float | None = None
        self.last_error: str | None = None
        self.session = requests.Session()

    # ---- settings ------------------------------------------------------------------
    def watch(self, cfg: dict) -> None:
        """Read the GitHub settings from cfg (held, so an admin-page change applies from the next error)."""
        self.cfg = cfg
        _install_thread_hook()

    def _github(self) -> tuple[str, str] | None:
        g = (self.cfg or {}).get("github") or {}
        return (g["token"], g["repo"].strip()) if g.get("token") and (g.get("repo") or "").strip() else None

    def enabled(self) -> bool:
        return self._github() is not None

    def _secrets(self) -> list:
        from .settings import SECRETS, get_path
        return [get_path(self.cfg or {}, p) for p in (*SECRETS, "github.token")]

    # ---- reporting -----------------------------------------------------------------
    def report(self, exc: BaseException, where: str) -> None:
        """Queue exc for GitHub, unless reporting is off or it's expected(). Never raises."""
        try:
            if not self.enabled() or expected(exc):
                return
            fp = fingerprint(exc)
            with self.lock:
                self._unsent[fp] = self._unsent.get(fp, 0) + 1
                if fp in self._last_comment and time.time() - self._last_comment[fp] < COMMENT_EVERY:
                    return                        # counted; said with the next comment
                self._last_comment[fp] = time.time()
            text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self._q.put_nowait((fp, type(exc).__qualname__, str(exc), text, where, time.time()))
            self._start()
        except Exception as e:   # noqa: BLE001 - reporting must never be the next error
            logger.warning("error report not queued: %s", e)

    def _start(self) -> None:
        with self.lock:
            if not self._thread:
                self._thread = threading.Thread(target=self._run, name="error-reports", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                self._send(*item)
                self.sent, self.last_at, self.last_error = self.sent + 1, time.time(), None
            except Exception as e:   # noqa: BLE001 - dropped; the error itself is still in the server's log
                self.last_error = scrub(f"{type(e).__name__}: {e}", self._secrets())
                logger.warning("error report not sent: %s", self.last_error)

    def _call(self, method: str, path: str, **kw) -> requests.Response:
        token, repo = self._github()
        r = self.session.request(method, f"{API}/repos/{repo}{path}", timeout=TIMEOUT, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path.split('?')[0]}: {r.status_code} {r.json().get('message', '') if r.content else ''}")
        return r

    def _find(self, fp: str) -> int | None:
        """The open issue with this fingerprint: remembered, or looked for among the open auto-reports."""
        marker = f"<!-- fingerprint: {fp} -->"
        if fp in self._issues:
            issue = self._call("GET", f"/issues/{self._issues[fp]}").json()
            if issue["state"] == "open":
                return issue["number"]
            del self._issues[fp]
            return None
        for issue in self._call("GET", f"/issues?state=open&labels={LABEL}&per_page=100").json():
            if marker in (issue.get("body") or ""):
                self._issues[fp] = issue["number"]
                return issue["number"]
        return None

    def _send(self, fp, kind, message, text, where, at) -> None:
        secrets = self._secrets()
        text, message = scrub(text, secrets), scrub(message, secrets)
        with self.lock:
            seen, self._unsent[fp] = self._unsent.pop(fp, 1), 0
        when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(at))
        number = self._find(fp)
        if number:
            again = f"Seen again {when}" + (f" ({seen} times since the last comment)" if seen > 1 else "")
            self._call("POST", f"/issues/{number}/comments",
                       json={"body": f"{again}, in `{where}`, version {_version()}.\n\n```\n{text[-6000:]}\n```"})
            return
        now = time.time()
        self._opened = [t for t in self._opened if now - t < 3600]
        if len(self._opened) >= NEW_PER_HOUR:
            raise RuntimeError(f"{NEW_PER_HOUR} issues opened this hour already; not opening another")
        self._opened.append(now)
        first = message.splitlines()[0][:100] if message else ""
        body = (f"Reported by the app itself.\n\n"
                f"- **Where:** `{where}`\n- **When:** {when}\n- **Version:** {_version()}\n"
                f"- **Python:** {platform.python_version()} on {platform.system()}\n\n"
                f"```\n{text[-12000:]}\n```\n\n<!-- fingerprint: {fp} -->")
        issue = self._call("POST", "/issues", json={"title": f"{kind}: {first}" if first else kind,
                                                    "body": body, "labels": [LABEL]}).json()
        self._issues[fp] = issue["number"]

    def status(self) -> dict:
        return {"enabled": self.enabled(), "sent": self.sent, "last_at": self.last_at, "error": self.last_error,
                "repo": (self._github() or (None, None))[1]}


reporter = Reporter()
_hooked = False


def _install_thread_hook() -> None:
    """A thread that dies of an exception is logged (with the traceback) and reported."""
    global _hooked
    if _hooked:
        return
    _hooked = True
    previous = threading.excepthook

    def hook(args):
        if args.exc_value is None or isinstance(args.exc_value, SystemExit):
            return previous(args)
        name = f"thread {args.thread.name if args.thread else '?'}"
        logger.error("%s died", name, exc_info=args.exc_value)
        reporter.report(args.exc_value, name)
    threading.excepthook = hook
