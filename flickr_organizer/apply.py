"""Execute an (edited) plan CSV against Flickr."""
from __future__ import annotations

from .flickr_client import FlickrCallError
from .planner import read_plan_csv, split_tags
from .util import norm_title, quote_tags, same_text

YES = {"y", "yes", "1", "true", "x", "✓"}

# photosets.addPhoto error 3 = "Photo already in set" → treat as success.
ALREADY_IN_SET = 3

# Both are written by flickr.photos.setMeta, which always sets title and description together.
META_ACTIONS = ("set_title", "set_description")


def apply_plan(client, store, path: str, dry_run: bool = False, log=print) -> dict:
    return apply_rows(client, store, read_plan_csv(path), dry_run=dry_run, log=log)


def apply_rows(client, store, rows: list[dict], dry_run: bool = False, log=print) -> dict:
    """Apply plan rows (dicts with the PLAN_FIELDS keys; approve = y/n)."""
    stats = {"applied": 0, "rejected": 0, "failed": 0, "albums_created": 0, "skipped": 0}
    decisions = store.decisions()
    created: dict[str, str] = {}  # norm title -> new album id
    meta_rows: dict[str, dict[str, dict]] = {}  # photo_id -> {action: row}
    for r in rows:
        if r["action"] in META_ACTIONS:
            meta_rows.setdefault(r["photo_id"], {})[r["action"]] = r
    meta_done: set[str] = set()

    for i, r in enumerate(rows, 1):
        pid, action = r["photo_id"], r["action"]
        approved = (r.get("approve") or "").strip().lower() in YES

        if action == "add_tags":
            tags = split_tags(r["target"])
            original = split_tags(r.get("original_target") or r["target"])
            dropped = [t for t in original if t not in tags]
            if not approved:
                dropped, tags = original, []
            tags = [t for t in tags if decisions.get((pid, "add_tag", t)) != "applied"]
            if not dry_run:
                for t in dropped:
                    store.record(pid, "add_tag", t, "rejected")
            stats["rejected"] += 1 if not approved else 0
            if not tags:
                continue
            if dry_run:
                log(f"  [dry-run] tag {pid}: {', '.join(tags)}")
                stats["applied"] += 1
                continue
            try:
                client.call("flickr.photos.addTags", photo_id=pid, tags=quote_tags(tags))
                for t in tags:
                    store.record(pid, "add_tag", t, "applied")
                store.add_tags_local(pid, tags)
                stats["applied"] += 1
            except FlickrCallError as e:
                for t in tags:
                    store.record(pid, "add_tag", t, "failed", str(e))
                stats["failed"] += 1
                log(f"  ! {pid} tags: {e}")

        elif action == "add_to_album":
            title = r["target"]
            if not approved:
                if not dry_run:
                    store.record(pid, "add_to_album", title, "rejected")
                stats["rejected"] += 1
                continue
            if decisions.get((pid, "add_to_album", title)) == "applied":
                stats["skipped"] += 1
                continue
            album_id = r.get("album_id") or created.get(norm_title(title), "")
            if dry_run:
                log(f"  [dry-run] {pid} → {title}{'' if album_id else ' (create)'}")
                if not album_id:
                    created[norm_title(title)] = "dry-run"
                stats["applied"] += 1
                continue
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
                store.add_membership(album_id, pid)
                store.record(pid, "add_to_album", title, "applied")
                stats["applied"] += 1
            except FlickrCallError as e:
                store.record(pid, "add_to_album", title, "failed", str(e))
                stats["failed"] += 1
                log(f"  ! {pid} → {title}: {e}")
        elif action in META_ACTIONS:
            if pid not in meta_done:
                # A photo's retitle and description go out together in one setMeta call.
                meta_done.add(pid)
                _apply_meta(client, store, pid, meta_rows[pid], decisions, dry_run, stats, log)
        else:
            log(f"  ? unknown action '{action}' on row {i}, skipping")
            stats["skipped"] += 1

        if i % 50 == 0:
            log(f"  {i}/{len(rows)} rows")

    log(f"Done: {stats}")
    return stats


def _apply_meta(client, store, pid: str, rows: dict[str, dict], decisions, dry_run: bool, stats: dict, log) -> None:
    """Apply a photo's set_title and/or set_description rows with a single setMeta call."""
    if not dry_run and any((r.get("approve") or "").strip().lower() in YES for r in rows.values()):
        # setMeta writes title AND description, so re-read both from Flickr first: the local
        # copy can be stale, and sending a stale field back would overwrite the real one.
        from .pipeline import refresh_photo
        try:
            refresh_photo(client, store, pid)
        except FlickrCallError as e:
            for action, r in rows.items():
                store.record(pid, action, (r["target"] or "").strip(), "failed", f"couldn't re-read photo: {e}")
            stats["failed"] += len(rows)
            log(f"  ! {pid} re-read before setMeta: {e}")
            return
    photo = store.photo(pid)
    changes: dict[str, str] = {}
    for action, r in rows.items():
        value = (r["target"] or "").strip()
        if (r.get("approve") or "").strip().lower() not in YES or not value:
            if not dry_run:
                store.record(pid, action, r["target"], "rejected")
            stats["rejected"] += 1
            continue
        if decisions.get((pid, action, value)) == "applied":
            stats["skipped"] += 1
            continue
        if not dry_run:
            if action == "set_title" and photo is not None and (photo["title"] or "") != (r.get("original_target") or ""):
                # You retitled it yourself since the plan was made; leave it alone.
                store.record(pid, action, value, "rejected", "title changed since plan")
                stats["skipped"] += 1
                continue
            current = (photo["description"] or "").strip() if photo is not None else None
            if action == "set_description" and (current is None or (current and not same_text(current, r.get("original_target")))):
                # Gone from the library, or the description changed since the plan (e.g. you wrote one).
                store.record(pid, action, value, "rejected", "description changed since plan")
                stats["skipped"] += 1
                continue
        changes[action] = value
    if not changes:
        return
    if dry_run:
        if "set_title" in changes:
            log(f"  [dry-run] retitle {pid}: {rows['set_title'].get('original_target', '')!r} → {changes['set_title']!r}")
        if "set_description" in changes:
            log(f"  [dry-run] describe {pid}: {changes['set_description']!r}")
        stats["applied"] += len(changes)
        return
    # setMeta replaces both fields, so whichever one isn't changing is sent back as it was at the last sync.
    title = changes.get("set_title", (photo["title"] if photo is not None else "") or "")
    description = changes.get("set_description", (photo["description"] if photo is not None else "") or "")
    try:
        client.call("flickr.photos.setMeta", photo_id=pid, title=title, description=description)
    except FlickrCallError as e:
        for action, value in changes.items():
            store.record(pid, action, value, "failed", str(e))
        stats["failed"] += len(changes)
        log(f"  ! {pid} setMeta: {e}")
        return
    if "set_title" in changes:
        store.set_title_local(pid, title)
    if "set_description" in changes:
        store.set_description_local(pid, description)
    for action, value in changes.items():
        store.record(pid, action, value, "applied")
    stats["applied"] += len(changes)
