"""Thin, throttled wrapper around the `flickrapi` package.

Everything else in the project talks to Flickr through `FlickrClient.call()`, which
makes it easy to swap in a fake for tests.
"""
from __future__ import annotations

import re
import time
import webbrowser
from typing import Any, Iterator

from .ratelimit import SharedRateLimiter, limiter_path

PHOTO_EXTRAS = ("description,tags,geo,media,date_taken,date_upload,last_update,url_q,url_n,url_m,url_z,"
                "views,count_faves,count_comments,license,o_dims")


REQUEST_TIMEOUT = 60  # seconds per Flickr API request


class FlickrCallError(Exception):
    def __init__(self, method: str, code: int | None, message: str):
        super().__init__(f"{method}: [{code}] {message}")
        self.method, self.code, self.message = method, code, message


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
        """limiter: defaults to the rate limit shared by every process using this API key."""
        if not api_key or not api_secret:
            raise SystemExit(
                "Missing Flickr API key/secret. Get one at https://www.flickr.com/services/apps/create/ "
                "and set flickr.api_key / flickr.api_secret in config.yaml."
            )
        import flickrapi  # lazy so tests don't need it

        self._flickrapi = flickrapi
        # flickrapi has no timeout by default, so one connection that goes quiet blocks a
        # long fetch forever; with one, the retry loop in call() takes over.
        self.api = flickrapi.FlickrAPI(api_key, api_secret, format="parsed-json", store_token=True,
                                       timeout=REQUEST_TIMEOUT)
        self.limiter = limiter or SharedRateLimiter(limiter_path(api_key), calls_per_second)
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
    def call(self, method: str, **params: Any) -> dict:
        fn = self.api
        for part in method.split(".")[1:]:
            fn = getattr(fn, part)
        delay = 2.0
        for attempt in range(5):
            self.limiter.wait()
            try:
                return fn(**params)
            except self._flickrapi.exceptions.FlickrError as e:
                code = _error_code(e)
                # 105 = service unavailable; anything else is a real API error.
                if code == 105 and attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise FlickrCallError(method, code, str(e)) from e
            except Exception as e:  # network hiccups, 5xx
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
