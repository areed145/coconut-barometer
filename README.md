# Coconut Barometer

Coconut Barometer is a personal website for browsing and organizing your Flickr library. It includes a photo gallery (public photos for visitors, everything once you sign in), map, analytics, and a review workflow for proposed titles, descriptions, tags and albums, plus pages for a WeatherFlow Tempest weather station and APRS tracking. The Python package is `coconut_barometer` and the command is `coconut-barometer` (both were `flickr_organizer` / `flickr-organizer` before the rename).

Adds your Flickr photos to the right albums and tags, using:

- **Geotags**: country albums, US state albums, and a few city albums (matched by distance from the city center)
- **A vision LLM**: subject albums like Food, Landscapes, Animals, descriptive tags, a title and a description. Claude is the default; OpenAI or a local Ollama model also work.
- **Your conventions**: standard tags for dates, places and camera names, plus camera albums.

Every photo (and video, from its preview frame) gets all four kinds of proposal: a title, a description, tags and albums. You accept, edit or decline each one in the reviewer; nothing is switched off in settings. It never takes a photo out of an album. Titles and descriptions are proposed as replacements, and so are tags: each photo's proposal is its whole tag list (new ones in bold, the ones it keeps among them), and the reviewer shows the tags it would remove. Untick any tag to leave it out. Nothing changes on Flickr until you approve it.

## Setup

Uses [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`). `uv sync` creates `.venv` with the exact versions in `uv.lock` and installs this project in editable mode. Run commands with `uv run coconut-barometer …`, or activate `.venv` and use `python -m coconut_barometer …` as below.

```bash
cd coconut-barometer
uv sync                     # add --extra openai for OpenAI/Ollama; --no-dev on a server
source .venv/bin/activate

# Settings live in the database and are edited on the web app's Settings page. To start from
# the example categories, briefs and rules, load them in (API keys in the file are stored encrypted):
python -m coconut_barometer settings import-file config.example.yaml
#   API keys: Settings → Connections, or flickr.api_key / flickr.api_secret / vision.api_key in the file before importing
#   Flickr key: https://www.flickr.com/services/apps/create/
#   Anthropic key: https://console.anthropic.com/
python -m coconut_barometer auth      # opens the browser; paste the code back (or Settings → Connections → Connect Flickr)
python -m coconut_barometer sync      # caches your photo list + album membership locally
python -m coconut_barometer albums    # lists your album titles, so you can match them in Settings
python -m coconut_barometer check     # validates the settings and checks album names against your library
```

Every command checks the settings when it starts and stops with a list of problems (a misspelled setting, a wrong type, a value out of range, an unknown `{placeholder}`, a broken regex) rather than failing partway through a run. `check` also shows warnings, such as album names that match no album, or missing keys.

In **Settings → Photos**, make the category album names (Subjects), the US state album title and the city albums (Location) match the albums you already have. Albums are matched by title, ignoring case, accents and punctuation. If a title doesn't match, that album is skipped. Nothing gets created unless you turn on **Albums → Create missing albums**.

## Web app

To run it on Render from GitHub (HTTPS, a persistent disk, the hourly scheduler), see [docs/DEPLOY.md](docs/DEPLOY.md). Backups: `coconut-barometer backup` (and `backup --list`), `restore <file>` with the app stopped, or **Settings → Backups → Restore a backup** in the web app (it restarts into it). A new server with no login opens on a setup page: create a login or drag in a backup, with the setup code from the server's log.

```bash
coconut-barometer user add <name>     # the login (or create it on the setup page; 12+ characters)
coconut-barometer serve               # http://127.0.0.1:8000
```

**Local mode.** On your own machine (listening on 127.0.0.1, not on Render), `serve` sends nothing as the station: no APRS-IS login and no CWOP reports, no push notifications, no GitHub error reports, no Flickr sync or review changes applied, and no backups. Each is logged instead, shows as "off (local mode)" on the Status page, and a **local** badge sits by the logo. Everything that's only read still runs (WeatherFlow, radar, satellite, the Weather map's layers), so you can look at the site against a copy of the live database without the copy and the live server both reporting. `--local` or `--production` (or `COCONUT_LOCAL=1`/`0`) overrides the choice. **A server you run yourself on 127.0.0.1 behind a proxy (Caddy) needs `--production`.** Render and anything listening on another address are live already.

Open it and sign in. The frontend is plain JavaScript modules (Preact + htm and uPlot, stored in `coconut_barometer/web/static/vendor`), so there's no build step:

Photos, Weather and APRS are public: visitors see public photos only, and the rest (Analytics, Review, Status, Settings) needs the login.

- **Home** (`/`, and the logo): a large link to Photos, Weather and APRS, each with a glance at it now (the newest photo, each station's temperature and wind, when each callsign was last heard); signed in, also **Radar**: a picture of the latest radar and storm cells around the stations (drawn on the server once per frame, on CARTO's tiles when there's a key), when it's from, the nearest rain and the storms nearby, linking to Weather → Map; below them, **Status**: a dot per connection (WeatherFlow, APRS-IS, radar and satellite; signed in, also CWOP, Flickr, the vision model and backups) with its state, folded to "All systems normal" when everything's green. Signed in, the worst of them is the color of the Status tab's dot; visitors see a dot by the logo when anything's amber or red. Then **Highlights** (days since it rained, this month's rain and extremes, this year's 100 °F days or freezing nights, the last lightning within 10 miles and this month's strikes, the igate's farthest station heard this month, photos taken this year and the top upload this month, each linking to its page) and **On this day**: photos taken on today's date in earlier years; signed in, links to Review, Analytics, Status and Settings.
- **Photos**: your photostream as a thumbnail grid that loads as you scroll; search titles, descriptions and tags, sort by upload, date taken, views, faves or comments, filter photos or videos, or **On this day** (taken on today's date, any year).
- **Albums**: covers and counts; open one for its photos.
- **Map**: geotagged photos clustered on a world map; filter by album or year, then open a cluster to browse its photos. Map tiles use CARTO; add a key under Settings → Connections → Maps. The tile provider sees the key in tile requests, so restrict it to your site in CARTO.
- **Lightbox** (click any photo): the 1024 px image, click or <kbd>Z</kbd> to zoom to 2048 px and pan; <kbd>←</kbd> <kbd>→</kbd> to browse, <kbd>I</kbd> for the info panel (description, views/faves/comments, a views-per-day chart, camera and exposure, albums, tags, latest comments), <kbd>Esc</kbd> to close.
- **Share** (in the lightbox, and on an album): a link of its own, `/p/<photo id>` or `/a/<album id>`, that shows a preview (title, description, image) wherever it's pasted, then opens the photo or album. Public photos and albums only: a private one gets the plain page, as if it weren't there. `/sitemap.xml` lists them all for search engines (`/robots.txt` points to it).
- **Review**: the photos in review, one at a time (see "Workflow" below), with how many are waiting in the tab.
- **Analytics**: views, faves and comments for 7/30/90/365 days or all of it; views per day, faves and comments per day, most viewed and most faved photos, photos gaining views, and views by album, camera, year or photo/video.
- **Visitors** (signed in, its own page): this site's own visitors, on every page of it: visitors and page views per day, pages, where they came from, cities and countries, devices, photos opened here, shared links previewed (iMessage, Slack...), and the latest visits. No cookies are set for visitors and no IP is stored (each is a fingerprint that changes daily), your own devices aren't counted once you've signed in on them, and browsers sending Global Privacy Control aren't counted. Cities come from DB-IP's free city database, downloaded by the app each month (~130 MB, in `geoip/` next to the database, not in backups). Settings → Visitors turns it off or changes how long page views are kept (90 days).
- **Weather**: WeatherFlow Tempest stations (set the token in Settings → Connections). **Live** shows each station now, with the last three hours as sparklines; each reading links to its chart in History. **History** charts temperature, pressure, wind, rain and lightning, sun and sky, by day and as a calendar, over 24 hours to a year or a custom range. **Records** has each station's records, all-time and by month (hottest, coldest, top gust, wettest day and hour, highest UV, pressure extremes, nearest lightning), each linking to that day in History, plus today's sunrise, sunset and daylight, and this date in earlier years. **Map** shows the stations with weather layers: radar and storm tracks, watches and warnings, fronts, temperature and wind, satellite, and lightning (where GOES-East's lightning mapper saw it in the last 15 minutes, from NOAA's open data; the stations' own strikes are rings at the distance each measured, since a Tempest can't tell direction). The stations themselves, the live stream's status and **Sync now** are under Settings → Weather.
- **APRS**: **Tracking** follows the callsigns in Settings → APRS, heard live from APRS-IS; **Igate** maps what APRS-IS credits your igate with gating, and its effective range. **Weather reports (CWOP)**: give a station a callsign-SSID (e.g. KK6GPV-13) and your APRS-IS passcode in Settings → APRS, and every 10 minutes (5–60) it sends its readings and exact position to the Citizen Weather Observer Program over APRS-IS (shown on aprs.fi; NOAA uses them). That's a separate, logged-in connection; tracking stays receive-only. Nothing is sent until a station has a callsign, and never a reading over 15 minutes old.
- **Status**: how everything the app depends on is doing, top to bottom: the same panel as Home (open when anything's amber or red, with where to fix it), the job running now and the ones waiting (syncs, applies, re-processing), each service's health, rate limit and use, and a log of every call, stream message and finished job (pick a service to filter it). The tab's dot is the worst connection's color and pulses while the worker is busy. **Sync now** for Flickr is in Settings → Photos → Sync.

**Settings** (the admin page) holds everything: API keys, the vision model and briefs, categories, location rules, standard tags, map tiles and colors, weather and APRS, backups, and the sync schedule. Each section has its own address (`#/settings/weather`). Each setting shows where its value comes from; changes are validated like `check`, applied straight away (no restart) and logged. Layers, lowest to highest: built-in defaults < the admin page < environment variables (the database path and the platform's port and host names only). The settings that protect the server itself (database path, host, port, allowed hosts, trusted proxies, secure cookies) can't be changed on the page: they come from the defaults, the environment (`DATABASE_PATH`, `DATA_DIR`, `PORT`, `ALLOWED_HOSTS`, and Render's own variables).

API keys set there are stored AES-256-GCM encrypted, each bound to its setting; the encryption key is kept outside the database and the backups, in a key file (`~/.config/coconut-barometer/secret.key`, readable only by you, or the `flickr-organizer` one from before the rename; on a server, `secret.key` in the data directory). They're never sent to the browser (only "set, ends …1234"), and changing one needs your password again. API keys can't come from environment variables. `settings show` lists what the page or the environment sets. Keep the key safe and separate from database backups (Settings → Backups → **Download encryption key**): without it, stored keys can't be read (set them again).

The page's Account section changes your password (which signs out every session), lists signed-in sessions so you can end one, and shows the change history.

A FastAPI backend serving a JSON API under `/api` and the frontend under `/`. It also runs the review worker (applying and processing) and a scheduler that runs `sync` every `web.sync_every_minutes`, so the database keeps itself up to date (see "Keeping in sync"). All Flickr calls go through that one worker, in order, under the shared rate limit.

The API behind the public pages (photos, albums, map, weather, APRS, share links) answers visitors with public data only, and reads only; everything else, and every change, needs a session. It's built to be reachable from the internet (behind HTTPS):

- one owner login, password hashed with argon2id; failed logins are throttled per IP and per username
- the session cookie is HttpOnly, SameSite=Strict and Secure; only a hash of its token is stored, so a copy of the database can't be used to log in; a new password signs out every session
- every request that changes something needs the session's CSRF token (`X-CSRF-Token`)
- a strict Content-Security-Policy (scripts and styles from the app only), no framing, HSTS, and only `web.allowed_hosts` are accepted
- API keys are stored encrypted (see Settings); they're never read from the environment
- `serve` refuses to start without a login, and refuses to listen beyond localhost without secure cookies

| API | |
|---|---|
| `POST /api/auth/login`, `GET /api/auth/session`, `POST /api/auth/logout` | login (returns the CSRF token) |
| `GET /api/photos?q=&sort=&media=&album=&limit=&offset=`, `/api/photos/{id}`, `/api/albums`, `/api/albums/{id}` | gallery: image sizes up to 2048 px, EXIF, albums, engagement |
| `GET /api/analytics/overview`, `/photos?kind=`, `/tags`, `/tags/{tag}`, `/places`, `/gear`, `/audience`, `/groups`, `/impact` | the Analytics tabs (all from the local database) |
| `GET /api/map/config`, `GET /api/map/points?album=&year_from=&year_to=`, `POST /api/map/cards` | Map tile settings, geotagged photos (gzipped when supported), then cards for an opened cluster |
| `/api/review/summary`, `/queue`, `/items/{id}`, `POST /items/{id}/send`, `/decide` (apply), `/ignore`, `/process` (re-process), `/undo`, `/bulk`, `POST /replan`, `/applied`, `/jobs`, `/find` | the review queue, Bulk and Applied |
| `GET /api/analytics/gainers?days=`, `/breakdown?by=album\|camera\|year\|media`, `/photos/{id}` | views, faves and comments over time |
| `GET/PUT /api/admin/settings`, `PUT /secrets/{name}`, `/sessions`, `/backups`, `POST /restore`, `/flickr/connect` | Settings |
| `GET /api/setup`, `POST /api/setup/user`, `/restore` | the first-run setup page (with the code from the server's log) |
| `GET /api/status`, `/jobs`, `/activity?since=&limit=` | the Status page: each connection's health (public: visitors get the public ones, no errors; also Home's panel and the top bar's dot), the worker's jobs, and the log of every call |
| `GET /api/highlights` | Home's Highlights (weather streaks, the igate's farthest, this year's photos; public, no positions) |
| `GET /api/sync/status`, `POST /api/sync/run {"mode": null\|"incremental"\|"photo_list"\|"full"}` | the scheduler |
| `GET /api/wx/stations`, `/live`, `/history?device=&hours=`, `/days?device=`, `/records?device=`, `/rose?device=`, `/lightning?device=`, `/map`, `/glm?bbox=&zoom=&since=`, `/radar/snapshot.png?theme=&scale=` and `/radar/glance` (signed in), `POST /api/wx/sync` | the Weather pages: WeatherFlow Tempest stations (REST history + live WebSocket; the token is in Settings → Connections) |
| `GET /api/aprs/stations`, `/track?callsign=&hours=`, `/packets?callsign=` | the APRS page: positions of the callsigns in Settings → APRS, heard live from APRS-IS |
| `GET /api/aprs/igate?hours=&direct=&count=packets\|stations&cell=` | APRS → Igate: the packets APRS-IS credits the igate in Settings → APRS with gating, on a lat/lon grid and by distance, with a Weibull fit whose 90% distance is the effective range |

## Workflow: review photo by photo

```bash
python -m coconut_barometer process --limit 50   # sync, then per photo: EXIF -> classify -> plan, into review
coconut-barometer serve                          # then Photos → Review at http://127.0.0.1:8000
```

`process` starts with a bulk `sync` (about one call per 500 photos, so every photo's title, tags and albums are current), then works through the photos never processed, in small batches: fetch EXIF if it isn't cached, classify if there's no result for the current settings, and plan that photo's changes into review. It can be stopped and resumed; photos already processed (or in review) are skipped. A photo whose EXIF or classification failed isn't queued, so it's retried next run. `--refresh-each` also re-reads each photo and its albums individually (+2 calls per photo). After changing settings, `process --rebuild` re-plans the photos in review (`--media video` or `--media photo` to limit it; photos being applied are never touched).

A photo is in review from being sent there until you apply or ignore it. It gets there three ways: `process`, a sync bringing a new upload, or **Send to Review** on a photo in the gallery. **Settings → Photos → Review** decides what happens then, whichever way it came: processed right away (the default), or when you open it. There's no memory between visits: every processing proposes afresh, skipping only what's already true on Flickr.

**Photos → Review** shows the photos in review one at a time: a large image, what's on Flickr now, and each proposed change. Untick albums, click tags to drop them, edit the title or description, then:

- **Apply** (Enter): writes the changes you kept, and the photo leaves review. It doesn't wait for Flickr: a background worker makes the calls in order (at the usual rate limit) while you carry on. A change that fails is tried once more; if it fails again, the photo stays in review with just the changes that didn't go through and why, to apply again later (or re-process).
- **Ignore** (I): the photo leaves review and nothing changes on Flickr.
- **Re-process** (P): a fresh look with your current settings (re-reads the photo, asks the model again); it stays in review. It goes to the front of the worker's line, ahead of syncs and photos waiting their turn (behind the job already running, and any re-process you asked for before it). Like an apply, it's saved until it's done, so if the app stops first it runs when it starts again; so do an Undo and **Re-plan all in review**.
- **Bulk** (Review → Bulk): one kind of change across every ready photo (locations by confidence, tags, titles, descriptions, albums, groups), applied or dropped many at a time; each photo's other changes stay in review. **Re-plan all in review** plans them again with your current settings (e.g. new tags to always add or remove) from what's already known, with no vision or Flickr calls.
- **Groups**: Flickr enforces each group's rules. A group with a moderator takes the photo into its queue, and that counts as done. When you've posted as many as a group allows for now (so many a day, a week...), the photo stays in review saying when the group takes more, and that group isn't asked again until then (Bulk → Groups shows it); other groups carry on. A group that won't take the photo at all (its content rules, a full pool, or the photo already in as many groups as Flickr allows) isn't asked again, and isn't proposed again for that photo; the photo stays in review saying why, to untick it.
- **Undo** (Review → Applied): a photo's latest apply, put back: the title, description, tags, album, location or group it changed. Anything changed on Flickr since is left as it is.

**Skip** (→) moves on without deciding. A photo with nothing to change stays until you ignore or re-process it. Videos are classified from their preview frame. The **Go to** box jumps to any photo by ID or Flickr URL (one not in review can be sent there). Each photo also shows its engagement: views, faves, comments and groups, views gained since the first snapshot, license and size, the groups it's in, and (after `social`) who faved it recently and the latest comments. Sort the queue by newest, oldest, most viewed, most faved or most commented. Your choices are saved first, so if the app stops mid-way, unfinished photos are applied when it starts again. Before writing a title, description or tag replacement it re-reads the photo from Flickr, so a change you made there since the proposal isn't overwritten (that change stays in review, saying so).

## Useful commands

`coconut-barometer --help` lists every command and `coconut-barometer <command> --help` shows its options. Common maintenance commands:

```bash
coconut-barometer status
coconut-barometer check
coconut-barometer settings show
coconut-barometer settings import-file config.example.yaml
coconut-barometer backup --list
coconut-barometer user list
```

`settings import-file` copies settings and encrypted API keys from a YAML file into the database; settings already changed in the admin page take precedence unless `--overwrite` is supplied. `restore <file>` replaces the database and should be run with the web app stopped. On a server, stage a backup as `restore.db.gz` (or `restore.db`) in the data directory and restart the app to restore it during startup.

## Preview without queueing

```bash
python -m coconut_barometer plan            # totals of what the rules would propose right now
python -m coconut_barometer plan --list     # every change, e.g. after editing a category or a brief
```

`plan` is read-only: it uses what's already stored (it doesn't sync or classify), queues nothing, and changes nothing on Flickr.

## What's remembered

Everything lives in a local DuckDB file: `db.duckdb` in the current folder, `DATABASE_PATH` if set, or in `DATA_DIR` on a server. DuckDB lets one process use a database at a time, so stop `serve` before running a command-line command such as `sync` or `backup` against the same file (or use the app's own pages):

- Vision results are cached, so a photo is classified once. It's classified again only if you change the model, the categories, the prompt, or the description/title briefs.
- Applied, rejected and edited changes are never proposed again (dropping a tag rejects just that tag). Failed ones are proposed again. The full rules are at the top of `coconut_barometer/changes.py`.

## Tuning

These are in Settings → Photos (the model is in Connections).

- `vision.min_confidence` (default 0.75): raise it if the plan has too many false positives, lower it if it misses photos.
- Category `description`s are what the model actually reads. Spell out edge cases ("a person eating where the food is incidental doesn't count").
- Changing a category's description re-classifies the whole library on the next `classify` (the cache key includes the categories). Adding a new category does the same.
- `vision.model`: the default is a small, cheap Claude model. Swap in a larger one if its judgement isn't good enough.
- `geo.min_accuracy` (Location → Geotag precision): Flickr's geo accuracy runs 1 (world) to 16 (street). States need 6+ and cities need 11+ by default.

### Titles and descriptions

- Both come from the same model call as the categories, so they add only a few output tokens per photo.
- Every photo gets a proposal, including ones you titled or described yourself: the reviewer shows what it would replace, and you decide.
- The briefs are `vision.title.instructions` and `vision.describe.instructions` (Settings → Photos → Writing): plain English, edit them freely. Whatever they say, the model is also told never to guess a location or name a person. Editing one re-runs the model once for the photos it applies to, and the new text is proposed; other settings keep their cached results.
- Flickr's `setMeta` call writes title and description together, so before writing either the tool re-reads the photo from Flickr and sends the other field back exactly as it is there. If you changed the title or description on Flickr since the proposal, it's left alone, and the photo stays in review saying so.

### Standard tags

Tags you add by convention, each group switched on in Settings → Photos: date (`y2019 m07 d17`) and camera (`nikon d90`) under Cameras & dates, place (`unitedstates americas california ca bakersfield`) under Location.

- They share the photo's single tags row with the model's tags, so it's still one call per photo. Flickr allows 75 tags per photo; if a photo would go over, the later groups drop off first.
- Date tags skip photos where Flickr doesn't know the date taken. That flag comes from `sync`, so run `sync` once after upgrading.
- Camera tags need each photo's EXIF: `python -m coconut_barometer camera` fetches it once (one call per photo, cached, safe to stop and resume) and `process` keeps it up to date. Camera albums (regex on the camera name → album) also add photos to your camera albums.

### Keeping in sync, and stats history

`sync` does whatever is due, so it's cheap to run often (the web app runs it every `web.sync_every_minutes`, 60 by default; Settings → Photos → Sync):

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
- `python -m coconut_barometer social` fetches the details: your groups and which of your photos are in each pool (about one call per group), the comments on your photos, and who faved them and when. Comments and faves are fetched only for photos whose count changed since last time, so the first run is the big one (one call per photo with comments; one per 50 faves) and later runs are small. Use `--skip-groups`, `--skip-comments`, `--skip-faves` or `--limit` to split it up; it's safe to stop and resume.

## Costs and limits

- Each photo sends one ~500px thumbnail plus a short prompt, roughly 1k input tokens. Run `--limit 50` and look at the token counts it prints before doing the whole library. Check current API pricing.
- Flickr allows 3600 API calls per hour per API key. The app makes at most 1.1 calls a second (about 3960 an hour, deliberately a little over the stated limit), shared by everything using the key: the web app, `process` and `social` running at once take turns through a small file in `~/.flickr/`, so together they stay at that rate. Each applied change costs one call, except that a retitle and a description for the same photo share one `setMeta` call (plus one call to re-read the photo first). A sync costs about one call per 500 photos plus one per album.
- Reverse geocoding runs offline and returns the nearest town, so a photo taken right at a border can occasionally land in the neighbouring country. That's one reason to review before applying.

## Adding a vision provider

Subclass `coconut_barometer.vision.base.VisionProvider`, implement `classify(image, media_type, prompt, schema) -> dict`, and register the class in `coconut_barometer/vision/__init__.py`. If a server speaks the OpenAI API, it already works with `provider: openai` + `base_url`.

## Tests

```bash
uv run python -m unittest discover tests
```

The tests run the whole sync → classify → plan → queue → review → apply → re-plan loop against a fake Flickr, a fake geocoder and a fake model (`tests/fakes.py`), one file per area.
