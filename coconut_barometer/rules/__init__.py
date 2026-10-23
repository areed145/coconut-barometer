"""The rules that propose changes, one module each. To add one: write propose(ctx)
returning AlbumWants/TagWants (resolved together, first want wins) or Changes, and
list it below. Order matters for wants: earlier rules take priority, and when a photo
would go over Flickr's tag limit the tags from later rules are dropped first."""
from __future__ import annotations

from . import conventions, descriptions, geotag, location, subjects, titles
from .base import AlbumWant, Context, TagWant, resolve

WANT_RULES = [location, subjects, conventions]   # albums and tags, in priority order
CHANGE_RULES = [titles, descriptions, geotag]     # return Changes directly

__all__ = ["Context", "AlbumWant", "TagWant", "resolve", "WANT_RULES", "CHANGE_RULES"]
