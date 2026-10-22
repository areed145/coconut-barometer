"""Command line entry point:  python -m coconut_barometer <command>"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
import time

from .config import FLICKR_CALLS_PER_SECOND, load_config
from .store import Store


def _client(cfg):
    from .flickr_client import FlickrClient

    c = FlickrClient(cfg["flickr"]["api_key"], cfg["flickr"]["api_secret"], FLICKR_CALLS_PER_SECOND)
    c.authenticate("write")
    return c


def _new_client(cfg):
    """A Flickr client for the web app: never prompts (a server can't answer), and None until
    the API keys are set. Without a saved Flickr login, calls fail until you use Settings →
    Connect Flickr (or `coconut-barometer auth`)."""
    from .flickr_client import FlickrClient

    if not (cfg["flickr"]["api_key"] and cfg["flickr"]["api_secret"]):
        return None
    return FlickrClient(cfg["flickr"]["api_key"], cfg["flickr"]["api_secret"], FLICKR_CALLS_PER_SECOND)


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
    minutes = len(todo) / FLICKR_CALLS_PER_SECOND / 60
    took = f"{minutes / 60:.1f} h" if minutes >= 90 else f"{max(1, round(minutes))} min"
    print(f"{len(todo)} photo(s) need camera info: one Flickr call each (~{took} at "
          f"{FLICKR_CALLS_PER_SECOND}/s). Safe to stop and resume.")
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    print(f"Cameras: {fetch_cameras(_client(cfg), store, todo)}")


def cmd_check(config_path: str) -> int:
    """Validate the config, then check it against your library. Exit code 1 on errors."""
    from pathlib import Path

    from .config import DEFAULTS, _merge, apply_env, read_raw, validate
    from .util import norm_title

    from .settings import _store_for, layer

    file_raw = read_raw(config_path)
    raw, sources, problems = apply_env(file_raw), {}, []
    layered = _store_for(config_path, file_raw)
    if layered is not None:
        raw, sources, problems = layer(file_raw, layered)
    cfg = _merge(DEFAULTS, raw)
    errors, warnings = validate(raw, cfg)
    warnings += problems
    notes: list[str] = []
    admin = sorted(p for p, src in sources.items() if src == "admin")
    if admin:
        notes.append(f"set on the admin page (override config.yaml): {', '.join(admin)}")
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
        warnings.append("no saved Flickr login (~/.flickr); run `python -m coconut_barometer auth`")

    print(f"{config_path}: {len(errors)} error(s), {len(warnings)} warning(s)")
    for title, items in (("Errors", errors), ("Warnings", warnings), ("Info", notes)):
        if items:
            print(f"\n{title}:")
            print("\n".join(f"  - {x}" for x in items))
    return 1 if errors else 0


def cmd_user(cfg, store, args):
    """The web app's login: add/passwd prompt for a password (twice, hidden)."""
    import getpass

    from .web.security import MIN_PASSWORD, hash_password

    if args.action == "list":
        print("\n".join(store.usernames()) or "No users yet. `user add <name>` creates the login.")
        return
    if not args.name:
        sys.exit("Give a username.")
    if args.action == "remove":
        store.delete_user(args.name)
        print(f"Removed {args.name} (and signed out their sessions).")
        return
    if args.action == "passwd" and args.name not in store.usernames():
        sys.exit(f"No user {args.name!r}. `user add {args.name}` creates it.")
    if args.action == "add" and args.name in store.usernames():
        sys.exit(f"{args.name!r} exists; use `user passwd {args.name}`.")
    first = getpass.getpass(f"Password for {args.name} (at least {MIN_PASSWORD} characters): ")
    if getpass.getpass("Again: ") != first:
        sys.exit("The passwords didn't match.")
    try:
        store.set_password_hash(args.name, hash_password(first))
    except ValueError as e:
        sys.exit(str(e))
    print(f"Saved. {'Any existing sessions for this user were signed out.' if args.action == 'passwd' else ''}")


def _restore_on_start(cfg, store):
    """Bringing a database to a server: put it in the data directory as restore.db.gz (or
    restore.db) and restart. It's checked and restored before anything opens it, then
    renamed so it's never applied twice."""
    from pathlib import Path

    from . import settings as S
    from .backup import restore
    from .config import data_dir

    folder = data_dir() or Path(cfg["database"]).parent
    for name in ("restore.db.gz", "restore.db"):
        source = folder / name
        if source.exists():
            store.close()
            kept = restore(cfg, str(source))
            source.rename(folder / f"restored-{time.strftime('%Y%m%d-%H%M%S')}-{name}")
            print(f"Restored the database from {name}; the previous one is kept at {kept}.")
            staged_key = folder / "restore.key"
            if staged_key.exists():        # the key the backup's API keys are encrypted with
                target = S.key_file()
                if target.exists():
                    target.rename(target.with_name(f"{target.name}.before-restore-{time.strftime('%Y%m%d-%H%M%S')}"))
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(staged_key, target)
                os.chmod(target, 0o600)
                print(f"Installed the backup's encryption key at {target}.")
            return Store(cfg["database"])
    return store


def cmd_serve(cfg, store, args):
    """The web app: API + frontend, the review worker and the sync scheduler."""
    import uvicorn

    from .web.app import create_app

    import logging

    web = cfg["web"]
    store = _restore_on_start(cfg, store)

    class _NoQuery(logging.Filter):
        """Access log without query strings (Flickr's one-time login code arrives in one)."""
        def filter(self, record):
            if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
                record.args = (*record.args[:2], record.args[2].split("?")[0], *record.args[3:])
            return True
    logging.getLogger("uvicorn.access").addFilter(_NoQuery())
    host, port = args.host or web["host"], args.port or web["port"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"! Listening on {host}: anyone who can reach it gets the login page. "
              "On a server, keep 127.0.0.1 and put Caddy (HTTPS) in front.")
        if not web["secure_cookies"]:
            sys.exit("Refusing: web.secure_cookies is off, so the session cookie would travel unencrypted.")
    vcfg = cfg["vision"]
    provider = lambda: _provider(cfg)   # made on first use, from the settings at the time
    client = _new_client(cfg)
    if client is None:
        print("! No Flickr API key/secret yet: set them in Settings (syncing waits until then).")
    app = create_app(cfg, client, provider_factory=provider, cfg_path=args.config, client_factory=_new_client)
    print(f"Serving on http://{host}:{port}  (sync every {web['sync_every_minutes']} min; Ctrl-C stops)")
    uvicorn.run(app, host=host, port=port, proxy_headers=True, server_header=False,
                forwarded_allow_ips=",".join(web["trusted_proxies"]), log_level="info")


def cmd_settings(cfg, store, args):
    """Settings layers and the encrypted API keys (see settings.py)."""
    import re

    from . import settings as S
    from .config import read_raw

    file_raw = read_raw(args.config)
    if args.action == "show":
        _, sources, problems = S.layer(file_raw, store)
        shown = {p: src for p, src in sources.items() if src in ("admin", "env")}
        print("\n".join(f"  {src:<6} {p}" for p, src in sorted(shown.items())) or "Everything comes from config.yaml / the defaults.")
        for problem in problems:
            print(f"! {problem}")
        return
    if args.action == "import-file":
        # Everything in a YAML file into the database (admin layer) + its keys, encrypted:
        # then a server needs no config.yaml at all.
        source = read_raw(args.file or args.config)
        changes = {p: S.get_path(source, p) for p in S.leaves()
                   if S.get_path(source, p) is not None and p not in S.LOCKED and p not in S.SECRETS}
        # Settings you changed on the admin page win, unless --overwrite.
        kept = sorted(p for p in changes if p in store.admin_settings()
                      and store.admin_settings()[p] != changes[p]) if not args.overwrite else []
        for p in kept:
            changes.pop(p)
        out = S.save(args.config, {}, store, changes, [], "cli")
        keys = []
        rows = store.secret_rows()
        for path in (p for p in S.SECRETS if S.get_path(source, p)):
            row = rows.get(path)
            stored = S.decrypt(path, row["nonce"], row["ciphertext"]) if row else None
            if stored == S.get_path(source, path):
                continue                                   # already the same
            if stored and not args.overwrite:
                kept.append(path)                          # set on the admin page: it wins
                continue
            S.set_secret(store, path, S.get_path(source, path), "cli")
            keys.append(path)
        print(f"Stored {len(out['changed'])} setting(s) and {len(keys)} key(s) (encrypted) in {cfg['database']}.")
        if kept:
            print(f"Kept what the admin page has for: {', '.join(kept)} (--overwrite takes the file's).")
        skipped = [p for p in S.LOCKED if S.get_path(source, p) is not None]
        if skipped:
            print(f"Not stored (server settings; set them in the environment): {', '.join(skipped)}")
        return
    # import-secrets: API keys from config.yaml into the encrypted store
    moved = [p for p in S.SECRETS if S.get_path(file_raw, p)]
    for path in moved:
        S.set_secret(store, path, S.get_path(file_raw, path), "cli")
    print(f"Stored {len(moved)} key(s) encrypted: {', '.join(moved) or 'none were in config.yaml'}.")
    if moved and args.blank:
        text = open(args.config).read()
        new = re.sub(r'^(\s*(?:api_key|api_secret):\s*)"[^"]*"', r'\1""', text, flags=re.M)
        open(args.config, "w").write(new)
        print(f"Blanked them in {args.config}.")
    elif moved:
        print(f"They still work from {args.config} too; blank them there (or run with --blank).")


def cmd_backup(cfg, store, args):
    from .backup import list_backups, make_backup

    if args.list:
        for b in list_backups(cfg):
            print(f"  {b['name']}  {b['size'] / 1048576:6.1f} MB  {time.ctime(b['created_at'])}")
        return
    path = make_backup(cfg)
    store.set_meta("last_backup", int(time.time()))
    print(f"Backed up to {path} ({path.stat().st_size / 1048576:.1f} MB).")


def cmd_restore(cfg, store, args):
    """Replace the database with a backup. Stop `serve` first."""
    from .backup import restore

    if not args.yes and input(f"Replace {cfg['database']} with {args.file}? Stop the web app first. [y/N] ").strip().lower() != "y":
        return
    store.close()
    kept = restore(cfg, args.file)
    print(f"Restored. The previous database is kept at {kept}.")


def cmd_status(cfg, store, args):
    last = store.get_meta("last_sync")
    print(f"Last sync: {time.ctime(int(last)) if last else 'never'}")
    for k, v in store.counts().items():
        print(f"  {k:>11}: {v}")


def cmd_classify(cfg, store, args):
    from .classify import classify_photos, fingerprint, pending_photos

    vcfg = cfg["vision"]
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
    provider = _provider(cfg)
    skip = set(store.queue_ids(["applying"])) | (
        set() if args.rebuild else set(store.queue_ids()) - set(store.queue_ids(["new"])))
    todo = [p["id"] for p in store.photos() if p["id"] not in skip and (not args.photo or p["id"] in args.photo)
            and (not args.media or p["media"] == args.media)]
    todo = todo[:args.limit] if args.limit else todo
    if not todo:
        print("Every photo is already in the review queue. Run `review`, or `process --rebuild` after changing settings.")
        return
    exif_calls = len(set(todo) - store.camera_ids())
    calls = exif_calls + (2 * len(todo) if args.refresh_each else 0)
    minutes = calls / FLICKR_CALLS_PER_SECOND / 60
    print(f"{len(todo)} photo(s) to process: ~{calls} Flickr call(s) (~{minutes:.0f} min)"
          + f", vision on up to {len(todo)} (~${len(todo) * 0.0006:.2f} on Haiku 5.5)"
          + ". Safe to stop and resume.")
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    stats = process(cfg, store, client, provider, photo_ids=args.photo, limit=args.limit,
                    rebuild=args.rebuild, refresh_each=args.refresh_each, media=args.media)
    print(f"Done: {stats}\nReview them with:  python -m coconut_barometer review")


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
          f"~{calls} Flickr calls (~{calls / FLICKR_CALLS_PER_SECOND / 60:.0f} min). Safe to stop and resume.")
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
    ap = argparse.ArgumentParser(prog="coconut_barometer", description="Auto-tag and album-sort a Flickr library.")
    ap.add_argument("-c", "--config", default=os.environ.get("CONFIG_PATH", "config.yaml"))
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
    p = sub.add_parser("settings", help="admin-page settings: show | import-file | import-secrets")
    p.add_argument("action", choices=["show", "import-file", "import-secrets"])
    p.add_argument("file", nargs="?", help="with import-file: the YAML to import (default: the config file)")
    p.add_argument("--blank", action="store_true", help="with import-secrets: also blank the keys in config.yaml")
    p.add_argument("--overwrite", action="store_true",
                   help="with import-file: also replace settings you changed on the admin page")
    p = sub.add_parser("backup", help="back up the database now (safe while the app runs)")
    p.add_argument("--list", action="store_true", help="list backups instead")
    p = sub.add_parser("restore", help="replace the database with a backup (stop serve first)")
    p.add_argument("file")
    p.add_argument("-y", "--yes", action="store_true")
    p = sub.add_parser("user", help="the web app's login: add | passwd | list | remove")
    p.add_argument("action", choices=["add", "passwd", "list", "remove"])
    p.add_argument("name", nargs="?")
    p = sub.add_parser("serve", help="run the web app (API, frontend, review worker, sync scheduler)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p = sub.add_parser("review", help="open the review tool: approve each photo's changes and apply them")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    p = sub.add_parser("plan", help="preview what would be proposed (read-only; queues nothing)")
    p.add_argument("--list", action="store_true", help="list every change, not just the totals")
    p.add_argument("--photo", action="append", help="only this photo id (repeatable)")
    args = ap.parse_args(argv)
    if args.cmd == "check":   # before load_config, which stops at the first config error
        sys.exit(cmd_check(args.config))

    from .store import SchemaTooNew

    try:
        cfg = load_config(args.config)
        store = Store(cfg["database"])
    except SchemaTooNew as e:
        sys.exit(f"{e}\n(If the web app or another command is running older code, restart it on this version.)")
    {"auth": cmd_auth, "sync": cmd_sync, "stats": cmd_stats, "albums": cmd_albums, "status": cmd_status,
     "camera": cmd_camera, "process": cmd_process, "social": cmd_social, "user": cmd_user, "serve": cmd_serve, "settings": cmd_settings, "backup": cmd_backup, "restore": cmd_restore, "review": cmd_review, "classify": cmd_classify, "plan": cmd_plan}[args.cmd](cfg, store, args)


if __name__ == "__main__":
    main()
