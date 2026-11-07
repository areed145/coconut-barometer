"""Apple Music listening history: what was played, read from Apple every apple_music.every_minutes and kept here, for
the Music page (#/music). Setup is in docs/APPLE_MUSIC.md.

Two tokens reach Apple's API. The developer token is a short JWT this app signs with the MusicKit key from the Apple
Developer account (apple_music.team_id, key_id, private_key). The Music User Token says your account agrees: only
Apple's sign-in can issue it (the page at /api/music/connect), it's kept in apple_music.user_token, and Apple ends it
after months, at which point a read answers 401 or 403 and the Status page asks you to connect again.

What the API can't tell us shapes the rest. GET /v1/me/recent/played/tracks is just the latest tracks (about 130 in the
five pages read), newest first, with no times and with repeats often collapsed, so:
  - a new play is one that moved to, or appeared at, the top since the last read (new_count)
  - the time is an estimate: the new plays are stacked back from this read by their lengths, never earlier than the
    last read (play_times); window_start keeps how far off it could be
  - minutes assume each track played to its end
  - the first read only takes the list as a starting point: those plays have no time to give them
  - more new plays than the list holds between two reads are lost (counted in music_missed)
Plays the API leaves out never arrive: developers report library and own-playlist plays missing for some accounts.

Apple's data export fills in the time before the first read, with exact times and what was really heard
(apple_export.py). It has no genres, so its tracks are looked up in Apple's catalog, a batch a minute (enrich)."""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import textwrap
import time
from collections import Counter
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from . import activity, music_insights as I
from .errors import caught

logger = logging.getLogger(__name__)

API = "https://api.music.apple.com"
RECENT = "/v1/me/recent/played/tracks?limit=10"     # 10 a page is the size every report agrees works
CATALOG = "/v1/catalog/us/songs"                    # needs only the developer token
CATALOG_BATCH = 300                                 # Apple's most ids in a request
MAX_PAGES = 5
TIMEOUT = 30
TOKEN_SECONDS = 3600
BROWSER_TOKEN_SECONDS = 600                         # the one the connect page sees
CHECK_EVERY = 60
RETRIES = (2, 8)                                    # seconds before asking for a page again when Apple's servers fail
RETRY_READ_SECONDS = 300                            # a read that still failed: tried again this soon, not next hour
KEEP_IDS = 200                                      # the order remembered from one read to the next
TOP = 15
RECENT_ROWS = 20
ALL = 3650                                          # "All" on the Range buttons


class MusicError(RuntimeError):
    """Apple's answer, or our settings, kept a read from working. The message is safe to show."""


class NotAuthorized(MusicError):
    """Apple refused the tokens: the fix is in Settings or connecting again, not waiting."""


def _pem(text: str) -> bytes:
    """The key as PEM, whatever pasting did to it (a one-line field drops the line breaks)."""
    m = re.search(r"-----BEGIN PRIVATE KEY-----(.*?)-----END PRIVATE KEY-----", text, re.S)
    body = re.sub(r"\s", "", m.group(1) if m else text)
    return ("-----BEGIN PRIVATE KEY-----\n" + "\n".join(textwrap.wrap(body, 64)) + "\n-----END PRIVATE KEY-----\n").encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def keys_set(cfg: dict) -> bool:
    am = cfg["apple_music"]
    return bool(am["team_id"] and am["key_id"] and am["private_key"])


def connected(cfg: dict) -> bool:
    return keys_set(cfg) and bool(cfg["apple_music"]["user_token"])


def developer_token(cfg: dict, seconds: int = TOKEN_SECONDS, now: float | None = None) -> str:
    """An ES256 JWT, which is what Apple accepts (its signature is the two numbers side by side, not DER)."""
    am = cfg["apple_music"]
    if not keys_set(cfg):
        raise MusicError("set the Team ID, Key ID and private key first")
    try:
        key = serialization.load_pem_private_key(_pem(am["private_key"]), password=None)
    except (ValueError, TypeError):
        raise MusicError("the private key can't be read: paste the whole .p8 file") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise MusicError("that isn't a MusicKit key: Apple's .p8 files are P-256")
    now = int(now if now is not None else time.time())
    head = _b64(json.dumps({"alg": "ES256", "kid": am["key_id"].strip()}).encode())
    body = _b64(json.dumps({"iss": am["team_id"].strip(), "iat": now, "exp": now + seconds}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def fetch_recent(cfg: dict, user_token: str | None = None, get=requests.get, sleep=time.sleep) -> list[dict]:
    """The recently played list, newest first, every page of it or an error: part of a list isn't kept. Apple's API
    has passing 5xx and 429 answers, so a page is asked for again before giving up. user_token: try this one instead
    of the saved one."""
    headers = {"Authorization": f"Bearer {developer_token(cfg)}",
               "Music-User-Token": user_token or cfg["apple_music"]["user_token"]}
    items: list[dict] = []
    path = RECENT
    for _ in range(MAX_PAGES):
        for wait in (*RETRIES, None):
            with activity.log.call("apple-music", "recently played", f"{len(items)} so far") as call:
                r = get(API + path, headers=headers, timeout=TIMEOUT)
                call["status"] = str(r.status_code)
                call["error"] = r.status_code != 200
            if wait is None or not (r.status_code == 429 or r.status_code >= 500):
                break
            sleep(wait)
        if r.status_code in (401, 403):
            raise NotAuthorized("Apple refused the connection (it may have expired, or the account has no "
                                "Apple Music subscription): connect again")
        if r.status_code != 200:
            raise MusicError(f"Apple Music's servers answered {r.status_code}. It tries again in a few minutes.")
        body = r.json()
        items += body.get("data") or []
        path = body.get("next")
        if not (isinstance(path, str) and path.startswith("/v1/")):      # the tokens only ever go to Apple's API
            break
    return items


def fetch_catalog(cfg: dict, ids: list[str], get=requests.get) -> list[dict]:
    with activity.log.call("apple-music", "catalog", f"{len(ids)} tracks") as call:
        r = get(API + CATALOG, params={"ids": ",".join(ids)}, headers={"Authorization": f"Bearer {developer_token(cfg)}"},
                timeout=TIMEOUT)
        call["status"] = str(r.status_code)
        call["error"] = r.status_code != 200
    if r.status_code != 200:
        raise MusicError(f"Apple Music's catalog answered {r.status_code}")
    return r.json().get("data") or []


def _subsequence(short: list, long: list) -> bool:
    rest = iter(long)
    return all(x in rest for x in short)


def new_count(before: list[str], after: list[str]) -> int:
    """How many at the top of `after` are new plays. What's below them is the old list, in its old order, with gaps
    where tracks moved up, so it's the fewest from the top that leave the rest in the order `before` had. Unknown
    tracks below the last known one are older ones a longer list reached, not plays."""
    known = set(before)
    end = next((i for i in range(len(after), 0, -1) if after[i - 1] in known), len(after))
    return next(k for k in range(end + 1) if _subsequence(after[k:end], before))


def play_times(durations: list[float], start: float, now: float) -> list[int]:
    """When each play (newest first, lengths in seconds) probably began: back to back, ending now."""
    out, end = [], now
    for d in durations:
        end = max(start, end - d)
        out.append(int(end))
    return out


def _track(item: dict) -> dict:
    a = item.get("attributes") or {}
    return {"id": item["id"], "kind": item.get("type"), "name": a.get("name") or "Unknown", "artist": a.get("artistName") or "Unknown",
            "album": a.get("albumName") or "", "genres": json.dumps([g for g in a.get("genreNames") or [] if g != "Music"]),
            "duration_ms": int(a.get("durationInMillis") or 0), "release_date": a.get("releaseDate"), "isrc": a.get("isrc"),
            "artwork": (a.get("artwork") or {}).get("url"), "url": a.get("url"),
            "bg_color": (a.get("artwork") or {}).get("bgColor"), "text_color": (a.get("artwork") or {}).get("textColor1")}


class Music:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def poll(self, store, now: float | None = None, get=requests.get) -> int:
        """One read of the list: stores what's new and returns how many plays that was."""
        now = int(now if now is not None else time.time())
        items = [i for i in fetch_recent(self.cfg, get=get) if i.get("id")]
        ids = [i["id"] for i in items]
        raw = store.get_meta("music_snapshot")
        tracks = [_track(i) for i in items]
        if raw is None:                                # the starting point: no times to give these
            store.music_save(tracks, [], json.dumps(ids), now, 0)
            logger.info("Apple Music: %d tracks as the starting point", len(ids))
            return 0
        before = json.loads(raw)
        start = int(store.get_meta("music_last_ok") or now)
        count = new_count(before, ids)
        fresh, moved = tracks[:count], set(ids[:count]) - set(ids[count:])
        # The old order, less what moved up, so a track the API leaves out once isn't new when it comes back.
        remembered = (ids[:count] + [i for i in before if i not in moved])[:KEEP_IDS]
        times = play_times([t["duration_ms"] / 1000 for t in fresh], start, now)
        plays = [(t["id"], at, now, start) for t, at in zip(fresh, times)]
        overflow = 1 if before and len(fresh) == len(ids) >= MAX_PAGES * 10 else 0   # the whole list is new: some fell off it
        store.music_save(tracks, plays, json.dumps(remembered), now, overflow)
        return len(plays)

    def due(self, store, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        every = self.cfg["apple_music"]["every_minutes"] * 60
        return connected(self.cfg) and now - int(store.get_meta("music_last_poll") or 0) >= every

    def run_once(self, store, now: float | None = None, get=requests.get) -> dict:
        """One read, as a job on the review worker (the Status page's Jobs): what it found, or why it couldn't."""
        now = int(now if now is not None else time.time())
        store.set_meta("music_last_poll", now)
        try:
            rows = self.poll(store, now, get)
            store.set_meta("music_error", "")
            return {"status": "done", "rows": rows}
        except NotAuthorized as e:
            store.set_meta("music_error", f"auth:{e}")
            return {"status": "failed", "error": str(e)}
        except (MusicError, requests.RequestException, ValueError) as e:
            logger.warning("Apple Music: %s", e)
            store.set_meta("music_error", f"read:{e}"[:300])
            every = self.cfg["apple_music"]["every_minutes"] * 60
            store.set_meta("music_last_poll", now - every + min(every, RETRY_READ_SECONDS))
            return {"status": "failed", "error": str(e)}

    def enrich(self, store, now: float | None = None, get=requests.get) -> int:
        """One batch of imported tracks from the catalog; returns how many were asked about."""
        ids = store.music_to_enrich(CATALOG_BATCH) if keys_set(self.cfg) else []
        if not ids:
            return 0
        try:
            found = [_track(i) for i in fetch_catalog(self.cfg, ids, get) if i.get("id") in set(ids)]
        except (MusicError, requests.RequestException, ValueError) as e:
            logger.warning("Apple Music catalog: %s", e)
            return 0
        store.music_enrich(ids, found, int(now if now is not None else time.time()))
        return len(ids)

    def run(self, stop, store, queue) -> None:
        """Every minute: queue(), which puts a read on the worker, if one's due, and a batch for the catalog."""
        while not stop.wait(CHECK_EVERY):
            try:
                if self.due(store()):
                    queue()
                self.enrich(store())
            except Exception as e:
                caught(e, "apple music")

    def state(self, store) -> dict:
        """For Settings, the Music page and Status."""
        kind, _, error = (store.get_meta("music_error") or "").partition(":")
        return {"keys_set": keys_set(self.cfg), "connected": connected(self.cfg), "every_minutes": self.cfg["apple_music"]["every_minutes"],
                "last_ok": int(store.get_meta("music_last_ok") or 0) or None, "error": error or None, "reconnect": kind == "auth",
                "started": store.get_meta("music_snapshot") is not None, "missed": int(store.get_meta("music_missed") or 0),
                "imported": store.music_imported()}

    def report(self, store, days: int, tz: str = "UTC", now: float | None = None) -> dict:
        """The Music page. Worked out here, not in SQL: the days and hours are in the viewer's timezone, and a year
        of listening is well under a million rows."""
        now = int(now if now is not None else time.time())
        zone = _zone(tz)
        since = 0 if days >= ALL else now - days * 86400
        rows = store.music_plays(0 if days >= ALL else since - days * 86400)
        current = [r for r in rows if r["played_at"] >= since]
        earlier = [r for r in rows if r["played_at"] < since]
        by_day: Counter = Counter()
        minutes: Counter = Counter()
        heat = [[0] * 24 for _ in range(7)]
        genres: Counter = Counter()
        for r in current:
            local = dt.datetime.fromtimestamp(r["played_at"], zone)
            day, mins = local.date().isoformat(), r["ms"] / 60000
            by_day[day] += 1
            minutes[day] += mins
            heat[local.weekday()][local.hour] += 1
            genres.update(json.loads(r["genres"] or "[]"))
        first = min((r["played_at"] for r in current), default=now)
        start = dt.datetime.fromtimestamp(first if days >= ALL else since, zone).date()
        today = dt.datetime.fromtimestamp(now, zone).date()
        series = [(start + dt.timedelta(n)).isoformat() for n in range((today - start).days + 1)]
        return {"state": self.state(store), "days": days,
                "totals": _totals(current), "previous": _totals(earlier) if days < ALL and earlier else None,
                "series": [{"day": d, "plays": by_day[d], "minutes": round(minutes[d])} for d in series], "heat": heat,
                "artists": I.top(current, lambda r: r["artist"], TOP), "tracks": I.top(current, I.track_key, TOP),
                "albums": I.top(current, I.album_key, TOP), "genres": [{"label": k, "plays": n} for k, n in genres.most_common(TOP)],
                "wall": I.wall(current), "strip": I.strip(current, zone, days), "palettes": I.palettes(current, zone),
                "recent": [{"art": r["track_id"] if r["has_art"] else None, "bg": r["bg_color"],
                            **{k: r[k] for k in ("name", "artist", "album", "played_at", "window_start", "seen_at", "url")}}
                           for r in sorted(current, key=lambda r: (r["seen_at"], r["played_at"]), reverse=True)[:RECENT_ROWS]]}


def trip_soundtrack(store, first: str, last: str, tz: str) -> dict:
    """The plays between two days, in the viewer's timezone (photos carry a local time but not its zone)."""
    zone = _zone(tz)
    start = dt.datetime.fromisoformat(first).replace(tzinfo=zone)
    end = dt.datetime.fromisoformat(last).replace(tzinfo=zone) + dt.timedelta(days=1)
    return I.soundtrack(store.music_between(int(start.timestamp()), int(end.timestamp())))


def rain_report(store, days: int, tz: str, now: float | None = None) -> dict:
    now = int(now if now is not None else time.time())
    since = 0 if days >= ALL else now - days * 86400
    return I.weather(store.music_plays(since), *store.music_rain(since), _zone(tz))


def _totals(rows: list[dict]) -> dict:
    return {"plays": len(rows), "minutes": round(sum(r["ms"] for r in rows) / 60000),
            "tracks": len({(r["name"], r["artist"]) for r in rows}), "artists": len({r["artist"] for r in rows if r["artist"]})}


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):            # includes ZoneInfoNotFoundError; the name comes from the browser
        return dt.timezone.utc
