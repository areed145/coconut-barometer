"""Undoing an apply: what each written change replaced is kept (store.record_applied), and undo
puts it back, newest apply first. Like apply, it never overwrites what changed on Flickr since:
the photo is re-read first, and a field that no longer holds what was written is left alone
(and said so).

  set_title / set_description   the text it replaced (one setMeta for both)
  set_tags / add_tags           the tags the photo had before (setTags)
  add_to_album                  out of the album again; an album made for it, holding only it, is deleted
  set_location                  the location removed again
  add_to_group                  out of the group's pool again
"""
from __future__ import annotations

from .changes import META_ACTIONS, TAG_ACTIONS, Change, split_tags
from .flickr_client import FlickrCallError
from .sync import refresh_photo
from .util import norm_tag, quote_tags, same_text


def entries(store, changes: list[Change], tags_before: str) -> list[dict]:
    """What to keep for undoing these changes, all applied. tags_before: the photo's tags then."""
    out = []
    for c in changes:
        if c.action in META_ACTIONS:
            out.append({"action": c.action, "old": c.old, "new": str(c.value).strip()})
        elif c.action in TAG_ACTIONS:
            written = split_tags(c.value)
            before = c.old if c.action == "set_tags" else tags_before
            after = written if c.action == "set_tags" else before.split() + written
            out.append({"action": c.action, "old": before, "new": [norm_tag(t) for t in after], "written": written})
        elif c.action == "add_to_album":
            out.append({"action": c.action, "new": c.new, "album_id": c.album_id or store.album_id(c.new) or "",
                        "created": not c.album_id})
        elif c.action == "set_location":
            out.append({"action": c.action, "new": c.target})
        elif c.action == "add_to_group":
            out.append({"action": c.action, "new": c.new, "group_id": c.album_id})
    return out


def describe(e: dict) -> str:
    """One line for the Applied list."""
    a = e["action"]
    if a == "set_title":
        return f"title “{e['new']}”"
    if a == "set_description":
        return "description"
    if a in TAG_ACTIONS:
        return f"{len(e['new'])} tags"
    if a == "add_to_album":
        return f"album {e['new']}"
    if a == "add_to_group":
        return f"group {e['new']}"
    return "location"


def undo(client, store, photo_id: str, changes: list[dict], log=print) -> dict:
    """Put back what these changes replaced. Returns {"undone": [...], "skipped": [why, ...]}."""
    done, skipped = [], []
    refresh_photo(client, store, photo_id)
    p = store.photo(photo_id)
    if p is None:
        return {"undone": [], "skipped": ["the photo is gone from Flickr"]}

    meta = {e["action"]: e for e in changes if e["action"] in META_ACTIONS}
    if meta:
        fields = {"set_title": p["title"] or "", "set_description": p["description"] or ""}
        changed = False
        for action, e in meta.items():
            if same_text(fields[action], e["new"]):
                fields[action], changed = e["old"], True
            else:
                skipped.append(f"{action.removeprefix('set_')} changed on Flickr since; left as it is")
        if changed:
            client.call("flickr.photos.setMeta", photo_id=photo_id, title=fields["set_title"],
                        description=fields["set_description"])
            store.set_title_local(photo_id, fields["set_title"])
            store.set_description_local(photo_id, fields["set_description"])
            done += [a for a, e in meta.items() if fields[a] == e["old"]]

    tag = next((e for e in reversed(changes) if e["action"] in TAG_ACTIONS), None)
    if tag:
        now = {norm_tag(t) for t in (p["tags"] or "").split()}
        if now == set(tag["new"]):
            before = tag["old"].split()
            client.call("flickr.photos.setTags", photo_id=photo_id, tags=quote_tags(before))
            store.set_tags_local(photo_id, before)
            done.append(tag["action"])
        else:
            skipped.append("tags changed on Flickr since; left as they are")

    for e in changes:
        try:
            if e["action"] == "add_to_album" and e["album_id"]:
                members = sum(1 for a in store.memberships().values() if e["album_id"] in a)
                if e["created"] and members <= 1:     # made for this photo, and still only it: gone with it
                    client.call("flickr.photosets.delete", photoset_id=e["album_id"])
                    store.delete_albums([e["album_id"]])
                else:
                    client.call("flickr.photosets.removePhoto", photoset_id=e["album_id"], photo_id=photo_id)
                    store.remove_membership(e["album_id"], photo_id)
                done.append(e["action"])
            elif e["action"] == "set_location":
                if p["lat"] is None:
                    skipped.append("location already removed")
                    continue
                client.call("flickr.photos.geo.removeLocation", photo_id=photo_id)
                store.set_location_local(photo_id, None, None, None)
                done.append(e["action"])
            elif e["action"] == "add_to_group":
                client.call("flickr.groups.pools.remove", group_id=e["group_id"], photo_id=photo_id)
                store.remove_group_photo(e["group_id"], photo_id)
                done.append(e["action"])
        except FlickrCallError as err:
            skipped.append(f"{describe(e)}: {err}")
            log(f"  ! undo {photo_id} {e['action']}: {err}")

    for e in changes:                                  # the decisions log, for Analytics
        if e["action"] in done:
            for target in _targets(e):
                store.record(photo_id, *target, "undone")
    return {"undone": done, "skipped": skipped}


def _targets(e: dict) -> list[tuple[str, str]]:
    """The decisions-log keys this change was recorded under when applied."""
    if e["action"] in TAG_ACTIONS:
        return [("add_tag", t) for t in e["written"]]
    return [(e["action"], e["new"])]
