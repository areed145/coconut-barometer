"""Config loading: YAML + defaults, and validation.

DEFAULTS is the single source of every setting's default; code reads cfg[...] directly.
load_config refuses a config with errors (unknown key, wrong type, bad choice, number out
of range, unknown placeholder, broken regex). `python -m flickr_organizer check` also
lists warnings and checks the config against your library.
"""
from __future__ import annotations

import copy
import difflib
import os
import re
import string
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "flickr": {"api_key": None, "api_secret": None, "calls_per_second": 0.9},
    "database": "flickr_organizer.db",
    "albums": {"create_missing": False},
    "geo": {
        "enabled": True,
        "min_accuracy": {"country": 1, "state": 6, "city": 11},
        "countries": {"auto_match": True, "aliases": {}},
        "us_states": {"enabled": True, "title": "{state}"},
        "cities": [],
        "add_tags": False,
    },
    "rename": {
        "enabled": False,
        "min_accuracy": 11,
        "us_format": "{city}, {state_abbr}",
        "intl_format": "{city}, {country}",
        "fallback_to_region": False,
        "us_region_format": "{state}",
        "intl_region_format": "{region}, {country}",
        "generic_patterns": None,
        "extra_generic_patterns": [],
    },
    "vision": {
        "enabled": True,
        "provider": "claude",
        "model": "claude-haiku-5-5",
        "api_key": None,
        "base_url": None,
        "thumbnail": "url_m",
        "concurrency": 4,
        "min_confidence": 0.75,
        "use_photo_text": True,
        "use_location": True,
        "include_videos": False,
        "extra_tags": {"enabled": True, "max": 6, "prefix": ""},
        "describe": {"enabled": False, "max_words": 40, "instructions": None, "reword": True},
        "title": {"enabled": False, "max_words": 8, "replace": "generic_or_place", "instructions": None},
        "categories": {},
    },
    "standard_tags": {
        "max_tags_per_photo": 75,   # Flickr's limit
        "date": {"enabled": False, "formats": ["y{year}", "m{month:02d}", "d{day:02d}"], "skip_if_dated": True},
        "place": {"enabled": False, "country": True, "regions": True, "state": True, "state_abbr": True,
                  "city": True, "split_words": False, "extra": {}, "region_overrides": {}},
        "camera": {"enabled": False, "name_words": True, "rules": [], "albums": []},
        "album_tags": {"enabled": False, "from_categories": True, "albums": {}},
        "coords": {"enabled": False, "formats": ["lat{lat}", "lon{lon}"], "min_accuracy": 6},
        "owner": {"enabled": False, "tags": []},
        "filler": {"enabled": False, "tags": []},
    },
}

# Settings whose keys are yours (album titles, country codes, category names...).
FREE_MAPS = {("geo", "countries", "aliases"), ("vision", "categories"), ("standard_tags", "place", "extra"),
             ("standard_tags", "place", "region_overrides"), ("standard_tags", "album_tags", "albums")}
# Settings whose default is None but that take a value.
OPTIONAL = {("flickr", "api_key"): str, ("flickr", "api_secret"): str, ("vision", "api_key"): str,
            ("vision", "base_url"): str, ("vision", "describe", "instructions"): str,
            ("vision", "title", "instructions"): str, ("rename", "generic_patterns"): list}
CHOICES = {("vision", "provider"): ("claude", "anthropic", "openai", "ollama"),
           ("vision", "thumbnail"): ("url_q", "url_n", "url_m", "url_z"),
           ("vision", "title", "replace"): ("generic", "generic_or_place", "all")}
RANGES = {("flickr", "calls_per_second"): (0.01, None), ("vision", "concurrency"): (1, 32),
          ("vision", "min_confidence"): (0, 1), ("vision", "extra_tags", "max"): (0, 20),
          ("vision", "describe", "max_words"): (5, 200), ("vision", "title", "max_words"): (2, 20),
          ("standard_tags", "max_tags_per_photo"): (1, 75), ("rename", "min_accuracy"): (1, 16),
          ("standard_tags", "coords", "min_accuracy"): (1, 16)}
PLACEHOLDERS = {
    ("vision", "describe", "instructions"): {"max_words"},
    ("vision", "title", "instructions"): {"max_words"},
    ("geo", "us_states", "title"): {"state"},
    ("rename", "us_format"): {"city", "state", "state_abbr", "region", "country", "cc"},
    ("rename", "intl_format"): {"city", "state", "state_abbr", "region", "country", "cc"},
    ("rename", "us_region_format"): {"state", "state_abbr", "region", "country", "cc"},
    ("rename", "intl_region_format"): {"state", "state_abbr", "region", "country", "cc"},
}
LIST_PLACEHOLDERS = {("standard_tags", "date", "formats"): {"year", "month", "day", "season"},
                     ("standard_tags", "coords", "formats"): {"lat", "lon"}}
ITEM_KEYS = {("geo", "cities"): ({"album", "lat", "lon"}, {"radius_km", "name", "tag"}),
             ("standard_tags", "camera", "rules"): ({"match"}, {"tags"}),
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

    def rate():
        if get(("flickr", "calls_per_second")) > 1:
            warnings.append("flickr.calls_per_second: above 1/s goes over Flickr's 3600 calls/hour limit")
    checks.append(rate)
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

    def regexes():
        patterns = [("rename.extra_generic_patterns", p) for p in get(("rename", "extra_generic_patterns"))]
        patterns += [("rename.generic_patterns", p) for p in get(("rename", "generic_patterns")) or []]
        patterns += [(f"standard_tags.camera.{kind}[{i}].match", r.get("match", ""))
                     for kind in ("rules", "albums") for i, r in enumerate(get(("standard_tags", "camera", kind)))
                     if isinstance(r, dict)]
        for where, pat in patterns:
            try:
                re.compile(pat)
            except (re.error, TypeError) as e:
                errors.append(f"{where}: not a valid regular expression ({e})")
    checks.append(regexes)

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

    def credentials():
        if get(("vision", "enabled")) and get(("vision", "provider")) != "ollama" and not get(("vision", "api_key")):
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


def read_raw(path: str | os.PathLike) -> dict:
    """The YAML exactly as written (no defaults)."""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Config not found: {p}. Copy config.example.yaml to config.yaml first.")
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{p} isn't valid YAML: {e}")
    if not isinstance(raw, dict):
        raise SystemExit(f"{p}: expected settings (key: value), got {type(raw).__name__}")
    return raw


def load_config(path: str | os.PathLike) -> dict:
    p = Path(path)
    raw = read_raw(p)
    cfg = _merge(DEFAULTS, raw)
    errors, _warnings = validate(raw, cfg)
    if errors:
        raise SystemExit(f"{p} has {len(errors)} problem(s):\n" + "\n".join(f"  - {e}" for e in errors)
                         + "\nRun `python -m flickr_organizer check` for details.")
    # Resolve relative paths against the config file's folder.
    base = p.resolve().parent
    for key in ("database",):
        if not os.path.isabs(cfg[key]):
            cfg[key] = str(base / cfg[key])
    return cfg


def from_dict(raw: dict) -> dict:
    """Build a config from a dict (used by tests)."""
    return _merge(DEFAULTS, raw)
