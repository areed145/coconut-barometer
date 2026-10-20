"""Command line entry point:  python -m flickr_organizer <command>"""
from __future__ import annotations

import argparse
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
    from .sync import sync

    sync(_client(cfg), store, cfg["vision"]["thumbnail"])


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
    if vcfg.get("use_location", True):
        from .geo import locations_for

        locations = locations_for(todo, cfg["geo"]["min_accuracy"])
    stats = classify_photos(store, provider, vcfg, todo, locations=locations)
    print(f"Classified: {stats}")


def cmd_plan(cfg, store, args):
    from .planner import build_plan, write_plan_csv
    from .report import write_report

    if not store.get_meta("last_sync"):
        sys.exit("No local data yet — run `sync` first.")
    provider_key = None
    if cfg["vision"].get("enabled", True) and cfg["vision"]["categories"]:
        provider_key = f"{_provider_name(cfg)}:{cfg['vision']['model']}"
    rows = build_plan(cfg, store, provider_key)
    if not rows:
        print("Nothing to propose — library already matches the rules.")
        return None
    os.makedirs(cfg["output_dir"], exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    csv_path = os.path.join(cfg["output_dir"], f"plan-{stamp}.csv")
    html_path = os.path.join(cfg["output_dir"], f"plan-{stamp}.html")
    write_plan_csv(rows, csv_path)
    write_report(rows, html_path, os.path.basename(csv_path))
    summary: dict[str, int] = {}
    for r in rows:
        key = {"add_tags": "tags", "set_title": "renames", "set_description": "descriptions"}.get(r.action, r.target)
        summary[key] = summary.get(key, 0) + 1
    print(f"{len(rows)} proposed change(s):")
    for k, v in sorted(summary.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>6}  {k}")
    print(f"\nReview:  {html_path}\nPlan:    {csv_path}")
    print("Edit the CSV (approve=n to reject) or use the HTML page's download button, then run `apply <csv>`.")
    return csv_path


def _provider_name(cfg) -> str:
    """Must match VisionProvider.key without instantiating the SDK."""
    name = cfg["vision"]["provider"].lower()
    if name == "anthropic":
        return "claude"
    if name == "ollama" or (name == "openai" and cfg["vision"].get("base_url")):
        return "openai-compat"
    return name


def cmd_apply(cfg, store, args):
    from .apply import apply_plan

    if not os.path.exists(args.plan):
        sys.exit(f"Plan not found: {args.plan}")
    client = None if args.dry_run else _client(cfg)
    apply_plan(client, store, args.plan, dry_run=args.dry_run)


def cmd_process(cfg, store, args):
    """Per photo: (refresh) -> EXIF -> classify -> plan, into the review queue."""
    from .pipeline import process

    client = _client(cfg)
    if not args.no_sync:
        cmd_sync(cfg, store, args)
    vcfg = cfg["vision"]
    provider = _provider(cfg) if vcfg.get("enabled", True) and vcfg["categories"] else None
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
        cmd_sync(cfg, store, args)
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


def cmd_run(cfg, store, args):
    """Incremental run: sync → classify new photos → plan."""
    cmd_sync(cfg, store, args)
    if cfg["standard_tags"]["camera"].get("enabled"):
        cmd_camera(cfg, store, args)
    if cfg["vision"].get("enabled", True) and cfg["vision"]["categories"]:
        cmd_classify(cfg, store, args)
    cmd_plan(cfg, store, args)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="flickr_organizer", description="Auto-tag and album-sort a Flickr library.")
    ap.add_argument("-c", "--config", default=os.environ.get("FLICKR_ORGANIZER_CONFIG", "config.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="authorize with Flickr (write access)")
    sub.add_parser("sync", help="download photo list + album membership")
    sub.add_parser("albums", help="list cached albums (to help write config)")
    sub.add_parser("status", help="show local state")
    p = sub.add_parser("camera", help="fetch each photo's camera from EXIF (one call per photo, cached)")
    p.add_argument("--limit", type=int, help="only fetch this many photos")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    for name in ("classify", "run"):
        p = sub.add_parser(name, help="classify photos with the vision model" if name == "classify"
                           else "sync + classify new photos + plan (the incremental workflow)")
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
    sub.add_parser("plan", help="write a reviewable plan (CSV + HTML) — changes nothing on Flickr")
    p = sub.add_parser("apply", help="apply an approved plan CSV to Flickr")
    p.add_argument("plan")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    store = Store(cfg["database"])
    {"auth": cmd_auth, "sync": cmd_sync, "albums": cmd_albums, "status": cmd_status,
     "camera": cmd_camera, "process": cmd_process, "social": cmd_social, "review": cmd_review, "classify": cmd_classify, "plan": cmd_plan, "apply": cmd_apply, "run": cmd_run}[args.cmd](cfg, store, args)


if __name__ == "__main__":
    main()
