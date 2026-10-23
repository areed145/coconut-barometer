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


def tag_matcher(patterns: list[str]):
    """A test of a tag against these, both as Flickr normalizes them; * and ? are wildcards (www*)."""
    import fnmatch

    def norm(p: str) -> str:
        p = unicodedata.normalize("NFKD", p or "")
        return re.sub(r"[^a-z0-9:=*?]+", "", "".join(c for c in p if not unicodedata.combining(c)).casefold())
    pats = [p for p in map(norm, patterns) if p.strip("*?")]
    return lambda tag: any(fnmatch.fnmatchcase(norm_tag(tag), p) for p in pats)


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
