"""Command line entry point:  python -m flickr_organizer <command>"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
import time

from .config import load_config
from .store import Store


def _client(cfg):
    from .flickr_client import FlickrClient

    c = FlickrClient(cfg["flickr"]["api_key"], cfg["flickr"]["api_secret"], cfg["flickr"]["calls_per_second"])
    c.authenticate("write")
    return c


def _provider(cfg):
    from .vision import make_provider

    return make_provider(cfg["vision"])


def cmd_auth(cfg, store, args):
    c = _client(cfg)
    print(f"Authenticated as {c.nsid()} (token cached in ~/.flickr/)")


def cmd_sync(cfg, store, args):
    """Bring the database up to date with Flickr: whatever's due (see schedule.py)."""
    from .schedule import run_due

    force = getattr(args, "mode", None)
    out = run_due(cfg, _client(cfg), store, force=force, log=print)
    for key in ("full", "photo_list", "incremental", "stats"):
        if key in out:
            print(f"  {key}: {out[key]}")
    print(f"Used {out['calls']} Flickr call(s).")


def _quick_sync(cfg, store, client) -> None:
    """Before process/social: just the incremental changes (or whatever sync is overdue)."""
    from .schedule import run_due

    out = run_due(cfg, client, store, stats=False, log=lambda *a: None)
    print(f"Synced ({', '.join(out['ran'])}, {out['calls']} call(s)).")


def cmd_stats(cfg, store, args):
    """Per-day stats collected so far; --backfill N fetches N more days now."""
    from . import stats_history

    if args.backfill:
        out = stats_history.update(_client(cfg), store, args.backfill)
        print(f"Fetched {out['days']} day(s).")
    days = store.stats_days()
    done = sorted(d for d, s in days.items() if s == "done")
    if not done:
        print("No per-day stats yet. `sync` collects them (Flickr Pro), or run `stats --backfill 30`.")
        return
    boundary = max((d for d, s in days.items() if s == "none"), default=None)
    print(f"Per-day stats for {len(done)} day(s): {done[0]} to {done[-1]}"
          + (f" (Flickr has none before {boundary})" if boundary else " (backfill continues on each sync)"))
    totals = store.daily_totals(since=done[-1] if len(done) < 7 else done[-7])
    print("\nRecent days (views):")
    for t in totals[-7:]:
        print(f"  {t['day']}  {t['total']:>7,}")
    since = done[-7] if len(done) >= 7 else done[0]
    print(f"\nMost viewed since {since}:")
    for p in store.top_photos(since, limit=args.top):
        print(f"  {p['views']:>7,} views  {p['favorites']:>4} faves  {(p['title'] or p['photo_id'])[:60]}")


def cmd_albums(cfg, store, args):
    albums = store.albums()
    if not albums:
        print("No albums cached yet — run `sync` first.")
    for a in albums:
        print(f"{a['id']:>20}  {a['count']:>6}  {a['title']}")


def cmd_camera(cfg, store, args):
    from .camera import fetch_cameras, pending_cameras

    todo = pending_cameras(store, limit=args.limit)
    if not todo:
        print("Nothing to fetch — every photo's camera is cached.")
        return
    minutes = len(todo) / cfg["flickr"]["calls_per_second"] / 60
    took = f"{minutes / 60:.1f} h" if minutes >= 90 else f"{max(1, round(minutes))} min"
    print(f"{len(todo)} photo(s) need camera info: one Flickr call each (~{took} at "
          f"{cfg['flickr']['calls_per_second']}/s). Safe to stop and resume.")
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    print(f"Cameras: {fetch_cameras(_client(cfg), store, todo)}")


def cmd_check(config_path: str) -> int:
    """Validate the config, then check it against your library. Exit code 1 on errors."""
    from pathlib import Path

    from .config import DEFAULTS, _merge, read_raw, validate
    from .util import norm_title

    raw = read_raw(config_path)
    cfg = _merge(DEFAULTS, raw)
    errors, warnings = validate(raw, cfg)
    notes: list[str] = []
    db = Path(cfg["database"])
    db = db if db.is_absolute() else Path(config_path).resolve().parent / db
    if not errors and db.exists():
        store = Store(str(db))
        have = {norm_title(a["title"]) for a in store.albums()}
        missing = lambda title: title and norm_title(title) not in have and not cfg["albums"]["create_missing"]
        named = [(f"vision.categories.{k}.album", c.get("album")) for k, c in cfg["vision"]["categories"].items()]
        named += [(f"geo.cities[{i}].album", c["album"]) for i, c in enumerate(cfg["geo"]["cities"])]
        named += [(f"geo.countries.aliases.{cc}", t) for cc, v in cfg["geo"]["countries"]["aliases"].items()
                  for t in ([v] if isinstance(v, str) else v)]
        named += [(f"standard_tags.camera.albums[{i}].album", r["album"])
                  for i, r in enumerate(cfg["standard_tags"]["camera"]["albums"])]
        named += [(f"standard_tags.album_tags.albums.{t}", t) for t in cfg["standard_tags"]["album_tags"]["albums"]]
        for where, title in named:
            if missing(title):
                close = difflib.get_close_matches(norm_title(title), sorted(have), n=1)
                warnings.append(f"{where}: no album called {title!r}, so it's skipped"
                                + (f" (closest: {next(a['title'] for a in store.albums() if norm_title(a['title']) == close[0])!r})" if close else ""))
        last = store.get_meta("last_sync")
        notes.append(f"library: {len(store.albums())} albums, last sync {time.ctime(int(last)) if last else 'never'}")
        if last and time.time() - int(last) > 7 * 86400:
            warnings.append("last sync is over a week old; process and social sync first, or run sync")
        counts = store.queue_counts()
        notes.append(f"queue: {counts.get('pending', 0)} to review, {counts.get('failed', 0)} failed")
    elif not errors:
        warnings.append(f"no database yet at {db}; run `auth` then `sync`")
    if not os.path.exists(os.path.expanduser("~/.flickr/oauth-tokens.sqlite")):
        warnings.append("no saved Flickr login (~/.flickr); run `python -m flickr_organizer auth`")

    print(f"{config_path}: {len(errors)} error(s), {len(warnings)} warning(s)")
    for title, items in (("Errors", errors), ("Warnings", warnings), ("Info", notes)):
        if items:
            print(f"\n{title}:")
            print("\n".join(f"  - {x}" for x in items))
    return 1 if errors else 0


def cmd_status(cfg, store, args):
    last = store.get_meta("last_sync")
    print(f"Last sync: {time.ctime(int(last)) if last else 'never'}")
    for k, v in store.counts().items():
        print(f"  {k:>11}: {v}")


def cmd_classify(cfg, store, args):
    from .classify import classify_photos, fingerprint, pending_photos

    vcfg = cfg["vision"]
    if not vcfg["categories"]:
        sys.exit("No vision.categories configured.")
    provider = _provider(cfg)
    fp = fingerprint(vcfg, provider.key)
    todo = pending_photos(store, vcfg, fp, limit=args.limit, photo_ids=args.photo)
    if not todo:
        print("Nothing to classify — all photos are cached for this model/category setup.")
        return
    est_in = len(todo) * 3400  # measured: ~500px thumbnail + 20-category prompt on Haiku 5.5
    print(f"{len(todo)} photo(s) to classify with {provider.key} "
          f"(~{est_in/1e6:.2f}M input tokens, rough estimate).")
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    locations = {}
    if vcfg["use_location"]:
        from .geo import locations_for

        locations = locations_for(todo, cfg["geo"]["min_accuracy"])
    stats = classify_photos(store, provider, vcfg, todo, locations=locations)
    print(f"Classified: {stats}")


def cmd_plan(cfg, store, args):
    """Preview what the rules would propose right now, from what's stored. Read-only:
    nothing is queued and nothing changes on Flickr. Use `process` and `review` to act."""
    from .planner import build_plan, summarize

    if not store.get_meta("last_sync"):
        sys.exit("No local data yet — run `sync` first.")
    provider_key = None
    if cfg["vision"]["enabled"] and cfg["vision"]["categories"]:
        provider_key = f"{_provider_name(cfg)}:{cfg['vision']['model']}"
    changes = build_plan(cfg, store, provider_key, photo_ids=set(args.photo) if args.photo else None)
    if not changes:
        print("Nothing to propose — the library already matches the rules.")
        return
    print(f"{len(changes)} change(s) would be proposed:")
    for what, n in summarize(changes).most_common():
        print(f"  {n:>6}  {what}")
    if args.list:
        for c in changes:
            new = " | ".join(c.new) if isinstance(c.new, list) else c.new
            print(f"{c.photo_id}  {c.action:<15} {new[:100]}  [{c.source}]")
    print("\nThis is a preview. `process` queues changes; `review` approves and applies them.")


def _provider_name(cfg) -> str:
    """Must match VisionProvider.key without instantiating the SDK."""
    name = cfg["vision"]["provider"].lower()
    if name == "anthropic":
        return "claude"
    if name == "ollama" or (name == "openai" and cfg["vision"]["base_url"]):
        return "openai-compat"
    return name


def cmd_process(cfg, store, args):
    """Per photo: (refresh) -> EXIF -> classify -> plan, into the review queue."""
    from .pipeline import process

    client = _client(cfg)
    if not args.no_sync:
        _quick_sync(cfg, store, client)
    vcfg = cfg["vision"]
    provider = _provider(cfg) if vcfg["enabled"] and vcfg["categories"] else None
    skip = set(store.queue_ids(["applying"])) | (set() if args.rebuild else set(store.queue_ids()))
    todo = [p["id"] for p in store.photos() if p["id"] not in skip and (not args.photo or p["id"] in args.photo)
            and (not args.media or p["media"] == args.media)]
    todo = todo[:args.limit] if args.limit else todo
    if not todo:
        print("Every photo is already in the review queue. Run `review`, or `process --rebuild` after changing settings.")
        return
    exif_calls = len(set(todo) - store.camera_ids())
    calls = exif_calls + (2 * len(todo) if args.refresh_each else 0)
    minutes = calls / cfg["flickr"]["calls_per_second"] / 60
    print(f"{len(todo)} photo(s) to process: ~{calls} Flickr call(s) (~{minutes:.0f} min)"
          + (f", vision on up to {len(todo)} (~${len(todo) * 0.0006:.2f} on Haiku 5.5)" if provider else "")
          + ". Safe to stop and resume.")
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    stats = process(cfg, store, client, provider, photo_ids=args.photo, limit=args.limit,
                    rebuild=args.rebuild, refresh_each=args.refresh_each, media=args.media)
    print(f"Done: {stats}\nReview them with:  python -m flickr_organizer review")


def cmd_social(cfg, store, args):
    """Groups + pools, comments and faves, fetched only where counts changed."""
    from . import social

    client = _client(cfg)
    if not args.no_sync:
        _quick_sync(cfg, store, client)
    todo = social.plan(store)
    counts = {p["id"]: p for p in store.photos()}
    comments = [] if args.skip_comments else todo["comments"][:args.limit] if args.limit else todo["comments"]
    faves = [] if args.skip_faves else todo["faves"][:args.limit] if args.limit else todo["faves"]
    calls = (sum(1 for pid in comments if counts[pid]["comments"])
             + sum(-(-(counts[pid]["faves"] or 0) // 50) for pid in faves)
             + (0 if args.skip_groups else 2 * max(store.group_count(), 200)))
    print(f"To fetch: {'' if args.skip_groups else 'groups + your photos in each pool, '}"
          f"comments on {len(comments)} photo(s), faves on {len(faves)} photo(s): "
          f"~{calls} Flickr calls (~{calls / cfg['flickr']['calls_per_second'] / 60:.0f} min). Safe to stop and resume.")
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    if not args.skip_groups:
        print("Groups…")
        print(f"  {social.fetch_groups(client, store)}")
    if comments:
        print("Comments…")
        print(f"  {social.fetch_comments(client, store, comments)}")
    if faves:
        print("Faves…")
        print(f"  {social.fetch_faves(client, store, faves)}")


def cmd_review(cfg, store, args):
    from .review import ReviewApp, serve

    serve(ReviewApp(store, _client(cfg), cfg=cfg, provider=lambda: _provider(cfg)),
          port=args.port, open_browser=not args.no_browser)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="flickr_organizer", description="Auto-tag and album-sort a Flickr library.")
    ap.add_argument("-c", "--config", default=os.environ.get("FLICKR_ORGANIZER_CONFIG", "config.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="authorize with Flickr (write access)")
    p = sub.add_parser("sync", help="bring the database up to date with Flickr (whatever's due; cheap)")
    p.add_argument("--full", dest="mode", action="store_const", const="full",
                   help="re-read everything (~180 calls for 10k photos)")
    p.add_argument("--photo-list", dest="mode", action="store_const", const="photo_list",
                   help="re-read every photo (counts, daily snapshot) + incremental")
    p.add_argument("--incremental", dest="mode", action="store_const", const="incremental",
                   help="only what changed since last time")
    p = sub.add_parser("stats", help="per-day views/faves/comments collected so far")
    p.add_argument("--backfill", type=int, default=0, metavar="DAYS", help="fetch this many more past days now")
    p.add_argument("--top", type=int, default=10, help="how many top photos to list")
    sub.add_parser("albums", help="list cached albums (to help write config)")
    sub.add_parser("status", help="show local state")
    sub.add_parser("check", help="validate config.yaml and check it against your library")
    p = sub.add_parser("camera", help="fetch each photo's camera from EXIF (one call per photo, cached)")
    p.add_argument("--limit", type=int, help="only fetch this many photos")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    for name in ("classify",):
        p = sub.add_parser(name, help="classify photos with the vision model (process does this per photo)")
        p.add_argument("--limit", type=int, help="only classify this many photos (try 50 first)")
        p.add_argument("--photo", action="append", help="only this photo id (repeatable)")
        p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("process", help="per photo: EXIF, classify and plan into the review queue")
    p.add_argument("--limit", type=int, help="only process this many photos")
    p.add_argument("--photo", action="append", help="only this photo id (repeatable)")
    p.add_argument("--rebuild", action="store_true", help="re-plan photos already queued (after changing settings)")
    p.add_argument("--media", choices=["photo", "video"], help="only photos or only videos")
    p.add_argument("--refresh-each", action="store_true",
                   help="also re-read each photo and its albums from Flickr (+2 calls per photo)")
    p.add_argument("--no-sync", action="store_true", help="skip the bulk sync at the start")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("social", help="fetch your groups (and photos in each pool), comments and faves")
    p.add_argument("--skip-groups", action="store_true")
    p.add_argument("--skip-comments", action="store_true")
    p.add_argument("--skip-faves", action="store_true")
    p.add_argument("--limit", type=int, help="only this many photos' comments/faves")
    p.add_argument("--no-sync", action="store_true", help="skip the bulk sync at the start")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("review", help="open the review tool: approve each photo's changes and apply them")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    p = sub.add_parser("plan", help="preview what would be proposed (read-only; queues nothing)")
    p.add_argument("--list", action="store_true", help="list every change, not just the totals")
    p.add_argument("--photo", action="append", help="only this photo id (repeatable)")
    args = ap.parse_args(argv)
    if args.cmd == "check":   # before load_config, which stops at the first config error
        sys.exit(cmd_check(args.config))

    cfg = load_config(args.config)
    store = Store(cfg["database"])
    {"auth": cmd_auth, "sync": cmd_sync, "stats": cmd_stats, "albums": cmd_albums, "status": cmd_status,
     "camera": cmd_camera, "process": cmd_process, "social": cmd_social, "review": cmd_review, "classify": cmd_classify, "plan": cmd_plan}[args.cmd](cfg, store, args)


if __name__ == "__main__":
    main()
