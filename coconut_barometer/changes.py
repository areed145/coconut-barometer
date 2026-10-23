"""What a proposed change is.

Change is the one shape a proposal has everywhere: the planner makes them, the queue
stores them, the review tool shows and edits them, and apply carries them out:

  add_to_album      one album
  add_tags          tags the photo doesn't have yet
  set_tags          (processing with "overwrite") the photo's whole tag list, replaced
  set_title         the title
  set_description   the description

Every processing proposes afresh: nothing you applied or ignored before holds a proposal
back (only what's already true on Flickr does). The decisions table is a log of what
apply did, for Analytics: applied (recorded against the text written, and per tag as
add_tag / remove_tag) or failed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .util import clean_tag, same_text

ACTIONS = ("add_to_album", "add_tags", "set_tags", "set_title", "set_description")
TAG_ACTIONS = ("add_tags", "set_tags")
META_ACTIONS = ("set_title", "set_description")   # both written by one flickr.photos.setMeta


@dataclass
class Change:
    action: str                 # one of ACTIONS
    photo_id: str
    new: str | list[str]        # album title | tags | proposed title | proposed description
    old: str = ""               # title/description it replaces ("" when blank); set_tags: the tags it replaces
    album_id: str = ""          # add_to_album: "" means the album will be created
    source: str = ""            # geo:country, vision:food, std:date, vision:title, ...
    confidence: str = ""
    reason: str = ""
    # Set in review:
    approved: bool = True
    edited: str | list[str] | None = None   # your version: edited text, or the tags you kept

    @property
    def value(self) -> str | list[str]:
        """What gets written: your edit if you made one, else the proposal."""
        return self.new if self.edited is None else self.edited

    @property
    def tags(self) -> list[str]:
        return list(self.new) if self.action in TAG_ACTIONS else []

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Change":
        if "new" in d:
            return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        # Queue rows written before Change existed: target / original_target / approve.
        action, target, original = d["action"], d.get("target") or "", d.get("original_target") or ""
        if action == "add_tags":
            new: str | list[str] = split_tags(original or target)
            kept = split_tags(target)
            edited = kept if kept != new else None
            old = ""
        else:
            new, edited = target, None
            # set_description rows once stored the new text itself here when the old one was blank.
            old = "" if action == "set_description" and same_text(original, target) else original
        return cls(action=action, photo_id=d["photo_id"], new=new, old=old, album_id=d.get("album_id", ""),
                   source=d.get("source", ""), confidence=d.get("confidence", ""), reason=d.get("reason", ""),
                   approved=(d.get("approve", "y") or "").strip().lower() not in ("n", "no", "0", "false"),
                   edited=edited)


def split_tags(text: str | list[str]) -> list[str]:
    if isinstance(text, list):
        return [clean_tag(t) for t in text if clean_tag(t)]
    return [clean_tag(t) for t in (text or "").split("|") if clean_tag(t)]

