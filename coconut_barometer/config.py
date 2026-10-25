"""Config loading: YAML + defaults, and validation.

DEFAULTS is the single source of every setting's default; code reads cfg[...] directly.
load_config refuses a config with errors (unknown key, wrong type, bad choice, number out
of range, unknown placeholder, broken regex). `python -m coconut_barometer check` also
lists warnings and checks the config against your library.
"""
from __future__ import annotations

import copy
import difflib
import logging
import os
import re
import string
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "flickr": {"api_key": None, "api_secret": None},
    "weatherflow": {
        "token": None,
        "backfill_days": 7,          # history fetched from REST: forward from the newest stored, back this many days
    },
    "qc": {                          # station QC (#104): each station checked against RTMA, radar, GLM and more
        "enabled": True,              # sample the references at the stations and run the checks
        "backfill_days": 30,          # the past filled in from NOAA's archives (RTMA, hourly) on first run
        "keep_days": 365,             # reference samples and check results kept this long
        "window_days": 30,            # each check looks at this much
        "min_pairs": 96,              # fewer readings to compare than this: "not enough data" (a day of RTMA)
        "temp_bias_warn": 1.5,        # temperature vs RTMA, °C either way (after the height difference)
        "temp_bias_fail": 3.0,
        "dew_bias_warn": 2.0,         # dewpoint vs RTMA, °C either way (RTMA's dewpoint is less sure than its temperature)
        "dew_bias_fail": 4.0,
        "sun_warn": 1.0,              # °C warmer in strong sun with light wind than at night (vs RTMA): the radiation shield
        "sun_fail": 2.0,
        "drift_warn": 1.0,            # °C the last week's temperature bias has moved from the rest of the window's
        "drift_fail": 2.0,
        "coverage_warn": 98.0,        # % of the minutes reported over the window, below which: warn ...
        "coverage_fail": 90.0,        # ... and fail
        "solar_warn": 0.8,            # clear days' solar peak over the clear-sky model's, below which: warn (dirty or shaded) ...
        "solar_fail": 0.6,            # ... and fail
        "wind_dir_warn": 15.0,        # degrees the station's wind direction is off RTMA's on average (aligned to north?)
        "wind_dir_fail": 30.0,
        "wind_sector_warn": 0.6,      # a direction's speed ratio (vs RTMA) over the station's overall one, below which:
        "wind_sector_fail": 0.4,      # warn, and fail (something blocking the wind from that side)
        "pressure_warn": 1.0,         # hPa either way vs RTMA at the station's elevation setting (~8 m of elevation per hPa)
        "pressure_fail": 2.5,
        "pressure_drift_warn": 0.5,   # hPa the last week's pressure offset has moved from the rest of the window's
        "pressure_drift_fail": 1.0,
        "lightning_false_warn": 0.3,  # share of the station's strikes with no GLM lightning in reach (interference) ...
        "lightning_false_fail": 0.6,
        "lightning_miss_warn": 0.5,   # share of the periods GLM saw lightning within 20 km that the station heard none
        "lightning_miss_fail": 0.8,
        "fronts_warn": 0.5,           # share of front passages with the expected wind shift, pressure and temperature
    },
    "display": {"units": "metric"},  # how Weather and APRS show values; always stored metric
    "aprs": {
        "callsigns": ["KJ4TFD-10"],   # tracked on the APRS page, heard live from APRS-IS
        "igate": "WA5DTR-15",         # the packets APRS-IS credits it with gating, for the Igate page ("" for none)
        "igate_location": "",         # "lat, lon" or a grid locator; empty: where its own beacons put it
        "passcode": None,             # APRS-IS passcode for the callsign: only for sending (CWOP weather reports)
        "cwop": {},                   # station name -> the callsign-SSID it reports to CWOP as; none: nothing sent
        "cwop_minutes": 10,           # how often each reports (CWOP asks for 5-10 minutes, no more often than 5)
        "trip_stop_minutes": 10,      # staying this long (within 100 m) ends a trip (#120)
        "trip_gap_minutes": 30,       # nothing heard this long while it moved: a gap, ending a trip without a stop
        "places": [],                 # named circles: {name, lat, lon, radius_m (200)}, for arrive/leave alerts and stops
    },
    "alerts": {                       # notifications to the devices turned on in Settings → Alerts; 0 turns one off
        "lightning_km": 15,           # a strike this close to a station
        "gust_kmh": 60,               # a gust this strong
        "pressure_drop_hpa": 2.0,     # pressure down this much in 3 hours (a storm sign)
        "callsign_quiet_hours": 6,    # a tracked callsign heard after this long without a packet
        "places": [],                 # {callsign, place, on: arrive | leave | both, quiet: "22:00-07:00" (optional)}
        "place_dwell_minutes": 5,     # a single position in (or out of) a place counts once it has stood this long
        "igate_quiet_minutes": 60,    # the igate gating nothing this long when it usually would, or its beacon stopped
        "igate_record_days": 0,       # the igate heard a station direct farther than any in this many days (0: off)
        "igate_altitude_km": 0,       # a packet the igate gated from this high up: a balloon or a flight (0: off)
        "storm_km": 10,               # a storm cell (NWS radar tracking) whose path comes this close to a station ...
        "storm_minutes": 60,          # ... within this long
        "storm_dbz": 50,              # ... and at least this strong
        "rain_minutes": 30,           # rain on radar (MRMS) closing in on a station, due within this long
        "storm_max_kmh": 120,         # a storm or rain reported faster than this is a tracking error: not projected
        "radar_per_hour": 3,          # storm and rain alerts per station per hour, at most, whatever else
        "cooldown_minutes": 60,       # the same alert (same kind, same station or callsign) at most this often
    },
    "github": {
        "token": None,                # fine-grained, Issues: read and write; set: unexpected errors become issues
        "repo": "areed145/coconut-barometer",   # owner/name the issues go to
    },
    "database": "db.duckdb",
    "albums": {"create_missing": False},
    "groups": {
        "suggest": True,              # propose group pools for each photo, from the photos you've put in each
        "max_per_photo": 3,
        "min_match": 3.0,             # how well it must match (see grouping.py); higher: fewer, surer
    },
    "tags": {
        "always_add": [],             # on every photo's proposed tag list (e.g. your name, your site)
        "always_remove": [],          # never on it: taken off when proposed (* and ? wildcards, e.g. www*)
    },
    "trips": {                        # photos grouped by themselves into trips (trips.py)
        "gap_hours": 36,              # a new trip after this long without a photo
        "jump_km": 50,                # ... or after moving this far across most of a day without photos
        "min_photos": 5,              # fewer than this: not a trip
    },
    "geo": {
        "min_accuracy": {"country": 1, "state": 6, "city": 11},
        "countries": {"aliases": {}},
        "us_states": {"enabled": True, "title": "{state}"},
        "cities": [],
    },
    "vision": {
        "provider": "claude",
        "model": "claude-haiku-5-5",
        "api_key": None,
        "base_url": None,
        "min_confidence": 0.75,
        "use_photo_text": True,
        "use_location": True,
        "extra_tags": {"max": 6},
        "describe": {"max_words": 40, "instructions": None},
        "title": {"max_words": 8, "instructions": None},
        "categories": {},
    },
    "schedule": {
        "full_every_days": 7,        # full sync: every photo, album and album's photos (~180 calls)
        "photo_list_every_hours": 24,  # every photo re-read: view counts + the daily snapshot (~22 calls)
        "stats": True,               # per-day stats (Flickr Pro): yesterday, plus backfill
        "backfill_days_per_run": 7,  # older days fetched per run until Flickr has none (~6 calls/day)
    },
    "review": {
        "when_queued": "process",    # a photo sent to review (new from a sync, or from the gallery): "process" it
                                     # right away (vision costs), or "queue" it, processed when you open it
        "missing_location": True,    # photos without a geotag: into review with a guessed location (the daily
                                     # re-read of every photo sends any that aren't there), and one proposed
    },
    "backup": {
        "every_hours": 24,            # the web app's scheduler makes one this often; 0 = off
        "keep": 7,                    # newest kept; older deleted
        "dir": None,                  # default: backups/ next to the database
    },
    "visitors": {
        "enabled": True,              # count this site's visitors (the Visitors page)
        "keep_days": 90,              # raw page views kept; older days keep only their totals
        "honor_gpc": True,            # leave out browsers sending Global Privacy Control or Do Not Track
    },
    "web": {
        "host": "127.0.0.1",          # behind a reverse proxy (Caddy) on a server; never 0.0.0.0 bare
        "port": 8000,
        "allowed_hosts": ["localhost", "127.0.0.1"],   # Host headers accepted (add your domain)
        "secure_cookies": True,       # cookies only over HTTPS; false only for http://localhost testing
        "session_days": 14,
        "sync_every_minutes": 60,     # the scheduler runs `sync` (whatever's due) this often; 0 = off
        "api_docs": False,            # /api/docs (OpenAPI); only after login when on
        "trusted_proxies": ["127.0.0.1"],   # where X-Forwarded-For/-Proto are believed from
    },
    "theme": {
        "color_low": "#f7d125",       # the gradient for map clusters and chart bars: low values ...
        "color_mid": "#8abe35",       # ... through the middle (by default halfway between the other two) ...
        "color_high": "#1dab45",      # ... to high (log scale)
        "radar_colors": [             # the Weather map's radar, by dBZ: blended between these stops
            {"dbz": 15, "color": "#8fd3ff"}, {"dbz": 25, "color": "#2fa84f"}, {"dbz": 35, "color": "#f2d22e"},
            {"dbz": 45, "color": "#f28c28"}, {"dbz": 55, "color": "#d7301f"}, {"dbz": 65, "color": "#b03ab5"},
        ],
        "lightning_colors": [         # the Weather map's lightning, by age in minutes (0-15): blended between these
            {"min": 0, "color": "#ffffff"}, {"min": 2, "color": "#fff36b"}, {"min": 5, "color": "#ffb020"},
            {"min": 10, "color": "#f0582a"}, {"min": 15, "color": "#b3122e"},
        ],
    },
    "map": {
        "expand_at": 40,              # a cluster this small opens into thumbnails when clicked
        "carto_key": None,            # CARTO basemaps key (carto.com/basemaps/apikey); goes in every tile URL
    },
    "standard_tags": {
        "date": {"enabled": False, "formats": ["y{year}", "m{month:02d}", "d{day:02d}"]},
        "place": {"enabled": False},
        "camera": {"enabled": False, "name_words": True, "albums": []},
    },
}

# Fixed: not settings any more (see RETIRED).
FLICKR_CALLS_PER_SECOND = 1.1       # ~3960/hour, over Flickr's stated 3600/hour per key, on purpose; shared by everything
WEATHERFLOW_CALLS_PER_SECOND = 2.0
VISION_THUMBNAIL = "url_m"          # 500 px: plenty for the model
VISION_CONCURRENCY = 4
MAX_TAGS_PER_PHOTO = 75             # Flickr's limit

# Settings whose keys are yours (album titles, country codes, category names...).
FREE_MAPS = {("geo", "countries", "aliases"), ("vision", "categories"), ("aprs", "cwop")}
# Settings whose default is None but that take a value.
OPTIONAL = {("backup", "dir"): str, ("map", "carto_key"): str, ("flickr", "api_key"): str, ("flickr", "api_secret"): str, ("vision", "api_key"): str, ("weatherflow", "token"): str,
            ("vision", "base_url"): str, ("aprs", "passcode"): str, ("github", "token"): str, ("vision", "describe", "instructions"): str,
            ("vision", "title", "instructions"): str}
CHOICES = {("vision", "provider"): ("claude", "anthropic", "openai", "ollama"),
           ("review", "when_queued"): ("process", "queue"), ("display", "units"): ("metric", "imperial")}
RANGES = {("vision", "min_confidence"): (0, 1), ("vision", "extra_tags", "max"): (0, 20),
          ("vision", "describe", "max_words"): (5, 200), ("vision", "title", "max_words"): (2, 20),
          ("schedule", "full_every_days"): (1, 365),
          ("schedule", "photo_list_every_hours"): (1, 24 * 30), ("schedule", "backfill_days_per_run"): (0, 400),
          ("backup", "every_hours"): (0, 24 * 30), ("backup", "keep"): (1, 365), ("visitors", "keep_days"): (7, 3650),
          ("web", "port"): (1, 65535), ("web", "session_days"): (1, 365), ("web", "sync_every_minutes"): (0, 24 * 60),
          ("map", "expand_at"): (2, 200), ("weatherflow", "backfill_days"): (0, 3650),
          ("trips", "gap_hours"): (1, 24 * 14), ("trips", "jump_km"): (1, 20000), ("trips", "min_photos"): (2, 1000),
          ("groups", "max_per_photo"): (0, 10), ("groups", "min_match"): (0, 50), ("aprs", "cwop_minutes"): (5, 60),
          ("aprs", "trip_stop_minutes"): (1, 24 * 60), ("aprs", "trip_gap_minutes"): (5, 24 * 60),
          ("alerts", "lightning_km"): (0, 40), ("alerts", "gust_kmh"): (0, 300), ("alerts", "pressure_drop_hpa"): (0, 20),
          ("alerts", "callsign_quiet_hours"): (0, 24 * 30), ("alerts", "igate_altitude_km"): (0, 50),
          ("alerts", "igate_quiet_minutes"): (0, 24 * 60),
          ("alerts", "place_dwell_minutes"): (1, 24 * 60),
          ("alerts", "igate_record_days"): (0, 3650),
          ("alerts", "cooldown_minutes"): (5, 24 * 60),
          ("alerts", "storm_km"): (0, 100), ("alerts", "storm_minutes"): (5, 180), ("alerts", "storm_dbz"): (20, 75),
          ("alerts", "rain_minutes"): (0, 120), ("alerts", "storm_max_kmh"): (30, 300), ("alerts", "radar_per_hour"): (1, 30),
          ("qc", "backfill_days"): (0, 365), ("qc", "keep_days"): (30, 3650), ("qc", "window_days"): (1, 365),
          ("qc", "min_pairs"): (1, 10000), ("qc", "temp_bias_warn"): (0.1, 20), ("qc", "temp_bias_fail"): (0.1, 30),
          ("qc", "dew_bias_warn"): (0.1, 20), ("qc", "dew_bias_fail"): (0.1, 30), ("qc", "sun_warn"): (0.1, 10),
          ("qc", "sun_fail"): (0.1, 20), ("qc", "drift_warn"): (0.1, 10), ("qc", "drift_fail"): (0.1, 20),
          ("qc", "coverage_warn"): (0, 100), ("qc", "coverage_fail"): (0, 100), ("qc", "solar_warn"): (0, 1.5),
          ("qc", "solar_fail"): (0, 1.5), ("qc", "wind_dir_warn"): (1, 180), ("qc", "wind_dir_fail"): (1, 180),
          ("qc", "wind_sector_warn"): (0, 1), ("qc", "wind_sector_fail"): (0, 1), ("qc", "pressure_warn"): (0.1, 20),
          ("qc", "pressure_fail"): (0.1, 50), ("qc", "pressure_drift_warn"): (0.1, 10), ("qc", "pressure_drift_fail"): (0.1, 20),
          ("qc", "lightning_false_warn"): (0, 1), ("qc", "lightning_false_fail"): (0, 1), ("qc", "lightning_miss_warn"): (0, 1),
          ("qc", "lightning_miss_fail"): (0, 1), ("qc", "fronts_warn"): (0, 1)}
COLORS = {("theme", "color_low"), ("theme", "color_mid"), ("theme", "color_high")}   # #rrggbb

# Settings the environment can set (API keys can't: they go in Settings, encrypted).
ENV_OVERRIDES = {"DATABASE_PATH": ("database",)}
PLACEHOLDERS = {
    ("vision", "describe", "instructions"): {"max_words"},
    ("vision", "title", "instructions"): {"max_words"},
    ("geo", "us_states", "title"): {"state"},
}
LIST_PLACEHOLDERS = {("standard_tags", "date", "formats"): {"year", "month", "day", "season"}}
# Settings that no longer exist. Old config files and databases may still have them; they're
# dropped on reading (nothing to fix by hand). Every photo now gets every proposal (title,
# description, tags, albums), and you accept or decline each in the reviewer.
RETIRED = {("geo", "enabled"), ("rename",), ("vision", "enabled"), ("vision", "include_videos"),
           ("vision", "extra_tags", "enabled"), ("vision", "describe", "enabled"), ("vision", "describe", "reword"),
           ("vision", "title", "enabled"), ("vision", "title", "replace"), ("web", "process_new"),
           ("standard_tags", "place", "split_words"), ("standard_tags", "coords"), ("standard_tags", "owner"),
           ("standard_tags", "filler"), ("weather",),
           # Settings cleanup: tags come from place tags (not geo.add_tags), and the rest are fixed above.
           ("geo", "add_tags"), ("geo", "countries", "auto_match"), ("flickr", "calls_per_second"),
           ("weatherflow", "calls_per_second"), ("vision", "thumbnail"), ("vision", "concurrency"),
           ("vision", "extra_tags", "prefix"), ("standard_tags", "max_tags_per_photo"),
           ("standard_tags", "date", "skip_if_dated"), ("standard_tags", "place", "country"),
           ("standard_tags", "place", "regions"), ("standard_tags", "place", "state"),
           ("standard_tags", "place", "state_abbr"), ("standard_tags", "place", "city"),
           ("standard_tags", "place", "region_overrides"), ("standard_tags", "album_tags")}


# Settings that moved: old path -> new path (the old one is used if the new one isn't set).
RENAMED = {"map.color_low": "theme.color_low", "map.color_high": "theme.color_high",
           "weather.units": "display.units", "schedule.new_photos": "review.when_queued"}


def retire(raw: dict) -> dict:
    """raw without RETIRED settings (a copy), with RENAMED ones moved. web.process_new: true
    becomes review.when_queued: process."""
    import copy

    raw = copy.deepcopy(raw)
    for old, new in RENAMED.items():
        o, n = old.split("."), new.split(".")
        value = (raw.get(o[0]) or {}).pop(o[1], None) if isinstance(raw.get(o[0]), dict) else None
        if value is not None and n[1] not in (raw.get(n[0]) or {}):
            raw.setdefault(n[0], {})[n[1]] = value
    if (raw.get("web") or {}).get("process_new") is True and "when_queued" not in (raw.get("review") or {}):
        raw.setdefault("review", {})["when_queued"] = "process"
    for path in RETIRED:
        node = raw
        for k in path[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return raw


ITEM_KEYS = {("geo", "cities"): ({"album", "lat", "lon"}, {"radius_km", "name", "tag"}),
             ("aprs", "places"): ({"name", "lat", "lon"}, {"radius_m"}),
             ("alerts", "places"): ({"callsign", "place", "on"}, {"quiet"}),
             ("standard_tags", "camera", "albums"): ({"match", "album"}, set())}


def validate(raw: dict, cfg: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a config: raw is what the YAML said, cfg is it merged with DEFAULTS."""
    errors: list[str] = []
    warnings: list[str] = []
    name = lambda path: ".".join(path)

    def walk(raw_node, default, path):
        if not isinstance(raw_node, dict):
            return
        for key, value in raw_node.items():
            here = path + (key,)
            if key not in default:
                close = difflib.get_close_matches(str(key), [str(k) for k in default], n=1)
                errors.append(f"{name(here)}: unknown setting" + (f" (did you mean {name(path + (close[0],))}?)" if close else ""))
                continue
            want = default[key]
            if here in OPTIONAL:
                if value is not None and not isinstance(value, OPTIONAL[here]):
                    errors.append(f"{name(here)}: should be {OPTIONAL[here].__name__} or empty, not {value!r}")
            elif isinstance(want, bool):
                if not isinstance(value, bool):
                    errors.append(f"{name(here)}: should be true or false, not {value!r}")
            elif isinstance(want, (int, float)):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(want, int) and not isinstance(want, bool) and isinstance(value, float) and not value.is_integer()):
                    errors.append(f"{name(here)}: should be a number, not {value!r}")
            elif isinstance(want, str):
                if not isinstance(value, str):
                    errors.append(f"{name(here)}: should be text, not {value!r}")
            elif isinstance(want, list):
                if not isinstance(value, list):
                    errors.append(f"{name(here)}: should be a list, not {value!r}")
            elif isinstance(want, dict):
                if value is None:
                    errors.append(f"{name(here)}: is empty; delete it to use the defaults, or give it settings")
                elif not isinstance(value, dict):
                    errors.append(f"{name(here)}: should be a group of settings, not {value!r}")
                elif here not in FREE_MAPS:
                    walk(value, want, here)
    walk(raw, DEFAULTS, ())
    # The value checks assume the right shapes; any that hit a value already reported as the
    # wrong type are skipped, so every other problem is still listed in the same pass.
    _check_values(cfg, errors, warnings)
    return errors, warnings


def _check_values(cfg: dict, errors: list[str], warnings: list[str]) -> None:
    name = lambda path: ".".join(path)

    def get(path):
        v = _at(cfg, path)
        default = _at(DEFAULTS, path)
        if path not in OPTIONAL and default is not None and not isinstance(v, type(default)) \
                and not (isinstance(default, float) and isinstance(v, int)):
            raise TypeError(name(path))   # wrong shape, already reported
        return v

    def each(checks):
        for check in checks:
            try:
                check()
            except TypeError:
                pass

    checks = []
    for path, options in CHOICES.items():
        def choice(path=path, options=options):
            if get(path) not in options:
                errors.append(f"{name(path)}: {get(path)!r} isn't one of {', '.join(options)}")
        checks.append(choice)
    for path, (lo, hi) in RANGES.items():
        def in_range(path=path, lo=lo, hi=hi):
            v = get(path)
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                errors.append(f"{name(path)}: {v} is outside {lo}..{'' if hi is None else hi}".rstrip("."))
        checks.append(in_range)

    for path, allowed in PLACEHOLDERS.items():
        def placeholders(path=path, allowed=allowed):
            if get(path) is not None:
                errors.extend(_placeholders(name(path), get(path), allowed))
        checks.append(placeholders)
    for path, allowed in LIST_PLACEHOLDERS.items():
        def list_placeholders(path=path, allowed=allowed):
            for i, fmt in enumerate(get(path)):
                errors.extend(_placeholders(f"{name(path)}[{i}]", str(fmt), allowed))
        checks.append(list_placeholders)
    for path, (required, optional) in ITEM_KEYS.items():
        def items(path=path, required=required, optional=optional):
            for i, item in enumerate(get(path)):
                where = f"{name(path)}[{i}]"
                if not isinstance(item, dict):
                    errors.append(f"{where}: should be a group of settings, not {item!r}")
                    continue
                errors.extend(f"{where}: missing {k}" for k in sorted(required - item.keys()))
                errors.extend(f"{where}.{k}: unknown setting" for k in sorted(item.keys() - required - optional))
        checks.append(items)

    def city_numbers():
        for i, c in enumerate(get(("geo", "cities"))):
            if isinstance(c, dict) and not all(isinstance(c.get(k, 0), (int, float)) and not isinstance(c.get(k, 0), bool)
                                               for k in ("lat", "lon", "radius_km")):
                errors.append(f"geo.cities[{i}]: lat, lon and radius_km should be numbers")
    checks.append(city_numbers)

    def places():
        from .aprs.places import EVENTS, quiet_minutes

        names = set()
        for i, p in enumerate(get(("aprs", "places"))):
            if not isinstance(p, dict):
                continue
            nums = [p.get(k, 0) for k in ("lat", "lon", "radius_m")]
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in nums):
                errors.append(f"aprs.places[{i}]: lat, lon and radius_m should be numbers")
            elif not (-90 <= p.get("lat", 0) <= 90 and -180 <= p.get("lon", 0) <= 180 and 0 <= p.get("radius_m", 1) <= 100_000):
                errors.append(f"aprs.places[{i}]: lat, lon or radius_m out of range")
            name = str(p.get("name", "")).strip().lower()
            if not name or name in names:
                errors.append(f"aprs.places[{i}].name: {p.get('name')!r} should be a name no other place has")
            names.add(name)
        for i, r in enumerate(get(("alerts", "places"))):
            if not isinstance(r, dict):
                continue
            if str(r.get("place", "")).strip().lower() not in names:
                errors.append(f"alerts.places[{i}].place: {r.get('place')!r} isn't one of Settings → APRS → Places")
            if r.get("on") not in EVENTS:
                errors.append(f"alerts.places[{i}].on: {r.get('on')!r} should be arrive, leave or both")
            try:
                quiet_minutes(r.get("quiet") or "")
            except ValueError:
                errors.append(f"alerts.places[{i}].quiet: {r.get('quiet')!r} should be like 22:00-07:00, or empty")
    checks.append(places)

    def regexes():
        patterns = [(f"standard_tags.camera.albums[{i}].match", r.get("match", ""))
                     for i, r in enumerate(get(("standard_tags", "camera", "albums"))) if isinstance(r, dict)]
        for where, pat in patterns:
            try:
                re.compile(pat)
            except (re.error, TypeError) as e:
                errors.append(f"{where}: not a valid regular expression ({e})")
    checks.append(regexes)

    def tag_lists():
        for kind in ("always_add", "always_remove"):
            for i, t in enumerate(get(("tags", kind))):
                if not isinstance(t, str) or not t.strip():
                    errors.append(f"tags.{kind}[{i}]: should be a tag, not {t!r}")
    checks.append(tag_lists)

    def callsigns():
        from .aprs.stream import CALLSIGN
        for i, c in enumerate(get(("aprs", "callsigns"))):
            if not isinstance(c, str) or not CALLSIGN.match(c.strip().upper()):
                errors.append(f"aprs.callsigns[{i}]: {c!r} isn't a callsign (like KJ4TFD or KJ4TFD-10)")
    checks.append(callsigns)

    def cwop():
        from .aprs.cwop import one_callsign
        from .aprs.stream import CALLSIGN
        seen = {}
        for name, c in (get(("aprs", "cwop")) or {}).items():
            if not CALLSIGN.match(one_callsign(c)):
                errors.append(f"aprs.cwop.{name}: {c!r} isn't one callsign-SSID (like KK6GPV-13)")
            elif one_callsign(c) in seen:
                errors.append(f"aprs.cwop.{name}: {one_callsign(c)} is {seen[one_callsign(c)]}'s too: each station needs its own SSID")
            else:
                seen[one_callsign(c)] = name
        code = get(("aprs", "passcode"))
        if code not in (None, "") and not str(code).strip().isdigit():
            errors.append("aprs.passcode: an APRS-IS passcode is a number")
    checks.append(cwop)

    def igate():
        from .aprs.stream import CALLSIGN
        gate = get(("aprs", "igate")).strip()
        if gate and not CALLSIGN.match(gate.upper()):
            errors.append(f"aprs.igate: {gate!r} isn't a callsign (like WA5DTR-15), or leave it empty")
    checks.append(igate)

    def color_stops(name, key, top, unit, example):
        stops = get(("theme", name))
        ok = isinstance(stops, list) and len(stops) >= 2 and all(
            isinstance(s, dict) and set(s) == {key, "color"} and isinstance(s[key], (int, float)) and 0 <= s[key] <= top
            and isinstance(s["color"], str) and re.fullmatch(r"#[0-9a-fA-F]{6}", s["color"]) for s in stops)
        if not ok:
            errors.append(f"theme.{name}: should be at least two stops like {example} ({unit} 0-{top})")
        elif len({st[key] for st in stops}) < len(stops):
            errors.append(f"theme.{name}: two stops at the same {unit}; give each its own")
    checks.append(lambda: color_stops("radar_colors", "dbz", 90, "dBZ", '{dbz: 20, color: "#2fa84f"}'))
    checks.append(lambda: color_stops("lightning_colors", "min", 15, "minute", '{min: 5, color: "#ffb020"}'))

    def github_repo():
        repo = get(("github", "repo")).strip()
        if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            errors.append(f"github.repo: {repo!r} should be owner/name (like areed145/coconut-barometer)")
    checks.append(github_repo)

    def igate_location():
        from .aprs.igate import parse_location
        try:
            parse_location(get(("aprs", "igate_location")))
        except ValueError:
            errors.append(f"aprs.igate_location: {get(('aprs', 'igate_location'))!r} should be \"lat, lon\" "
                          "(like 30.27, -97.74) or a grid locator (like EM10dg), or empty")
    checks.append(igate_location)

    def categories():
        for cat, c in get(("vision", "categories")).items():
            if not isinstance(c, dict):
                errors.append(f"vision.categories.{cat}: should be a group of settings")
                continue
            if not str(c.get("description") or "").strip():
                errors.append(f"vision.categories.{cat}: needs a description (it's what the model reads)")
            for k in sorted(c.keys() - {"album", "description", "tags"}):
                close = difflib.get_close_matches(k, ["album", "description", "tags"], n=1)
                errors.append(f"vision.categories.{cat}.{k}: unknown setting" + (f" (did you mean {close[0]}?)" if close else ""))
            if not c.get("album") and not c.get("tags"):
                warnings.append(f"vision.categories.{cat}: has neither album nor tags, so it changes nothing")
    checks.append(categories)

    def colors():
        for path in sorted(COLORS):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", get(path)):
                errors.append(f"{name(path)}: should be a color like #1dab45, not {get(path)!r}")
        key = get(("map", "carto_key"))
        if key and not re.fullmatch(r"[A-Za-z0-9._~-]{8,200}", key):
            errors.append("map.carto_key: should be the key from CARTO's dashboard (letters, digits, - _ . ~)")
    checks.append(colors)

    def credentials():
        if get(("vision", "provider")) != "ollama" and not get(("vision", "api_key")):
            warnings.append("vision.api_key: empty; classify/process will stop until it's set")
        if not get(("flickr", "api_key")) or not get(("flickr", "api_secret")):
            warnings.append("flickr.api_key / api_secret: empty; nothing can reach Flickr until they're set")
    checks.append(credentials)
    each(checks)


def _at(cfg: dict, path: tuple):
    for k in path:
        cfg = cfg[k]
    return cfg


def _placeholders(where: str, text: str, allowed: set[str]) -> list[str]:
    try:
        fields = {f.split(".")[0].split("[")[0] for _, f, _, _ in string.Formatter().parse(text) if f is not None}
    except ValueError as e:
        return [f"{where}: {e} (use {{{{ and }}}} for literal braces)"]
    bad = sorted(fields - allowed)
    if "" in fields:
        bad = sorted((fields - {""}) - allowed) + ["{} (empty)"]
    return [f"{where}: unknown placeholder {', '.join('{' + b + '}' if not b.startswith('{') else b for b in bad)}; "
            f"available: {', '.join('{' + a + '}' for a in sorted(allowed))}"] if bad else []


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def data_dir(environ=os.environ) -> Path | None:
    """DATA_DIR: where a server keeps everything that must persist (on Render,
    the disk's mount path): the database, the Flickr login, the rate-limit file, backups."""
    d = environ.get("DATA_DIR")
    return Path(os.path.expanduser(d)) if d else None


def database_path(cfg_path: str | os.PathLike, raw: dict, environ=os.environ) -> str:
    """DATABASE_PATH, else config.yaml's `database`, else the data directory's,
    else the default; relative paths are relative to config.yaml's folder."""
    db = environ.get("DATABASE_PATH") or raw.get("database")
    if not db and data_dir(environ):
        return str(data_dir(environ) / DEFAULTS["database"])
    db = db or DEFAULTS["database"]
    path = Path(db) if os.path.isabs(db) else Path(cfg_path).resolve().parent / db
    return str(path)


def read_raw(path: str | os.PathLike) -> dict:
    """The YAML exactly as written (no defaults). It's optional: without it, everything comes from the
    defaults, Settings (in the database) and the environment."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{p} isn't valid YAML: {e}")
    if not isinstance(raw, dict):
        raise SystemExit(f"{p}: expected settings (key: value), got {type(raw).__name__}")
    return retire(raw)


def load_config(path: str | os.PathLike, admin: bool = True) -> dict:
    """DEFAULTS < config.yaml < admin-page settings (in the database) < environment.
    admin=False skips the database layer (it's only read if the database exists)."""
    p = Path(path)
    file_raw = read_raw(p)
    raw = apply_env(file_raw)
    if admin:
        from .settings import _store_for, layer

        store = _store_for(p, file_raw)
        if store is not None:
            raw, _sources, problems = layer(file_raw, store)
            store.close()
            for problem in problems:
                logger.warning("%s", problem)
    cfg = _merge(DEFAULTS, raw)
    errors, _warnings = validate(raw, cfg)
    if errors:
        raise SystemExit(f"{p} has {len(errors)} problem(s):\n" + "\n".join(f"  - {e}" for e in errors)
                         + "\nRun `python -m coconut_barometer check` for details.")
    cfg["database"] = database_path(p, raw)
    return cfg


def apply_env(raw: dict, environ=os.environ) -> dict:
    """Environment variables (ENV_OVERRIDES) win over the file. Empty ones are ignored."""
    raw = copy.deepcopy(raw)
    for var, path in ENV_OVERRIDES.items():
        value = environ.get(var)
        if not value:
            continue
        node = raw
        for key in path[:-1]:
            node = node.setdefault(key, {}) if isinstance(node.get(key), dict) or key not in node else node[key]
        node[path[-1]] = value
    return apply_platform(raw, environ)


def apply_platform(raw: dict, environ=os.environ) -> dict:
    """Hosting platforms set their own variables. On Render: listen on 0.0.0.0:$PORT behind
    its TLS proxy (trusted for X-Forwarded-*; it puts the real client address first), and
    accept the service's own host name. ALLOWED_HOSTS adds more (comma-separated,
    e.g. your custom domain). Secure cookies stay on."""
    web = raw.setdefault("web", {}) if isinstance(raw.get("web", {}), dict) else raw["web"]
    if not isinstance(web, dict):
        return raw
    if environ.get("PORT", "").isdigit():
        web["port"] = int(environ["PORT"])
    extra = [h.strip() for h in environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
    if environ.get("RENDER_EXTERNAL_HOSTNAME"):
        extra.append(environ["RENDER_EXTERNAL_HOSTNAME"])
    if extra:
        hosts = list(web.get("allowed_hosts", DEFAULTS["web"]["allowed_hosts"]))
        web["allowed_hosts"] = hosts + [h for h in extra if h not in hosts]
    if environ.get("RENDER"):
        web["host"] = "0.0.0.0"
        web["trusted_proxies"] = ["*"]
        web["secure_cookies"] = True
    if not web:
        raw.pop("web")
    return raw


PLATFORM_ENV = {"web.port": "PORT", "web.allowed_hosts": "ALLOWED_HOSTS / RENDER_EXTERNAL_HOSTNAME",
                "web.host": "RENDER", "web.trusted_proxies": "RENDER", "web.secure_cookies": "RENDER"}


def platform_paths(environ=os.environ) -> dict[str, str]:
    """Settings the platform's environment is setting right now (for the admin page)."""
    on = {"web.port": environ.get("PORT", "").isdigit(),
          "web.allowed_hosts": bool(environ.get("ALLOWED_HOSTS") or environ.get("RENDER_EXTERNAL_HOSTNAME")),
          "web.host": bool(environ.get("RENDER")), "web.trusted_proxies": bool(environ.get("RENDER")),
          "web.secure_cookies": bool(environ.get("RENDER"))}
    return {p: PLATFORM_ENV[p] for p, yes in on.items() if yes}


def from_dict(raw: dict) -> dict:
    """Build a config from a dict (used by tests)."""
    return _merge(DEFAULTS, retire(raw))
