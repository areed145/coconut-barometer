from __future__ import annotations

import html
import re
import unicodedata


def norm_title(s: str) -> str:
    """Normalize an album title for matching: case/accents/punctuation-insensitive."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


def norm_tag(s: str) -> str:
    """Flickr's normalized tag form: lowercase, no spaces/punctuation."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9:=]+", "", s.casefold())


def clean_tag(s: str) -> str:
    """Tag as we want to write it (lowercase, single spaces)."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def quote_tags(tags: list[str]) -> str:
    """Flickr addTags takes space-separated tags; multi-word ones must be quoted."""
    return " ".join(f'"{t}"' if " " in t else t for t in tags)


def same_text(a: str | None, b: str | None) -> bool:
    """Equal once Flickr's HTML escaping (&quot; &amp; …) and whitespace are ignored."""
    clean = lambda t: " ".join(html.unescape(t or "").split())
    return clean(a) == clean(b)
