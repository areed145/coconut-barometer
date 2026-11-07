"""Recording credits from MusicBrainz, for the artist graph (music_graph.py, the "musicbrainz" edges). Issue #192.

Apple's catalog gives one free-text composer per song. MusicBrainz has real credits: producers, performers, writers,
featured artists, and which artists are members of which bands. Each track is looked up once:
  - by the ISRC Apple gave it (music_tracks.isrc), else a search for its title and main artist, kept only when the
    best match is credited to that same artist
  - then the recording, with its artist relations and its work's (the writers)
  - and later each matched artist, for its band memberships and collaborations, and its facts: group or person,
    where it's from, its years, and the genres and tags MusicBrainz's editors voted for (the graph's profiles)
Answers, found or not, are kept (mb_recordings, mb_credits, mb_artists, mb_artist_rels), so nothing is asked twice.

MusicBrainz asks for at most one request a second and a User-Agent naming the app and a way to reach whoever runs it
(musicbrainz.contact). So lookups never run on a thread of their own: they're ("musicbrainz", "") jobs on the review
worker, the one line every outside job waits in, a batch of at most CALLS_PER_JOB requests each so a long backlog
doesn't hold up a Flickr sync, and every request waits its turn under a SharedRateLimiter (ratelimit.py) shared by every
process on this machine. Off until Settings → Music turns it on and gives a contact. Not in local mode."""
from __future__ import annotations

import logging
import re
import time
import unicodedata

import requests

from . import __version__, activity
from .config import data_dir
from .errors import caught
from .ratelimit import SharedRateLimiter, limiter_path

logger = logging.getLogger(__name__)

API = "https://musicbrainz.org/ws/2"
CALLS_PER_SECOND = 1.0                  # MusicBrainz's rule for every client
CALLS_PER_JOB = 60                      # about a minute of the worker's time, then it's queued again
TIMEOUT = 20
RETRIES = (2, 6)                        # seconds before asking again when MusicBrainz is busy (503) or failing
CHECK_EVERY = 120
SEARCH_MIN_SCORE = 90
RECORDING_INC = "artist-credits+artist-rels+work-rels+work-level-rels"
RECORDING_ROLES = {"producer": "producer", "co-producer": "producer", "instrument": "performer", "vocal": "performer",
                   "performer": "performer", "performing orchestra": "performer", "conductor": "performer",
                   "remixer": "remixer", "arranger": "arranger"}
WORK_ROLES = {"composer": "writer", "lyricist": "writer", "writer": "writer", "librettist": "writer"}
ARTIST_ROLES = {"member of band": "member", "collaboration": "collaboration"}
ARTIST_INC = "artist-rels+genres+tags"
MAX_TAGS = 15
# Kept from the looked-up artist's side: "in band" (it's in the other), "band of" (the other is in it), "collaboration".
FEAT_IN_TITLE = re.compile(r"\s*[(\[]\s*(?:feat\.?|featuring|ft\.?|with)\s+[^)\]]*[)\]]", re.I)


class Busy(RuntimeError):
    """MusicBrainz said to slow down, or didn't answer: the job stops here and the next one carries on."""


def norm(name: str | None) -> str:
    """A name as compared across Apple and MusicBrainz: no accents, case, punctuation, leading "The", "&" or "and"."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().casefold()
    s = re.sub(r"\s*(&|\+|\band\b)\s*", " and ", s)
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[4:] if s.startswith("the ") else s


def ready(cfg: dict) -> bool:
    mb = cfg.get("musicbrainz") or {}
    return bool(mb.get("enabled") and (mb.get("contact") or "").strip())


def user_agent(cfg: dict) -> str:
    return f"coconut-barometer/{__version__} ( {(cfg.get('musicbrainz') or {}).get('contact', '').strip()} )"


def shared_limiter() -> SharedRateLimiter:
    """One for every process on this machine: MusicBrainz counts by IP address, not by key."""
    d = data_dir()
    return SharedRateLimiter(limiter_path("musicbrainz", str(d) if d else "~/.flickr"), 0, CALLS_PER_SECOND)


# ---- reading MusicBrainz's answers ------------------------------------------------------------------------

def _credit_names(recording: dict) -> list[dict]:
    """The artist credit, in order: [{name, mbid}] ("A feat. B" is A, then B)."""
    out = []
    for c in recording.get("artist-credit") or []:
        a = c.get("artist") or {}
        if a.get("id"):
            out.append({"name": a.get("name") or c.get("name") or "", "mbid": a["id"]})
    return out


def pick(recordings: list[dict], artist: str) -> dict | None:
    """The recording credited first to this artist (by name), else none: a match by title alone isn't trusted."""
    want = norm(artist)
    for r in recordings:
        names = _credit_names(r)
        if names and norm(names[0]["name"]) == want:
            return r
    return None


def credits(recording: dict) -> list[dict]:
    """[{role, name, mbid}] of one recording: its main artist ("artist"), the rest of its artist credit ("featured"),
    and the people its relations, and its work's, name."""
    out, seen = [], set()

    def add(role, artist):
        if artist and artist.get("id") and (role, artist["id"]) not in seen:
            seen.add((role, artist["id"]))
            out.append({"role": role, "name": artist.get("name") or "", "mbid": artist["id"]})

    for i, c in enumerate(_credit_names(recording)):
        add("artist" if i == 0 else "featured", {"id": c["mbid"], "name": c["name"]})
    for rel in recording.get("relations") or []:
        if rel.get("target-type") == "artist" and rel.get("type") in RECORDING_ROLES:
            add(RECORDING_ROLES[rel["type"]], rel.get("artist"))
        elif rel.get("target-type") == "work":
            for wrel in (rel.get("work") or {}).get("relations") or []:
                if wrel.get("target-type") == "artist" and wrel.get("type") in WORK_ROLES:
                    add(WORK_ROLES[wrel["type"]], wrel.get("artist"))
    return out


def artist_rels(artist: dict) -> list[dict]:
    """[{role, mbid, name}]: the bands this artist is in or that it's made of, and its collaborations."""
    out = []
    for rel in artist.get("relations") or []:
        other = rel.get("artist") or {}
        if rel.get("target-type") == "artist" and rel.get("type") in ARTIST_ROLES and other.get("id"):
            role = ARTIST_ROLES[rel["type"]]
            if role == "member":
                role = "band of" if rel.get("direction") == "backward" else "in band"
            out.append({"role": role, "mbid": other["id"], "name": other.get("name") or ""})
    return out


def _year(date: str | None) -> int | None:
    m = re.match(r"(\d{4})", date or "")
    return int(m.group(1)) if m else None


def artist_facts(artist: dict) -> dict:
    """What the graph's profiles take from an artist: type, country (a code), area (the place it began: its plain
    area is often only a country), begin and end years, and its genres and tags as [name, votes], most voted first."""
    life = artist.get("life-span") or {}

    def voted(key):
        got = [(t.get("name", "").strip().lower(), int(t.get("count") or 0)) for t in artist.get(key) or []]
        return [[n, c] for n, c in sorted(got, key=lambda x: -x[1]) if n and c > 0][:MAX_TAGS]
    return {"type": artist.get("type"), "country": artist.get("country"),
            "area": (artist.get("begin-area") or {}).get("name"),
            "begin_year": _year(life.get("begin")), "end_year": _year(life.get("end")),
            "genres": voted("genres"), "tags": voted("tags")}


def _quote(text: str) -> str:
    return '"' + re.sub(r'([\\"])', r"\\\1", text) + '"'


class MusicBrainz:
    def __init__(self, cfg: dict, limiter: SharedRateLimiter | None = None, get=requests.get, sleep=time.sleep):
        self.cfg, self.get, self.sleep = cfg, get, sleep
        self.limiter = limiter or shared_limiter()
        self.calls = 0                                  # since start, for Status

    def ready(self) -> bool:
        return ready(self.cfg)

    # ---- one request ------------------------------------------------------------------------------------
    def _ask(self, path: str, what: str, detail: str = "", **params) -> dict | None:
        """GET one resource, after waiting for its slot. None when MusicBrainz doesn't have it (404 or 400)."""
        log = activity.log
        for wait in (*RETRIES, None):
            token = log.begin("musicbrainz", what, detail)
            self.limiter.wait(on_slot=lambda slot: log.until(token, slot))
            log.running(token)
            self.calls += 1
            try:
                r = self.get(f"{API}/{path}", params={**params, "fmt": "json"}, timeout=TIMEOUT,
                             headers={"User-Agent": user_agent(self.cfg), "Accept": "application/json"})
            except requests.RequestException as e:
                log.end(token, "error", f"{type(e).__name__}: {e}"[:300], error=True)
                if wait is None:
                    raise Busy("MusicBrainz didn't answer") from None
                self.sleep(wait)
                continue
            if r.status_code == 200:
                log.end(token)
                return r.json()
            if r.status_code in (400, 404):
                log.end(token, str(r.status_code), "not found")
                return None
            log.end(token, str(r.status_code), error=True)
            if wait is None or r.status_code not in (429, 503) and r.status_code < 500:
                raise Busy(f"MusicBrainz answered {r.status_code}")
            self.sleep(wait)
        raise Busy("MusicBrainz is busy")             # not reached: the last try raises or returns

    # ---- one track, one artist --------------------------------------------------------------------------
    def find(self, track: dict) -> tuple[str | None, str]:
        """(the recording's MBID, how it was found: isrc | search | ""), at most two requests."""
        from .music_graph import credited

        main = credited(track["artist"], "", set())[0]
        if track.get("isrc"):
            found = self._ask(f"isrc/{track['isrc']}", "ISRC", track["isrc"], inc="artist-credits")
            r = pick((found or {}).get("recordings") or [], main)
            if not r and found and len(found.get("recordings") or []) == 1:   # one recording for the code: it's ours
                r = found["recordings"][0]
            if r:
                return r["id"], "isrc"
        title = FEAT_IN_TITLE.sub("", track.get("name") or "").strip()
        if not title or not main:
            return None, ""
        found = self._ask("recording", "search", f"{main} – {title}"[:120],
                          query=f"recording:{_quote(title)} AND artist:{_quote(main)}", limit=5)
        good = [r for r in (found or {}).get("recordings") or [] if int(r.get("score") or 0) >= SEARCH_MIN_SCORE]
        r = pick(good, main)
        return (r["id"], "search") if r else (None, "")

    def run_once(self, store, budget: int = CALLS_PER_JOB, now: float | None = None) -> dict:
        """One job: tracks first, most played first, then the artists they matched, until `budget` requests."""
        now = int(now if now is not None else time.time())
        if not self.ready():
            return {"status": "skipped", "error": "turned off, or no contact in Settings → Music"}
        start, tracks, artists = self.calls, 0, 0
        try:
            for t in store.mb_to_ask(max(1, budget // 2)):
                if self.calls - start + 3 > budget:
                    break
                mbid, how = self.find(t)
                rec = self._ask(f"recording/{mbid}", "recording", t["name"][:80], inc=RECORDING_INC) if mbid else None
                store.mb_save_track(t["id"], mbid if rec else None, how if rec else "", credits(rec) if rec else [], now)
                tracks += 1
            for a in store.mb_artists_to_ask(budget):
                if self.calls - start + 1 > budget:
                    break
                found = self._ask(f"artist/{a['mbid']}", "artist", a["name"][:80], inc=ARTIST_INC)
                store.mb_save_artist(a["mbid"], (found or {}).get("name") or a["name"], artist_rels(found or {}), now,
                                     artist_facts(found or {}))
                artists += 1
        except Busy as e:
            return {"status": "failed", "error": str(e), "rows": tracks, "artists": artists, "calls": self.calls - start}
        return {"status": "done", "rows": tracks, "artists": artists, "calls": self.calls - start}

    def due(self, store) -> bool:
        return self.ready() and bool(store.mb_to_ask(1) or store.mb_artists_to_ask(1))

    def run(self, stop, store, queue) -> None:
        """Every couple of minutes: a job on the worker, queue(), if there's anything left to look up."""
        while not stop.wait(CHECK_EVERY):
            try:
                if self.due(store()):
                    queue()
            except Exception as e:
                caught(e, "musicbrainz")

    def state(self, store) -> dict:
        return {"enabled": bool((self.cfg.get("musicbrainz") or {}).get("enabled")), "ready": self.ready(),
                **store.mb_progress()}
