# Coconut Barometer

Coconut Barometer is a personal website for browsing and organizing your Flickr library. It includes a private photo gallery, map, analytics, and a review workflow for proposed titles, descriptions, tags and albums. The existing Python package and command-line entry point remain named `flickr_organizer` / `flickr-organizer`.

Adds your Flickr photos to the right albums and tags, using:

- **Geotags**: country albums, US state albums, and a few city albums (matched by distance from the city center)
- **A vision LLM**: subject albums like Food, Landscapes, Animals, descriptive tags, a title and a description. Claude is the default; OpenAI or a local Ollama model also work.
- **Your conventions**: standard tags for dates, places, camera names and optional album tags, plus camera albums.

Every photo (and video, from its preview frame) gets all four kinds of proposal: a title, a description, tags and albums. You accept, edit or decline each one in the reviewer; nothing is switched off in settings. Apart from titles and descriptions it only ever **adds**: it never takes a photo out of an album, and removes tags only when you re-process a photo with **Tags: overwrite existing** in the reviewer (that photo's tags are then proposed as one replacement, showing what it removes). Nothing changes on Flickr until you approve it.

## Setup

Uses [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`). `uv sync` creates `.venv` with the exact versions in `uv.lock` and installs this project in editable mode. Run commands with `uv run flickr-organizer …`, or activate `.venv` and use `python -m flickr_organizer …` as below.

```bash
cd flickr-organizer
uv sync                     # add --extra openai for OpenAI/Ollama; --no-dev on a server
source .venv/bin/activate

cp config.example.yaml config.yaml
# edit config.yaml: set flickr.api_key, flickr.api_secret and vision.api_key
#   Flickr key: https://www.flickr.com/services/apps/create/
#   Anthropic key: https://console.anthropic.com/
python -m flickr_organizer auth      # opens the browser; paste the code back. Token is cached in ~/.flickr/
python -m flickr_organizer sync      # caches your photo list + album membership locally
python -m flickr_organizer albums    # lists your album titles, so you can match them in config.yaml
python -m flickr_organizer check     # validates config.yaml and checks album names against your library
```

Every command checks `config.yaml` when it starts and stops with a list of problems (a misspelled setting, a wrong type, a value out of range, an unknown `{placeholder}`, a broken regex) rather than failing partway through a run. `check` also shows warnings, such as album names that match no album, a call rate over Flickr's limit, or missing keys.

Edit `config.yaml` so that the category `album:` names, US state `title:` format, and `cities:` match the albums you already have. Albums are matched by title, ignoring case, accents and punctuation. If a title doesn't match, that album is skipped. Nothing gets created unless you set `albums.create_missing: true`.

## Web app

To run it on Render from GitHub (HTTPS, a persistent disk, the hourly scheduler), see [docs/DEPLOY.md](docs/DEPLOY.md). Backups: `flickr-organizer backup` (and `backup --list`), `restore <file>` with the app stopped, or **Settings → Backups → Restore a backup** in the web app (it restarts into it). A new server with no login opens on a setup page: create a login or drag in a backup, with the setup code from the server's log.

```bash
flickr-organizer user add <name>     # the login (or create it on the setup page; 12+ characters)
flickr-organizer serve               # http://127.0.0.1:8000
```

Open it and sign in. The frontend is plain JavaScript modules (Preact + htm and uPlot, stored in `flickr_organizer/web/static/vendor`), so there's no build step:

- **Photos**: your photostream as a thumbnail grid that loads as you scroll; search titles, descriptions and tags, sort by upload, date taken, views, faves or comments, filter photos or videos.
- **Albums**: covers and counts; open one for its photos.
- **Map**: geotagged photos clustered on a world map; filter by album or year, then open a cluster to browse its photos. Map tiles use CARTO; add a key under Settings → Map (or `map.carto_key` in config). The tile provider sees the key in tile requests, so restrict it to your site in CARTO.
- **Lightbox** (click any photo): the 1024 px image, click or <kbd>Z</kbd> to zoom to 2048 px and pan; <kbd>←</kbd> <kbd>→</kbd> to browse, <kbd>I</kbd> for the info panel (description, views/faves/comments, a views-per-day chart, camera and exposure, albums, tags, latest comments), <kbd>Esc</kbd> to close.
- **Review**: the reviewer, as in `review`, with the count of photos waiting in the tab.
- **Analytics**: views, faves and comments for 7/30/90/365 days or all of it; views per day, faves and comments per day, most viewed and most faved photos, photos gaining views, and views by album, camera, year or photo/video.
- The top bar shows when the last sync ran and the next is due, with **Sync now**.

**Settings** (the admin page) holds what would otherwise be in config.yaml: API keys, the vision model and briefs, categories, location rules, standard tags, map tiles and colors, backups, and the sync schedule. Each setting shows where its value comes from; changes are validated like `check`, applied straight away (no restart) and logged. Layers, lowest to highest: built-in defaults < config.yaml < the admin page < environment variables. The settings that protect the server itself (database path, host, port, allowed hosts, trusted proxies, secure cookies) stay in config.yaml or the environment.

API keys set there are stored AES-256-GCM encrypted, each bound to its setting; the encryption key is kept outside the database, in `FLICKR_ORGANIZER_SECRET_KEY` or a key file (`~/.config/flickr-organizer/secret.key`, readable only by you; on a server, `secret.key` in the data directory; `FLICKR_ORGANIZER_KEY_FILE` moves it). They're never sent to the browser (only "set, ends …1234"), and changing one needs your password again. `flickr-organizer settings import-secrets --blank` moves the keys from config.yaml into the encrypted store; `settings show` lists what the page or the environment overrides; `settings new-key` makes a key for `FLICKR_ORGANIZER_SECRET_KEY` on a server. Keep that key safe and separate from database backups: without it, stored keys can't be read (set them again).

The page's Account section changes your password (which signs out every session), lists signed-in sessions so you can end one, and shows the change history.

A FastAPI backend serving a JSON API under `/api` and the frontend under `/`. It also runs the review worker (approvals and processing, as in `review`) and a scheduler that runs `sync` every `web.sync_every_minutes`, so the database keeps itself up to date (see "Keeping in sync"). All Flickr calls go through that one worker, in order, under the shared rate limit.

Every API route except login needs a session. It's built to be reachable from the internet (behind HTTPS):

- one owner login, password hashed with argon2id; failed logins are throttled per IP and per username
- the session cookie is HttpOnly, SameSite=Strict and Secure; only a hash of its token is stored, so a copy of the database can't be used to log in; a new password signs out every session
- every request that changes something needs the session's CSRF token (`X-CSRF-Token`)
- a strict Content-Security-Policy (scripts and styles from the app only), no framing, HSTS, and only `web.allowed_hosts` are accepted
- secrets can come from the environment (`FLICKR_API_KEY`, `FLICKR_API_SECRET`, `ANTHROPIC_API_KEY`) instead of config.yaml
- `serve` refuses to start without a login, and refuses to listen beyond localhost without secure cookies

| API | |
|---|---|
| `POST /api/auth/login`, `GET /api/auth/session`, `POST /api/auth/logout` | login (returns the CSRF token) |
| `GET /api/photos?q=&sort=&media=&album=&limit=&offset=`, `/api/photos/{id}`, `/api/albums`, `/api/albums/{id}` | gallery: image sizes up to 2048 px, EXIF, albums, engagement |
| `GET /api/analytics/overview`, `/photos?kind=`, `/tags`, `/tags/{tag}`, `/places`, `/gear`, `/audience`, `/groups`, `/impact` | the Analytics tabs (all from the local database) |
| `GET /api/map/config`, `GET /api/map/points?album=&year_from=&year_to=`, `POST /api/map/cards` | Map tile settings, geotagged photos (gzipped when supported), then cards for an opened cluster |
| `/api/review/summary`, `/queue`, `/items/{id}`, `POST /items/{id}/decide`, `POST /items/{id}/process`, `/jobs`, `/find` | the review queue |
| `GET /api/analytics/overview?days=`, `/gainers?days=`, `/breakdown?by=album\|camera\|year\|media`, `/photos/{id}` | views, faves and comments over time |
| `GET /api/sync/status`, `POST /api/sync/run {"mode": null\|"incremental"\|"photo_list"\|"full"}` | the scheduler |
| `GET /api/wx/stations`, `/live`, `/history?device=&hours=`, `/rose?device=`, `POST /api/wx/sync` | the Weather pages: WeatherFlow Tempest stations (REST history + live WebSocket, set the token in Settings → API keys) |

## Workflow: review photo by photo

```bash
python -m flickr_organizer process --limit 50   # sync, then per photo: EXIF -> classify -> plan, into a queue
python -m flickr_organizer review               # opens http://127.0.0.1:8765
```

`process` starts with a bulk `sync` (about one call per 500 photos, so every photo's title, tags and albums are current), then works through photos in small batches: fetch EXIF if it isn't cached, classify if there's no result for the current settings, and plan that photo's changes into the queue. It can be stopped and resumed; queued photos are skipped. A photo whose EXIF or classification failed isn't queued, so it's retried next run. `--refresh-each` also re-reads each photo and its albums individually (+2 calls per photo). After changing settings, `process --rebuild` re-plans everything (`--media video` or `--media photo` to limit it; photos the review tool is applying are never touched); applied and rejected changes still never come back.

`review` shows one photo at a time: a large image, what's on Flickr now, and each proposed change. Untick albums, click tags to drop them, edit the title or description, then **Approve** (Enter), **Reject all** (R), or **Skip** (→). Approving doesn't wait for Flickr: the photo goes to a background worker that makes the calls in order (at the usual rate limit) while you carry on, and the header shows how many are still applying. Failures pop up and land under "Failed" to retry. Photos and videos that `process` hasn't reached yet are under "Not processed": press **Process now** (P) to fetch EXIF, classify and plan that one in the background, then review it right there. Videos are classified from their preview frame. **Re-process** re-plans a queued photo with your current settings, and the **Go to** box jumps to any photo by ID or Flickr URL. Each photo also shows its engagement: views, faves, comments and groups, views gained since the first snapshot, license and size, the groups it's in, and (after `social`) who faved it recently and the latest comments. Sort the queue by newest, oldest, most viewed, most faved or most commented. Your choices are saved first, so if you stop the tool mid-way, unfinished photos are applied the next time you run `review`. Before writing a title or description it re-reads the photo from Flickr, so a change you made there since the queue was built isn't overwritten. Failed changes stay under "Failed" to retry. The server only listens on 127.0.0.1 and needs a per-run token, so other web pages can't use it.

## Useful commands

`flickr-organizer --help` lists every command and `flickr-organizer <command> --help` shows its options. Common maintenance commands:

```bash
flickr-organizer status
flickr-organizer check
flickr-organizer settings show
flickr-organizer settings import-file config.yaml
flickr-organizer backup --list
flickr-organizer user list
```

`settings import-file` copies settings and encrypted API keys from a YAML file into the database; settings already changed in the admin page take precedence unless `--overwrite` is supplied. `settings import-secrets --blank` stores just the API keys encrypted and clears them from the config file. `restore <file>` replaces the database and should be run with the web app stopped. On a server, stage a backup as `restore.db.gz` (or `restore.db`) in the data directory and restart the app to restore it during startup.

## Preview without queueing

```bash
python -m flickr_organizer plan            # totals of what the rules would propose right now
python -m flickr_organizer plan --list     # every change, e.g. after editing a category or a brief
```

`plan` is read-only: it uses what's already stored (it doesn't sync or classify), queues nothing, and changes nothing on Flickr.

## What's remembered

Everything lives in a local DuckDB file (`flickr_organizer.duckdb`). DuckDB lets one process use a database at a time, so stop `serve` before running a command-line command such as `sync` or `backup` against the same file (or use the app's own pages):

- Vision results are cached, so a photo is classified once. It's classified again only if you change the model, the categories, the prompt, or the description/title briefs.
- Applied, rejected and edited changes are never proposed again (dropping a tag rejects just that tag). Failed ones are proposed again. The full rules are at the top of `flickr_organizer/changes.py`.

## Tuning

- `vision.min_confidence` (default 0.75): raise it if the plan has too many false positives, lower it if it misses photos.
- Category `description`s are what the model actually reads. Spell out edge cases ("a person eating where the food is incidental doesn't count").
- Changing a category's description re-classifies the whole library on the next `classify` (the cache key includes the categories). Adding a new category does the same.
- `vision.model`: the default is a small, cheap Claude model. Swap in a larger one if its judgement isn't good enough.
- `geo.min_accuracy`: Flickr's geo accuracy runs 1 (world) to 16 (street). States need 6+ and cities need 11+ by default.

### Titles and descriptions

- Both come from the same model call as the categories, so they add only a few output tokens per photo.
- Every photo gets a proposal, including ones you titled or described yourself: the reviewer shows what it would replace, and you decide. Once you apply, edit or decline a proposal, it isn't proposed again.
- The briefs are `vision.title.instructions` and `vision.describe.instructions` (Settings → Titles / Descriptions): plain English, edit them freely. Whatever they say, the model is also told never to guess a location or name a person. Editing one re-runs the model once for the photos it applies to, and the new text is proposed; other settings keep their cached results.
- Flickr's `setMeta` call writes title and description together, so before writing either the tool re-reads the photo from Flickr and sends the other field back exactly as it is there. If you changed the title or description on Flickr since the proposal, it's left alone (and counts as declined).

### Standard tags

Tags you add by convention, each group with its own flag under `standard_tags`: date (`y2019 m07 d17`), place (`unitedstates americas california ca bakersfield`), camera (`nikon d90`), and optional album tags (every photo in Style gets `style`).

- They only add tags a photo doesn't already have, and they share the photo's single `add_tags` row, so it's still one call per photo. Flickr allows 75 tags per photo; if a photo would go over, the groups lower in the config list drop off first.
- Date tags skip photos that already have a year tag (`y2019` or `2019`) and photos where Flickr doesn't know the date taken. That flag comes from `sync`, so run `sync` once after upgrading.
- Camera tags need each photo's EXIF: `python -m flickr_organizer camera` fetches it once (one call per photo, cached, safe to stop and resume) and `process` keeps it up to date. `standard_tags.camera.albums` also adds photos to your camera albums.

### Keeping in sync, and stats history

`sync` does whatever is due, so it's cheap to run often (the web app will run it hourly):

| | when | Flickr calls (10k photos) |
|---|---|---|
| incremental | otherwise | ~4: recently updated photos, the photo count (finds deletions), the album list (only changed albums are re-read), recent activity (new comments/faves) |
| photo list | every `schedule.photo_list_every_hours` (24) | ~22: every photo re-read, so view counts and the daily snapshot stay current |
| full | every `schedule.full_every_days` (7) | ~180: everything, as a safety net |
| stats | each run, if `schedule.stats` (Flickr Pro) | ~6 per day fetched: yesterday once it's final, plus `schedule.backfill_days_per_run` older days until Flickr has none (about a year back) |

`sync --full`, `--photo-list` or `--incremental` forces one. `process` and `social` start with a quick sync (no stats). `stats` shows what's been collected (daily totals, most viewed photos); `stats --backfill 60` fetches more days now.

Per-day stats come from Flickr's own stats (`photo_daily`: views, comments and faves per photo per day; `account_daily`: daily totals), so they're exact, not estimated. Days run midnight to midnight GMT.

### Engagement data

- `sync` also records each photo's views, faves and comment count, license and original size (no extra calls), keeps a daily snapshot of those counts in `photo_stats` so you can see growth over time, and saves your account's total views in `account_stats` (1 call; needs Flickr Pro stats).
- `python -m flickr_organizer social` fetches the details: your groups and which of your photos are in each pool (about one call per group), the comments on your photos, and who faved them and when. Comments and faves are fetched only for photos whose count changed since last time, so the first run is the big one (one call per photo with comments; one per 50 faves) and later runs are small. Use `--skip-groups`, `--skip-comments`, `--skip-faves` or `--limit` to split it up; it's safe to stop and resume.

## Costs and limits

- Each photo sends one ~500px thumbnail plus a short prompt, roughly 1k input tokens. Run `--limit 50` and look at the token counts it prints before doing the whole library. Check current API pricing.
- Flickr allows 3600 API calls per hour per API key. `flickr.calls_per_second` (0.9 if unset; the example config uses 1) is shared by every command using the key: `review`, `process` and `social` running at once take turns through a small file in `~/.flickr/`, so together they stay at that rate. Each applied change costs one call, except that a retitle and a description for the same photo share one `setMeta` call (plus one call to re-read the photo first). A sync costs about one call per 500 photos plus one per album.
- Reverse geocoding runs offline and returns the nearest town, so a photo taken right at a border can occasionally land in the neighbouring country. That's one reason to review before applying.

## Adding a vision provider

Subclass `flickr_organizer.vision.base.VisionProvider`, implement `classify(image, media_type, prompt, schema) -> dict`, and register the class in `flickr_organizer/vision/__init__.py`. If a server speaks the OpenAI API, it already works with `provider: openai` + `base_url`.

## Tests

```bash
uv run python -m unittest discover tests
```

The tests run the whole sync → classify → plan → queue → review → apply → re-plan loop against a fake Flickr, a fake geocoder and a fake model (`tests/fakes.py`), one file per area.
