"""Config loading: YAML + defaults."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "flickr": {"api_key": None, "api_secret": None, "calls_per_second": 0.9},
    "database": "flickr_organizer.db",
    "output_dir": "plans",
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

def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Config not found: {p}. Copy config.example.yaml to config.yaml first.")
    raw = yaml.safe_load(p.read_text()) or {}
    cfg = _merge(DEFAULTS, raw)
    # Resolve relative paths against the config file's folder.
    base = p.resolve().parent
    for key in ("database", "output_dir"):
        if not os.path.isabs(cfg[key]):
            cfg[key] = str(base / cfg[key])
    return cfg


def from_dict(raw: dict) -> dict:
    """Build a config from a dict (used by tests)."""
    return _merge(DEFAULTS, raw)
