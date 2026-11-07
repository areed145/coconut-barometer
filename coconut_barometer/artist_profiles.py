"""Artist profiles from Claude, for the artist graph's profile matches (music_graph.py, the "profile" edges).

MusicBrainz gives facts for the artists it matched (musicbrainz.artist_facts): group or person, where they're from,
their years, genres. What it doesn't say, and what the artists it didn't match lack, Claude fills in once per artist:
the scenes or movements they belong to, a few style words, the decades they mattered most in, and their country.
The graph compares these itself, with no model in the loop, so the links can be tuned and explained.

Labels only match when they're spelled the same, so each request lists the scenes and styles already in use (and
MusicBrainz's genres) and asks Claude to reuse one when it fits. Everything is kept lower case.

Like MusicBrainz, this runs as ("profiles", "") jobs at the back of the review worker's line: a batch of BATCH of
the artists with over MIN_PLAYS plays not asked about yet, most played first, one request each, at most artist_profiles.daily_calls requests a day. It uses
the Anthropic connection Vision uses. Off until Settings → Music turns it on. Not in local mode."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections import Counter

from . import activity
from .errors import caught

logger = logging.getLogger(__name__)

MIN_PLAYS = 10                    # an artist is profiled once it has more plays than this, wherever it ranks
BATCH = 25
CHECK_EVERY = 300
MAX_SCENES, MAX_STYLES = 3, 5
VOCAB = 150                       # labels in use offered back, the most used first
META_CALLS = "profiles_calls"
SCHEMA = {"type": "object", "required": ["artists"], "properties": {"artists": {"type": "array", "items": {
    "type": "object", "required": ["name", "known"], "properties": {
        "name": {"type": "string", "description": "exactly as listed"},
        "known": {"type": "boolean", "description": "false if you aren't sure who this is; leave the rest empty"},
        "scenes": {"type": "array", "items": {"type": "string"}, "description": f"at most {MAX_SCENES} scenes or movements"},
        "styles": {"type": "array", "items": {"type": "string"}, "description": f"{MAX_STYLES} or fewer specific genres or styles"},
        "decades": {"type": "array", "items": {"type": "integer"}, "description": "the decades they mattered most in, e.g. 1970"},
        "country": {"type": "string", "description": "ISO 3166 two-letter code of where they're from, or empty"}}}}}}


class ProfileError(RuntimeError):
    """Claude can't be asked: the message is safe to show."""


def can_ask(cfg: dict) -> bool:
    v = cfg.get("vision") or {}
    return v.get("provider") in ("claude", "anthropic") and bool(v.get("api_key"))


def _label(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()[:40]


def clean(entry: dict, this_year: int) -> dict:
    """One artist's answer, as kept: lower-case labels, real decades, a two-letter country."""
    if not entry.get("known"):
        return {"scenes": [], "styles": [], "decades": [], "country": None}
    decades = sorted({int(d) for d in entry.get("decades") or [] if isinstance(d, (int, float)) and 1900 <= d <= this_year and int(d) % 10 == 0})
    country = str(entry.get("country") or "").strip().upper()
    return {"scenes": list(dict.fromkeys(filter(None, map(_label, entry.get("scenes") or []))))[:MAX_SCENES],
            "styles": list(dict.fromkeys(filter(None, map(_label, entry.get("styles") or []))))[:MAX_STYLES],
            "decades": decades, "country": country if re.fullmatch(r"[A-Z]{2}", country) else None}


def eligible_artists(store) -> list[str]:
    """The main artists ("A feat. B" is A's play, as the graph counts them) with over MIN_PLAYS plays, most played
    first. Not just the most played: the graph can draw any range, and more of your listening is worth exploring."""
    from .music_graph import credited

    rows = store.music_artist_plays()
    known = {a for a, _ in rows}
    plays: Counter = Counter()
    for artist, n in rows:
        plays[credited(artist, "", known)[0]] += n
    return [a for a, n in plays.most_common() if n > MIN_PLAYS]


class ArtistProfiles:
    def __init__(self, cfg: dict, ask=None):
        self.cfg = cfg
        self._ask = ask or self._claude                  # (prompt) -> (answer, input tokens, output tokens)

    def settings(self) -> dict:
        return self.cfg.get("artist_profiles") or {}

    def ready(self) -> bool:
        return bool(self.settings().get("enabled")) and can_ask(self.cfg)

    # ---- the daily limit ----------------------------------------------------------------------------------
    def calls_today(self, store, now: float | None = None) -> int:
        day = dt.datetime.fromtimestamp(now if now is not None else time.time(), dt.timezone.utc).date().isoformat()
        try:
            got = json.loads(store.get_meta(META_CALLS) or "{}")
        except ValueError:
            got = {}
        return int(got.get("n") or 0) if got.get("day") == day else 0

    def _count_call(self, store, now: float) -> None:
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
        store.set_meta(META_CALLS, json.dumps({"day": day, "n": self.calls_today(store, now) + 1}))

    def limit_reached(self, store, now: float | None = None) -> bool:
        return self.calls_today(store, now) >= int(self.settings().get("daily_calls") or 0)

    # ---- one batch ----------------------------------------------------------------------------------------
    def to_ask(self, store, limit: int | None = None) -> list[str]:
        asked = set(store.profiles())
        return [a for a in eligible_artists(store) if a not in asked][:limit or BATCH]

    def prompt(self, store, names: list[str]) -> str:
        have = store.profiles().values()
        scenes = Counter(s for p in have for s in p["scenes"])
        styles = Counter(s for p in have for s in p["styles"])
        for a in store.mb_graph()["artists"].values():
            styles.update({g: 1 for g, _ in a["genres"]})

        def use(c: Counter) -> str:
            return ", ".join(k for k, _ in c.most_common(VOCAB)) or "(none yet)"
        return ("For each artist below, say what scenes or movements they belong to, a few specific genres or styles, "
                "the decades they mattered most in, and the country they're from. These describe one person's "
                "listening, to find artists that belong together, so be specific (\"bakersfield sound\", not \"music\"), "
                "and reuse a label from the lists in use whenever one fits, spelled exactly the same. Use names exactly "
                "as listed. If you aren't sure who an artist is, say known: false rather than guessing.\n\n"
                f"Scenes in use: {use(scenes)}\n\nStyles and genres in use: {use(styles)}\n\nArtists:\n"
                + "\n".join(f"- {n}" for n in names) + "\n\nRecord your answer by calling record_profiles.")

    def run_once(self, store, now: float | None = None) -> dict:
        """One job: a batch of the artists not profiled yet, most played first, one request."""
        now = now if now is not None else time.time()
        if not self.ready():
            return {"status": "skipped", "error": "turned off, or no Anthropic key in Settings → Vision"}
        if self.limit_reached(store, now):
            return {"status": "skipped", "error": "today's limit of requests is reached"}
        names = self.to_ask(store)
        if not names:
            return {"status": "done", "rows": 0}
        self._count_call(store, now)
        try:
            answer, tin, tout = self._ask(self.prompt(store, names))
        except ProfileError as e:
            return {"status": "failed", "error": str(e)}
        said = {e.get("name"): e for e in answer.get("artists") or [] if isinstance(e, dict)}
        year = dt.datetime.fromtimestamp(now, dt.timezone.utc).year
        # One left out gets an empty profile too, so it isn't asked about forever.
        rows = [{"artist": n, **clean(said.get(n) or {}, year)} for n in names]
        store.profiles_save(rows, self.cfg["vision"].get("model") or "", int(now))
        return {"status": "done", "rows": len(rows), "input_tokens": tin, "output_tokens": tout}

    def _claude(self, prompt: str) -> tuple[dict, int, int]:
        import anthropic

        v = self.cfg["vision"]
        client = anthropic.Anthropic(api_key=v["api_key"], max_retries=3, **({"base_url": v["base_url"]} if v.get("base_url") else {}))
        with activity.log.call("vision", "artist profiles", f"{prompt.count(chr(10) + '- ')} artists") as call:
            resp = client.messages.create(
                model=v["model"], max_tokens=6000, messages=[{"role": "user", "content": prompt}],
                tools=[{"name": "record_profiles", "description": "Record the profiles.", "input_schema": SCHEMA}],
                tool_choice={"type": "tool", "name": "record_profiles"})
            call["status"] = "ok"
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_profiles":
                return dict(block.input), resp.usage.input_tokens, resp.usage.output_tokens
        raise ProfileError("the model didn't answer")

    # ---- the background ----------------------------------------------------------------------------------
    def due(self, store, now: float | None = None) -> bool:
        return self.ready() and not self.limit_reached(store, now) and bool(self.to_ask(store, 1))

    def run(self, stop, store, queue) -> None:
        """Every few minutes: a job on the worker, queue(), if an artist isn't profiled and today allows it."""
        while not stop.wait(CHECK_EVERY):
            try:
                if self.due(store()):
                    queue()
            except Exception as e:
                caught(e, "artist profiles")

    def state(self, store) -> dict:
        eligible = eligible_artists(store)
        have = store.profiles()
        return {"enabled": bool(self.settings().get("enabled")), "can_ask": can_ask(self.cfg), "ready": self.ready(),
                "eligible": len(eligible), "min_plays": MIN_PLAYS, "asked": sum(1 for a in eligible if a in have),
                "calls_today": self.calls_today(store), "daily_calls": int(self.settings().get("daily_calls") or 0)}
