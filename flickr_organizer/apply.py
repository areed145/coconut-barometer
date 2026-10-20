"""Carry out reviewed Changes on Flickr and record what happened (see changes.py)."""
from __future__ import annotations

from .changes import META_ACTIONS, TAG_ACTIONS, Change, split_tags
from .flickr_client import FlickrCallError
from .sync import refresh_photo
from .util import norm_tag, norm_title, quote_tags, same_text

# photosets.addPhoto error 3 = "Photo already in set" → treat as success.
ALREADY_IN_SET = 3


def apply_changes(client, store, changes: list[Change], dry_run: bool = False, log=print) -> dict:
    """Albums and tags one call each; a photo's title and description share one setMeta."""
    stats = {"applied": 0, "rejected": 0, "failed": 0, "albums_created": 0, "skipped": 0}
    decisions = store.decisions()
    created: dict[str, str] = {}  # norm title -> new album id
    meta: dict[str, dict[str, Change]] = {}
    for c in changes:
        if c.action in META_ACTIONS:
            meta.setdefault(c.photo_id, {})[c.action] = c
        elif c.action == "add_tags":
            _apply_tags(client, store, c, decisions, dry_run, stats, log)
        elif c.action == "set_tags":
            _apply_set_tags(client, store, c, dry_run, stats, log)
        elif c.action == "add_to_album":
            _apply_album(client, store, c, decisions, created, dry_run, stats, log)
        else:
            log(f"  ? unknown action '{c.action}' for {c.photo_id}, skipping")
            stats["skipped"] += 1
    for pid, by_action in meta.items():
        _apply_meta(client, store, pid, by_action, decisions, dry_run, stats, log)
    return stats


def outcomes(store, changes: list[Change]) -> list[dict]:
    """What happened to each change, read back from the decisions table."""
    decided = store.decisions()
    out = []
    for c in changes:
        if c.action in TAG_ACTIONS:
            out.append({"action": c.action, "tags": {t: decided.get((c.photo_id, "add_tag", t), "rejected")
                                                      for t in c.tags}})
            continue
        value = str(c.value).strip() if c.action in META_ACTIONS else c.new
        status = decided.get((c.photo_id, c.action, value)) or decided.get((c.photo_id, c.action, c.new), "rejected")
        out.append({"action": c.action, "target": value, "status": "applied" if status == "edited" else status})
    return out


def _apply_tags(client, store, c: Change, decisions, dry_run, stats, log) -> None:
    pid = c.photo_id
    kept = split_tags(c.value) if c.approved else []
    if not dry_run:
        for t in c.tags:
            if t not in kept:
                store.record(pid, "add_tag", t, "rejected")
    if not c.approved:
        stats["rejected"] += 1
    kept = [t for t in kept if decisions.get((pid, "add_tag", t)) != "applied"]
    if not kept:
        return
    if dry_run:
        log(f"  [dry-run] tag {pid}: {', '.join(kept)}")
        stats["applied"] += 1
        return
    try:
        client.call("flickr.photos.addTags", photo_id=pid, tags=quote_tags(kept))
    except FlickrCallError as e:
        for t in kept:
            store.record(pid, "add_tag", t, "failed", str(e))
        stats["failed"] += 1
        log(f"  ! {pid} tags: {e}")
        return
    for t in kept:
        store.record(pid, "add_tag", t, "applied")
    store.add_tags_local(pid, kept)
    stats["applied"] += 1


def _apply_set_tags(client, store, c: Change, dry_run, stats, log) -> None:
    """Replace the photo's tags with the ones you kept (flickr.photos.setTags). Removing is
    the one thing here that can't be added back by the tool, so it re-reads the photo first
    and writes only if its tags on Flickr are still the ones the proposal replaces."""
    pid = c.photo_id
    kept = split_tags(c.value) if c.approved else []
    if not dry_run:
        for t in c.tags:
            if t not in kept:
                store.record(pid, "add_tag", t, "rejected")
    if not kept:             # rejected (an empty list would wipe every tag: never)
        stats["rejected"] += 1
        return
    if dry_run:
        log(f"  [dry-run] replace tags {pid}: {c.old!r} → {', '.join(kept)}")
        stats["applied"] += 1
        return
    try:
        refresh_photo(client, store, pid)
    except FlickrCallError as e:
        for t in kept:
            store.record(pid, "add_tag", t, "failed", f"couldn't re-read photo: {e}")
        stats["failed"] += 1
        log(f"  ! {pid} re-read before setTags: {e}")
        return
    photo = store.photo(pid)
    now = {norm_tag(t) for t in (photo["tags"] or "").split()} if photo is not None else None
    if now != {norm_tag(t) for t in c.old.split()}:
        for t in kept:      # failed, not rejected: proposed again next time, against the new tags
            store.record(pid, "add_tag", t, "failed", "tags changed on Flickr since proposed")
        stats["skipped"] += 1
        log(f"  ! {pid}: tags changed on Flickr since proposed; left alone")
        return
    try:
        client.call("flickr.photos.setTags", photo_id=pid, tags=quote_tags(kept))
    except FlickrCallError as e:
        for t in kept:
            store.record(pid, "add_tag", t, "failed", str(e))
        stats["failed"] += 1
        log(f"  ! {pid} setTags: {e}")
        return
    for t in kept:
        store.record(pid, "add_tag", t, "applied")
    for t in sorted(now - {norm_tag(t) for t in kept}):
        store.record(pid, "remove_tag", t, "applied")
    store.set_tags_local(pid, [norm_tag(t) for t in kept])
    stats["applied"] += 1


def _apply_album(client, store, c: Change, decisions, created, dry_run, stats, log) -> None:
    pid, title = c.photo_id, c.new
    if not c.approved:
        if not dry_run:
            store.record(pid, "add_to_album", title, "rejected")
        stats["rejected"] += 1
        return
    if decisions.get((pid, "add_to_album", title)) == "applied":
        stats["skipped"] += 1
        return
    album_id = c.album_id or created.get(norm_title(title), "")
    if dry_run:
        log(f"  [dry-run] {pid} → {title}{'' if album_id else ' (create)'}")
        if not album_id:
            created[norm_title(title)] = "dry-run"
        stats["applied"] += 1
        return
    try:
        if not album_id:
            resp = client.call("flickr.photosets.create", title=title, primary_photo_id=pid)
            album_id = str(resp["photoset"]["id"])
            created[norm_title(title)] = album_id
            store.add_album(album_id, title)
            stats["albums_created"] += 1
            log(f"  created album '{title}'")
        else:
            try:
                client.call("flickr.photosets.addPhoto", photoset_id=album_id, photo_id=pid)
            except FlickrCallError as e:
                if e.code != ALREADY_IN_SET:
                    raise
    except FlickrCallError as e:
        store.record(pid, "add_to_album", title, "failed", str(e))
        stats["failed"] += 1
        log(f"  ! {pid} → {title}: {e}")
        return
    store.add_membership(album_id, pid)
    store.record(pid, "add_to_album", title, "applied")
    stats["applied"] += 1


def _apply_meta(client, store, pid: str, by_action: dict[str, Change], decisions, dry_run, stats, log) -> None:
    """A photo's set_title and/or set_description, sent as a single setMeta call."""
    if not dry_run and any(c.approved for c in by_action.values()):
        # setMeta writes title AND description, so re-read both from Flickr first: the local
        # copy can be stale, and sending a stale field back would overwrite the real one.
        try:
            refresh_photo(client, store, pid)
        except FlickrCallError as e:
            for action, c in by_action.items():
                store.record(pid, action, str(c.value).strip(), "failed", f"couldn't re-read photo: {e}")
            stats["failed"] += len(by_action)
            log(f"  ! {pid} re-read before setMeta: {e}")
            return
    photo = store.photo(pid)
    writes: dict[str, str] = {}
    for action, c in by_action.items():
        value = str(c.value).strip()
        if not c.approved or not value:
            if not dry_run:
                store.record(pid, action, c.new, "rejected")
            stats["rejected"] += 1
            continue
        if decisions.get((pid, action, value)) == "applied":
            stats["skipped"] += 1
            continue
        if not dry_run:
            current = (photo[action.removeprefix("set_")] or "").strip() if photo is not None else None
            # Changed on Flickr since this was proposed (or the photo is gone): leave it alone.
            stale = (current is None or (action == "set_title" and not same_text(current, c.old))
                     or (action == "set_description" and current and not same_text(current, c.old)))
            if stale:
                store.record(pid, action, c.new, "rejected", f"{action.removeprefix('set_')} changed since proposed")
                stats["skipped"] += 1
                continue
        writes[action] = value
    if not writes:
        return
    if dry_run:
        for action, value in writes.items():
            log(f"  [dry-run] {action.removeprefix('set_')} {pid}: {by_action[action].old!r} → {value!r}")
        stats["applied"] += len(writes)
        return
    # setMeta replaces both fields; the one not changing goes back exactly as just re-read.
    title = writes.get("set_title", (photo["title"] if photo is not None else "") or "")
    description = writes.get("set_description", (photo["description"] if photo is not None else "") or "")
    try:
        client.call("flickr.photos.setMeta", photo_id=pid, title=title, description=description)
    except FlickrCallError as e:
        for action, value in writes.items():
            store.record(pid, action, value, "failed", str(e))
        stats["failed"] += len(writes)
        log(f"  ! {pid} setMeta: {e}")
        return
    if "set_title" in writes:
        store.set_title_local(pid, title)
    if "set_description" in writes:
        store.set_description_local(pid, description)
    for action, value in writes.items():
        store.record(pid, action, value, "applied")
        if value != by_action[action].new:   # you edited it: the proposal itself is settled too
            store.record(pid, action, by_action[action].new, "edited", f"applied as edited: {value[:200]}")
    stats["applied"] += len(writes)
