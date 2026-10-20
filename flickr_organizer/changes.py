"""What a proposed change is, and when one counts as settled.

Change is the one shape a proposal has everywhere: the planner makes them, the queue
stores them, the review tool shows and edits them, and apply carries them out.

Settled is the one place that decides whether a change was already dealt with, so it
isn't proposed again. Failed changes never count: they're proposed again next time.

  add_to_album      settled per (photo, album) once applied or rejected
  add_tags          settled per (photo, tag) once applied or rejected; dropping a tag
                    in review rejects just that tag
  set_title         settled per (photo, proposed title) - a different proposal (after you
                    change the title brief) can still come; the planner only proposes
                    while the current title is replaceable, so an applied title ends it
  set_description   blank descriptions: one proposal per photo, ever (applied or rejected)
                    re-wording (describe.reword): a description this tool wrote and you
                    haven't edited gets one new proposal per brief change - its new text
                    must be newer than when the old one was applied

Statuses in the decisions table: applied (written to Flickr, recorded against the text
written), rejected, failed, and edited - the proposal you changed before approving it
(the text actually written is recorded as applied). A rejection is recorded against the
proposal even if you'd edited it. applied/rejected/edited all count as settled.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .util import clean_tag, same_text

ACTIONS = ("add_to_album", "add_tags", "set_title", "set_description")
META_ACTIONS = ("set_title", "set_description")   # both written by one flickr.photos.setMeta
SETTLED = ("applied", "rejected", "edited")


@dataclass
class Change:
    action: str                 # one of ACTIONS
    photo_id: str
    new: str | list[str]        # album title | tags | proposed title | proposed description
    old: str = ""               # title/description it replaces ("" when blank); else unused
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
        return list(self.new) if self.action == "add_tags" else []

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


class Settled:
    """Answers "was this already dealt with?" from the decisions table. Build one per plan."""

    def __init__(self, store):
        self.store = store
        self.done = {k: s for k, s in store.decisions().items() if s in SETTLED}
        self.described = {pid for (pid, action, _) in self.done if action == "set_description"}
        self._written: dict[str, tuple[str, int]] | None = None

    def album(self, photo_id: str, title: str) -> bool:
        return (photo_id, "add_to_album", title) in self.done

    def tag(self, photo_id: str, tag: str) -> bool:
        return (photo_id, "add_tag", tag) in self.done

    def title(self, photo_id: str, title: str) -> bool:
        return (photo_id, "set_title", title) in self.done

    def blank_description(self, photo_id: str) -> bool:
        return photo_id in self.described

    def may_reword(self, photo_id: str, current: str, proposal: str, written_at_least: int) -> bool:
        """A description this tool wrote, still as written, re-worded by a newer proposal."""
        if self._written is None:
            self._written = self.store.applied("set_description")
        ours = self._written.get(photo_id)
        return bool(ours and same_text(ours[0], current) and not same_text(proposal, current)
                    and written_at_least > ours[1] and (photo_id, "set_description", proposal) not in self.done)
