"""Turn what's known about each photo (geotag, vision results, EXIF, albums) into
proposed Changes, by running the rules in coconut_barometer/rules/.

Proposals only ever add, apart from titles and descriptions, and tags when you process a
photo with "overwrite".
Anything already true on Flickr is skipped.
"""
from __future__ import annotations

from collections import Counter

from .changes import Change
from .rules import CHANGE_RULES, WANT_RULES, Context, resolve


def photo_page(nsid: str, photo_id: str) -> str:
    return f"https://www.flickr.com/photos/{nsid}/{photo_id}"


def build_plan(cfg: dict, store, provider_key: str | None, geocoder=None,
               photo_ids: set[str] | None = None, tag_mode: str = "add") -> list[Change]:
    """photo_ids: plan only these photos (the per-photo queue); default is the whole library.
    tag_mode: "add" to the photo's tags, or "replace" them (see rules.resolve)."""
    ctx = Context(cfg, store, provider_key, geocoder=geocoder, photo_ids=photo_ids)
    changes = resolve(ctx, [w for rule in WANT_RULES for w in rule.propose(ctx)], tag_mode)
    for rule in CHANGE_RULES:
        changes += rule.propose(ctx)
    changes.sort(key=lambda c: (c.action, c.new.lower() if c.action == "add_to_album" else "", c.photo_id))
    return changes


def summarize(changes: list[Change]) -> Counter:
    """How many of each: an album's title, or tags / renames / descriptions."""
    label = {"add_tags": "tags", "set_tags": "tag replacements", "set_title": "titles", "set_description": "descriptions"}
    return Counter(label.get(c.action, c.new) for c in changes)
