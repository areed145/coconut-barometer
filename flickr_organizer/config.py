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
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "flickr": {"api_key": None, "api_secret": None, "calls_per_second": 0.9},
    "weatherflow": {
        "token": None,
        "backfill_days": 7,          # history fetched from REST: forward from the newest stored, back this many days
        "calls_per_second": 2.0,
    },
    "weather": {"units": "metric"},  # how the Weather pages show values; always stored metric
    "database": "flickr_organizer.db",
    "albums": {"create_missing": False},
    "geo": {
        "min_accuracy": {"country": 1, "state": 6, "city": 11},
        "countries": {"auto_match": True, "aliases": {}},
        "us_states": {"enabled": True, "title": "{state}"},
        "cities": [],
        "add_tags": False,
    },
    "vision": {
        "provider": "claude",
        "model": "claude-haiku-5-5",
        "api_key": None,
        "base_url": None,
        "thumbnail": "url_m",
        "concurrency": 4,
        "min_confidence": 0.75,
        "use_photo_text": True,
        "use_location": True,
        "extra_tags": {"max": 6, "prefix": ""},
        "describe": {"max_words": 40, "instructions": None},
        "title": {"max_words": 8, "instructions": None},
        "categories": {},
    },
    "schedule": {
        "full_every_days": 7,        # full sync: every photo, album and album's photos (~180 calls)
        "photo_list_every_hours": 24,  # every photo re-read: view counts + the daily snapshot (~22 calls)
        "stats": True,               # per-day stats (Flickr Pro): yesterday, plus backfill
        "backfill_days_per_run": 7,  # older days fetched per run until Flickr has none (~6 calls/day)
        "new_photos": "queue",       # after a sync, new photos: "process" now (vision costs), or "queue" for
                                     # To review, processed when you open them
    },
    "backup": {
        "every_hours": 24,            # the web app's scheduler makes one this often; 0 = off
        "keep": 7,                    # newest kept; older deleted
        "dir": None,                  # default: backups/ next to the database
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
        "color_high": "#1dab45",      # ... to high (log scale)
    },
    "map": {
        "expand_at": 40,              # a cluster this small opens into thumbnails when clicked
        "carto_key": None,            # CARTO basemaps key (carto.com/basemaps/apikey); goes in every tile URL
    },
    "standard_tags": {
        "max_tags_per_photo": 75,   # Flickr's limit
        "date": {"enabled": False, "formats": ["y{year}", "m{month:02d}", "d{day:02d}"], "skip_if_dated": True},
        "place": {"enabled": False, "country": True, "regions": True, "state": True, "state_abbr": True,
                  "city": True, "extra": {}, "region_overrides": {}},
        "camera": {"enabled": False, "name_words": True, "rules": [], "albums": []},
        "album_tags": {"enabled": False, "from_categories": True, "albums": {}},
    },
}

# Settings whose keys are yours (album titles, country codes, category names...).
FREE_MAPS = {("geo", "countries", "aliases"), ("vision", "categories"), ("standard_tags", "place", "extra"),
             ("standard_tags", "place", "region_overrides"), ("standard_tags", "album_tags", "albums")}
# Settings whose default is None but that take a value.
OPTIONAL = {("backup", "dir"): str, ("map", "carto_key"): str, ("flickr", "api_key"): str, ("flickr", "api_secret"): str, ("vision", "api_key"): str, ("weatherflow", "token"): str,
            ("vision", "base_url"): str, ("vision", "describe", "instructions"): str,
            ("vision", "title", "instructions"): str}
CHOICES = {("vision", "provider"): ("claude", "anthropic", "openai", "ollama"),
           ("vision", "thumbnail"): ("url_q", "url_n", "url_m", "url_z"),
           ("schedule", "new_photos"): ("process", "queue"), ("weather", "units"): ("metric", "imperial")}
RANGES = {("flickr", "calls_per_second"): (0.01, None), ("vision", "concurrency"): (1, 32),
          ("vision", "min_confidence"): (0, 1), ("vision", "extra_tags", "max"): (0, 20),
          ("vision", "describe", "max_words"): (5, 200), ("vision", "title", "max_words"): (2, 20),
          ("standard_tags", "max_tags_per_photo"): (1, 75),
          ("schedule", "full_every_days"): (1, 365),
          ("schedule", "photo_list_every_hours"): (1, 24 * 30), ("schedule", "backfill_days_per_run"): (0, 400),
          ("backup", "every_hours"): (0, 24 * 30), ("backup", "keep"): (1, 365),
          ("web", "port"): (1, 65535), ("web", "session_days"): (1, 365), ("web", "sync_every_minutes"): (0, 24 * 60),
          ("map", "expand_at"): (2, 200), ("weatherflow", "backfill_days"): (0, 3650),
          ("weatherflow", "calls_per_second"): (0.1, 10)}
COLORS = {("theme", "color_low"), ("theme", "color_high")}   # #rrggbb

# Secrets can come from the environment instead of config.yaml (on a server, keep them there).
ENV_OVERRIDES = {"FLICKR_API_KEY": ("flickr", "api_key"), "FLICKR_API_SECRET": ("flickr", "api_secret"),
                 "WEATHERFLOW_TOKEN": ("weatherflow", "token"), "ANTHROPIC_API_KEY": ("vision", "api_key"), "VISION_API_KEY": ("vision", "api_key"),
                 "FLICKR_ORGANIZER_DATABASE": ("database",)}
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
           ("standard_tags", "filler")}


# Settings that moved: old path -> new path (the old one is used if the new one isn't set).
RENAMED = {"map.color_low": "theme.color_low", "map.color_high": "theme.color_high"}


def retire(raw: dict) -> dict:
    """raw without RETIRED settings (a copy), with RENAMED ones moved. web.process_new: true
    becomes schedule.new_photos: process."""
    import copy

    raw = copy.deepcopy(raw)
    for old, new in RENAMED.items():
        o, n = old.split("."), new.split(".")
        value = (raw.get(o[0]) or {}).pop(o[1], None) if isinstance(raw.get(o[0]), dict) else None
        if value is not None and n[1] not in (raw.get(n[0]) or {}):
            raw.setdefault(n[0], {})[n[1]] = value
    if (raw.get("web") or {}).get("process_new") is True and "new_photos" not in (raw.get("schedule") or {}):
        raw.setdefault("schedule", {})["new_photos"] = "process"
    for path in RETIRED:
        node = raw
        for k in path[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return raw


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
        patterns = [(f"standard_tags.camera.{kind}[{i}].match", r.get("match", ""))
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

    def colors():
        for path in sorted(COLORS):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", get(path)):
                errors.append(f"{name(path)}: should be a colour like #1dab45, not {get(path)!r}")
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
    """FLICKR_ORGANIZER_DATA: where a server keeps everything that must persist (on Render,
    the disk's mount path): the database, the Flickr login, the rate-limit file, backups."""
    d = environ.get("FLICKR_ORGANIZER_DATA")
    return Path(os.path.expanduser(d)) if d else None


def database_path(cfg_path: str | os.PathLike, raw: dict, environ=os.environ) -> str:
    """FLICKR_ORGANIZER_DATABASE, else config.yaml's `database`, else the data directory's,
    else the default; relative paths are relative to config.yaml's folder."""
    db = environ.get("FLICKR_ORGANIZER_DATABASE") or raw.get("database")
    if not db and data_dir(environ):
        return str(data_dir(environ) / DEFAULTS["database"])
    db = db or DEFAULTS["database"]
    return db if os.path.isabs(db) else str(Path(cfg_path).resolve().parent / db)


def read_raw(path: str | os.PathLike) -> dict:
    """The YAML exactly as written (no defaults). On a server (FLICKR_ORGANIZER_DATA set),
    config.yaml is optional: everything else comes from the admin page and the environment."""
    p = Path(path)
    if not p.exists():
        if data_dir():
            return {}
        raise SystemExit(f"Config not found: {p}. Copy config.example.yaml to config.yaml first.")
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
                print(f"warning: {problem}", file=sys.stderr)
    cfg = _merge(DEFAULTS, raw)
    errors, _warnings = validate(raw, cfg)
    if errors:
        raise SystemExit(f"{p} has {len(errors)} problem(s):\n" + "\n".join(f"  - {e}" for e in errors)
                         + "\nRun `python -m flickr_organizer check` for details.")
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
    accept the service's own host name. FLICKR_ORGANIZER_ALLOWED_HOSTS adds more (comma-separated,
    e.g. your custom domain). Secure cookies stay on."""
    web = raw.setdefault("web", {}) if isinstance(raw.get("web", {}), dict) else raw["web"]
    if not isinstance(web, dict):
        return raw
    if environ.get("PORT", "").isdigit():
        web["port"] = int(environ["PORT"])
    extra = [h.strip() for h in environ.get("FLICKR_ORGANIZER_ALLOWED_HOSTS", "").split(",") if h.strip()]
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


PLATFORM_ENV = {"web.port": "PORT", "web.allowed_hosts": "FLICKR_ORGANIZER_ALLOWED_HOSTS / RENDER_EXTERNAL_HOSTNAME",
                "web.host": "RENDER", "web.trusted_proxies": "RENDER", "web.secure_cookies": "RENDER"}


def platform_paths(environ=os.environ) -> dict[str, str]:
    """Settings the platform's environment is setting right now (for the admin page)."""
    on = {"web.port": environ.get("PORT", "").isdigit(),
          "web.allowed_hosts": bool(environ.get("FLICKR_ORGANIZER_ALLOWED_HOSTS") or environ.get("RENDER_EXTERNAL_HOSTNAME")),
          "web.host": bool(environ.get("RENDER")), "web.trusted_proxies": bool(environ.get("RENDER")),
          "web.secure_cookies": bool(environ.get("RENDER"))}
    return {p: PLATFORM_ENV[p] for p, yes in on.items() if yes}


def from_dict(raw: dict) -> dict:
    """Build a config from a dict (used by tests)."""
    return _merge(DEFAULTS, retire(raw))
