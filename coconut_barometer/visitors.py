"""Who visits the site (#83): page views counted by the page itself, kept in this database, and shown signed in
on the Visitors page (#/visitors).

The app's routes come after the # (#/photos, #/weather/history), so the server never sees them change: main.js
sends a small POST to /api/seen each time the route does (navigator.sendBeacon). Hits are held here and written
every FLUSH_SECONDS, so a page view never waits on the database's write lock while a sync holds it.

No cookies for visitors, and no IP is stored. A visitor is sha256(salt + IP + user agent), with a random salt
that's kept only in memory and replaced at midnight UTC. That counts people per day and groups their views into
sessions (a SESSION_GAP pause starts a new one), but can't follow anyone from one day to the next. The IP's city,
region and country are looked up as the hit arrives (geoip.py), and only those are kept.

Not counted: the owner (a session cookie, or the OWNER_COOKIE that signing in leaves on the device and that
stays after signing out), requests with Sec-GPC or DNT when visitors.honor_gpc is on, and posts from other
sites. Bots (by user agent) are kept as kind 'bot', so the page can say how many were left out, and link
previews of /p/ and /a/ (iMessage, Slack, Discord...) as kind 'preview'."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
import threading
import time
from collections import Counter, deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .errors import caught
from .geoip import GeoIP

FLUSH_SECONDS = 10
SESSION_GAP = 30 * 60
PER_IP_HOUR = 300             # hits an address may send in an hour
PER_VISITOR_DAY = 1000        # rows kept for one visitor in a day
BUFFER_MAX = 20000            # held if the database can't be written for a while; past that, dropped
OWNER_COOKIE = "cb_owner"

PREVIEWS = re.compile(r"facebookexternalhit|facebot|twitterbot|slackbot|discordbot|whatsapp|telegrambot|linkedinbot|"
                      r"skypeuripreview|redditbot|mastodon|bluesky|embedly|pinterest|iframely|vkshare", re.I)
BOTS = re.compile(r"bot\b|bot/|crawl|spider|slurp|scrap|headless|lighthouse|pagespeed|gtmetrix|curl|wget|python|httpx|"
                  r"aiohttp|go-http|java/|okhttp|axios|node-fetch|phantom|puppeteer|playwright|selenium|uptime|monitor|"
                  r"pingdom|preview|fetch|^$", re.I)
OSES = [(r"iPhone|iPad|iPod", "iOS"), (r"Android", "Android"), (r"Windows", "Windows"), (r"CrOS", "ChromeOS"),
        (r"Macintosh|Mac OS X", "macOS"), (r"Linux", "Linux")]
BROWSERS = [(r"Edg(e|A|iOS)?/", "Edge"), (r"OPR/|Opera", "Opera"), (r"SamsungBrowser", "Samsung Internet"),
            (r"Firefox|FxiOS", "Firefox"), (r"CriOS|Chrome/", "Chrome"), (r"Safari/", "Safari")]
SEGMENT = re.compile(r"[a-z][a-z0-9_-]{0,30}")
COLUMNS = ("ts", "day", "visitor", "session", "kind", "route", "item", "ref_host", "utm_source", "utm_medium",
           "utm_campaign", "city", "region", "country", "country_code", "device", "browser", "os", "width")


def parse_ua(ua: str) -> dict:
    """{device: phone | tablet | desktop, browser, os}: the only parts of the user agent kept."""
    ua = ua or ""
    device = ("tablet" if re.search(r"iPad|Tablet|Android(?!.*Mobi)", ua) else
              "phone" if re.search(r"Mobi|iPhone|iPod|Android", ua) else "desktop")
    first = lambda pairs: next((name for pat, name in pairs if re.search(pat, ua)), "Other")
    return {"device": device, "browser": first(BROWSERS), "os": first(OSES)}


APPS = {"facebookexternalhit": "iMessage / Facebook", "facebot": "iMessage / Facebook", "twitterbot": "X",
        "slackbot": "Slack", "discordbot": "Discord", "whatsapp": "WhatsApp", "telegrambot": "Telegram",
        "linkedinbot": "LinkedIn", "skypeuripreview": "Teams / Skype", "redditbot": "Reddit", "mastodon": "Mastodon",
        "bluesky": "Bluesky", "pinterest": "Pinterest"}


def preview_app(ua: str) -> str:
    m = PREVIEWS.search(ua or "")
    return APPS.get(m[0].lower(), m[0]) if m else "Other"


def bot_name(ua: str) -> str:
    """The word of the user agent that gave it away: Googlebot, HeadlessChrome, python-requests."""
    m = BOTS.search(ua or "")
    if not ua or not m:
        return "no user agent" if not ua else "Other"
    hit = m[0].lower()
    return next((w for w in re.findall(r"[A-Za-z][\w.-]*", ua) if hit in w.lower()), m[0])[:40]


def normalize(route: str) -> tuple[str, str | None]:
    """A #/ route as (template, item): '#/photos/photo/123' -> ('/photos/photo/:id', '123'); an album's
    photostream gives item 'album:<id>'. Map positions (@lat,lon,zoom) and anything unexpected end it."""
    path, _, qs = (route or "").lstrip("#").lstrip("/").partition("?")
    out, item = [], None
    for part in [p for p in path.split("/") if p][:4]:
        if part.isdigit() and len(part) <= 20:
            item = item or part
            out.append(":id")
        elif SEGMENT.fullmatch(part):
            out.append(part)
        else:
            break
    album = (parse_qs(qs).get("album") or [""])[0]
    if not item and album.isdigit() and len(album) <= 20:
        item = f"album:{album}"
    return "/" + "/".join(out or ["home"]), item


def ref_host(referrer: str, own_host: str) -> str | None:
    """The referring site's host name, or None for none, this site, or something that isn't a web address."""
    try:
        u = urlsplit(referrer or "")
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.")
    if u.scheme not in ("http", "https") or not host or host == (own_host or "").lower().removeprefix("www."):
        return None
    return host[:100]


class Visitors:
    """One per app: the salt, today's sessions, the per-address limits, and the hits not written yet."""

    def __init__(self, cfg: dict, db_path: str, geoip: GeoIP | None = None):
        self.cfg = cfg
        self.geo = geoip or GeoIP(Path(db_path).parent / "geoip")
        self.lock = threading.Lock()
        self.buffer: list[tuple] = []
        self._day: str | None = None
        self._salt = b""
        self._seen: dict[str, dict] = {}        # visitor -> {session, last, today}
        self._per_ip: dict[str, deque] = {}
        self.left_out: Counter = Counter()      # since the app started: owner, gpc, rate, limit, buffer
        self.written = 0
        self.flushed_at: float | None = None
        self.pruned_at = 0.0
        self.error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg["visitors"]["enabled"]

    def _today(self, now: float) -> str:
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
        if day != self._day:                    # midnight UTC: a new salt, and yesterday's visitors are gone
            self._day, self._salt, self._seen, self._per_ip = day, secrets.token_bytes(16), {}, {}
        return day

    def _limited(self, ip: str, now: float) -> bool:
        q = self._per_ip.setdefault(ip, deque())
        while q and q[0] <= now - 3600:
            q.popleft()
        if len(q) >= PER_IP_HOUR:
            return True
        q.append(now)
        return False

    def hit(self, kind: str, route: str, ip: str, ua: str, *, referrer: str = "", own_host: str = "",
            width: int | None = None, utm: dict | None = None, now: float | None = None) -> bool:
        """Count one hit (kind 'page' or 'preview'; a page from a bot becomes 'bot'). False if it was left out."""
        if not self.enabled:
            return False
        now = now or time.time()
        ua = (ua or "")[:500]
        if kind == "page" and BOTS.search(ua):
            kind = "bot"
        route, item = normalize(route)
        place = self.geo.lookup(ip) if ip and ip != "?" else {}   # outside the lock: it reads a file
        with self.lock:
            day = self._today(now)
            if self._limited(ip, now):
                self.left_out["rate"] += 1
                return False
            visitor = hashlib.sha256(self._salt + f"{ip}\0{ua}".encode()).hexdigest()[:16]
            seen = self._seen.get(visitor)
            if seen is None:
                seen = self._seen[visitor] = {"session": None, "last": 0.0, "today": 0}
            if seen["today"] >= PER_VISITOR_DAY:
                self.left_out["limit"] += 1
                return False
            if now - seen["last"] > SESSION_GAP:
                seen["session"] = secrets.token_hex(6)
            seen["last"], seen["today"] = now, seen["today"] + 1
            if len(self.buffer) >= BUFFER_MAX:
                self.left_out["buffer"] += 1
                return False
            utm = utm or {}
            clip = lambda v, n=100: (str(v)[:n] or None) if v else None
            agent = parse_ua(ua)
            if kind == "preview":               # the app that unfurled the link, in place of the browser
                agent["browser"] = preview_app(ua)
            elif kind == "bot":                 # and which bot
                agent["browser"] = bot_name(ua)
            self.buffer.append((
                int(now), day, visitor, seen["session"], kind, route, item, ref_host(referrer, own_host),
                clip(utm.get("source")), clip(utm.get("medium")), clip(utm.get("campaign")),
                place.get("city"), place.get("region"), place.get("country"), place.get("country_code"),
                agent["device"], agent["browser"], agent["os"], width if isinstance(width, int) and 0 < width < 20000 else None))
        return True

    def leave_out(self, why: str) -> None:
        with self.lock:
            self.left_out[why] += 1

    # ---- the scheduler's side ---------------------------------------------------------------
    def flush(self, store) -> int:
        """Write what's held. If the write fails it's kept for the next try."""
        with self.lock:
            rows, self.buffer = self.buffer, []
        if not rows:
            return 0
        try:
            store.site_hits_add(rows)
        except Exception as e:
            with self.lock:
                self.buffer[:0] = rows[:BUFFER_MAX - len(self.buffer)]
            self.error = f"{type(e).__name__}: {e}"[:300]
            caught(e, "visitors")
            return 0
        self.written += len(rows)
        self.flushed_at, self.error = time.time(), None
        return len(rows)

    def maintain(self, store, now: float | None = None) -> None:
        """Hourly: raw hits older than visitors.keep_days become a line in site_days; and the GeoIP file, monthly."""
        now = now or time.time()
        if now - self.pruned_at > 3600:
            self.pruned_at = now
            keep = self.cfg["visitors"]["keep_days"]
            store.site_hits_prune((dt.datetime.fromtimestamp(now, dt.timezone.utc).date() - dt.timedelta(days=keep)).isoformat())
        if self.enabled and self.geo.due(now):
            threading.Thread(target=self.geo.refresh, name="geoip", daemon=True).start()

    def run(self, stop: threading.Event, store_for) -> None:
        """The flush loop, until `stop`; a last flush on the way out."""
        while not stop.wait(FLUSH_SECONDS):
            try:
                self.flush(store_for())
                self.maintain(store_for())
            except Exception as e:
                caught(e, "visitors")
        self.flush(store_for())

    def status(self) -> dict:
        with self.lock:
            held = len(self.buffer)
        return {"enabled": self.enabled, "held": held, "written": self.written, "flushed_at": self.flushed_at,
                "error": self.error, "left_out": dict(self.left_out), "geoip_age": self.geo.age(),
                "geoip_updating": self.geo.updating, "geoip_error": self.geo.error}
