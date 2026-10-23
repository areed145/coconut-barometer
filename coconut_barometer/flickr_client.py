"""Thin, throttled wrapper around the `flickrapi` package.

Everything else in the project talks to Flickr through `FlickrClient.call()`, which
makes it easy to swap in a fake for tests.
"""
from __future__ import annotations

import re
import time
import webbrowser
from typing import Any, Iterator

from . import activity
from .config import data_dir
from .ratelimit import SharedRateLimiter, limiter_path

PHOTO_EXTRAS = ("description,tags,geo,media,date_taken,date_upload,last_update,url_q,url_n,url_m,url_z,"
                "url_l,url_h,url_k,views,count_faves,count_comments,license,o_dims")


REQUEST_TIMEOUT = 60  # seconds per Flickr API request


class FlickrCallError(Exception):
    def __init__(self, method: str, code: int | None, message: str):
        super().__init__(f"{method}: [{code}] {message}")
        self.method, self.code, self.message = method, code, message


DETAIL_PARAMS = ("photo_id", "photoset_id", "group_id", "date", "page", "min_date", "extras_count")


def _detail(params: dict) -> str:
    """What a call was about, for the Status page's log (ids and pages, never anything else)."""
    return " ".join(f"{k}={params[k]}" for k in DETAIL_PARAMS if k in params)


def _error_code(exc: Exception) -> int | None:
    code = getattr(exc, "code", None)
    if code is not None:
        try:
            return int(code)
        except (TypeError, ValueError):
            pass
    m = re.search(r"Error:\s*(\d+)", str(exc))
    return int(m.group(1)) if m else None


class FlickrClient:
    def __init__(self, api_key: str, api_secret: str, calls_per_second: float = 1.0, limiter=None):
        """limiter: defaults to the rate limit shared by every process using this API key.
        With DATA_DIR set (a server), the Flickr login and the rate-limit file
        live there instead of ~/.flickr, so they survive redeploys."""
        if not api_key or not api_secret:
            raise SystemExit(
                "Missing Flickr API key/secret. Get one at https://www.flickr.com/services/apps/create/ "
                "and set flickr.api_key / flickr.api_secret in config.yaml."
            )
        import flickrapi  # lazy so tests don't need it

        self._flickrapi = flickrapi
        # flickrapi has no timeout by default, so one connection that goes quiet blocks a
        # long fetch forever; with one, the retry loop in call() takes over.
        data = data_dir()
        token_dir = str(data / "flickr-auth") if data else None
        self.api = flickrapi.FlickrAPI(api_key, api_secret, format="parsed-json", store_token=True,
                                       timeout=REQUEST_TIMEOUT, token_cache_location=token_dir)
        self.limiter = limiter or SharedRateLimiter(
            limiter_path(api_key, str(data) if data else "~/.flickr"), calls_per_second)
        self.call_count = 0   # requests made, retries included (for "used N calls" reports)
        self._nsid: str | None = None

    # ---- auth -------------------------------------------------------------
    def authenticate(self, perms: str = "write") -> None:
        """OAuth 1.0a out-of-band flow. The token is cached in ~/.flickr/."""
        if self.api.token_valid(perms=perms):
            return
        self.api.get_request_token(oauth_callback="oob")
        url = self.api.auth_url(perms=perms)
        print(f"\nOpen this URL, approve access, and paste the code Flickr shows you:\n\n  {url}\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        verifier = input("Verifier code: ").strip()
        self.api.get_access_token(verifier)

    def nsid(self) -> str:
        if not self._nsid:
            self._nsid = self.call("flickr.test.login")["user"]["id"]
        return self._nsid

    # ---- calls ------------------------------------------------------------
    def call(self, method: str, expect: tuple[int, ...] = (), **params: Any) -> dict:
        """expect: error codes that are answers rather than failures (groups.pools.add's "waiting for
        a moderator"): still raised, but not counted as errors in the activity log."""
        fn = self.api
        for part in method.split(".")[1:]:
            fn = getattr(fn, part)
        delay, log = 2.0, activity.log
        for attempt in range(5):
            token = log.begin("flickr", method, _detail(params) + (f" (try {attempt + 1})" if attempt else ""))
            self.limiter.wait(on_slot=lambda slot: log.until(token, slot))
            log.running(token)
            self.call_count += 1
            try:
                out = fn(**params)
                log.end(token)
                return out
            except self._flickrapi.exceptions.FlickrError as e:
                code = _error_code(e)
                # 105 = service unavailable; anything else is a real API error.
                retry = code == 105 and attempt < 4
                if code in expect:
                    log.end(token, f"answer {code}", str(e)[:300])
                    raise FlickrCallError(method, code, str(e)) from e
                log.end(token, "retrying" if retry else f"error {code}", str(e)[:300], error=True)
                if retry:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise FlickrCallError(method, code, str(e)) from e
            except Exception as e:  # network hiccups, 5xx
                log.end(token, "retrying" if attempt < 4 else "error", f"{type(e).__name__}: {e}"[:300], error=True)
                if attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise FlickrCallError(method, None, str(e)) from e
        raise AssertionError("unreachable")


def paginate(client, method: str, container: str, item: str, **params) -> Iterator[dict]:
    """Yield every item across all pages of a paged Flickr response."""
    page = 1
    while True:
        resp = client.call(method, page=page, per_page=500, **params)[container]
        for x in resp.get(item, []):
            yield x
        if page >= int(resp.get("pages", 1) or 1):
            return
        page += 1
