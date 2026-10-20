# flickr-organizer

Adds your Flickr photos to the right albums and tags, using:

- **Geotags**: country albums, US state albums, and a few city albums (matched by distance from the city center)
- **A vision LLM**: subject albums like Food, Landscapes, Animals, plus optional descriptive tags. Claude is the default; OpenAI or a local Ollama model also work.

- **Renames (optional, off by default)**: photos that still have a camera-default or blank title (`IMG_1234`, `DSC01234.JPG`, `PXL_2024…`, `20240101_123456`, "Untitled") get retitled with their location, e.g. "Houston, TX" or "Luanda, Angola". Turn it on with `rename.enabled: true`.
- **Descriptions (optional, off by default)**: photos with a blank description get a 1–2 sentence one written by the vision model. Turn it on with `vision.describe.enabled: true`.

Apart from renaming generic titles and filling blank descriptions, it only ever **adds**. It never removes a tag or takes a photo out of an album. Every change waits in a queue until you approve it.

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

## Workflow: review photo by photo

```bash
python -m flickr_organizer process --limit 50   # sync, then per photo: EXIF -> classify -> plan, into a queue
python -m flickr_organizer review               # opens http://127.0.0.1:8765
```

`process` starts with a bulk `sync` (about one call per 500 photos, so every photo's title, tags and albums are current), then works through photos in small batches: fetch EXIF if it isn't cached, classify if there's no result for the current settings, and plan that photo's changes into the queue. It can be stopped and resumed; queued photos are skipped. A photo whose EXIF or classification failed isn't queued, so it's retried next run. `--refresh-each` also re-reads each photo and its albums individually (+2 calls per photo). After changing settings, `process --rebuild` re-plans everything (`--media video` or `--media photo` to limit it; photos the review tool is applying are never touched); applied and rejected changes still never come back.

`review` shows one photo at a time: a large image, what's on Flickr now, and each proposed change. Untick albums, click tags to drop them, edit the title or description, then **Approve** (Enter), **Reject all** (R), or **Skip** (→). Approving doesn't wait for Flickr: the photo goes to a background worker that makes the calls in order (at the usual rate limit) while you carry on, and the header shows how many are still applying. Failures pop up and land under "Failed" to retry. Photos and videos that `process` hasn't reached yet are under "Not processed": press **Process now** (P) to fetch EXIF, classify and plan that one in the background, then review it right there. Videos are classified from their preview frame, even with `include_videos: false`. **Re-process** re-plans a queued photo with your current settings, and the **Go to** box jumps to any photo by ID or Flickr URL. Each photo also shows its engagement: views, faves, comments and groups, views gained since the first snapshot, license and size, the groups it's in, and (after `social`) who faved it recently and the latest comments. Sort the queue by newest, oldest, most viewed, most faved or most commented. Your choices are saved first, so if you stop the tool mid-way, unfinished photos are applied the next time you run `review`. Before writing a title or description it re-reads the photo from Flickr, so a change you made there since the queue was built isn't overwritten. Failed changes stay under "Failed" to retry. The server only listens on 127.0.0.1 and needs a per-run token, so other web pages can't use it.

## Preview without queueing

```bash
python -m flickr_organizer plan            # totals of what the rules would propose right now
python -m flickr_organizer plan --list     # every change, e.g. after editing a category or a brief
```

`plan` is read-only: it uses what's already stored (it doesn't sync or classify), queues nothing, and changes nothing on Flickr.

## What's remembered

Everything lives in a local SQLite file (`flickr_organizer.db`):

- Vision results are cached, so a photo is classified once. It's classified again only if you change the model, the categories, the prompt, or the description/title briefs.
- Applied, rejected and edited changes are never proposed again (dropping a tag rejects just that tag). Failed ones are proposed again. The full rules are at the top of `flickr_organizer/changes.py`.

## Tuning

- `vision.min_confidence` (default 0.75): raise it if the plan has too many false positives, lower it if it misses photos.
- Category `description`s are what the model actually reads. Spell out edge cases ("a person eating where the food is incidental doesn't count").
- Changing a category's description re-classifies the whole library on the next `classify` (the cache key includes the categories). Adding a new category does the same.
- `vision.model`: the default is a small, cheap Claude model. Swap in a larger one if its judgement isn't good enough.
- `geo.min_accuracy`: Flickr's geo accuracy runs 1 (world) to 16 (street). States need 6+ and cities need 11+ by default.

### Renames

- A title is replaced only if it matches a generic pattern. Anything you typed yourself is never touched. You can add your own patterns with `rename.extra_generic_patterns`.
- The city name comes from the nearest town in the geocoder's data. A city in `geo.cities` takes priority, so a photo in a suburb still reads "Austin, TX". Give that entry a `name:` if the title should differ from the album name.
- City-level precision is required by default. Set `fallback_to_region: true` to title less precise photos "Texas" or "Bavaria, Germany" instead.
- In the review page you can edit each proposed title or skip it.
- When applying, a photo you retitled yourself since the last `sync` is left alone.
- Flickr's `setMeta` call writes title and description together, so before writing either the tool re-reads the photo from Flickr and sends the other field back exactly as it is there.

### Descriptions

- Only photos whose description is blank get one. Anything you wrote yourself is never touched.
- The description comes from the same model call as the categories, so it adds only a few output tokens per photo. Turning it on re-classifies photos that were cached without a description, once. Turning it off keeps the cache.
- The writing brief is `vision.describe.instructions` in config.yaml: plain English, edit it freely (the default asks for a short, understated gallery-caption style). Whatever it says, the model is also told never to guess a location or name a person. Editing it re-runs the model once for the photos it applies to; other settings keep their cached results. With `describe.reword: true` (the default), descriptions this tool already wrote, if they're still exactly as written, get a re-worded proposal after you change the brief, shown in the review tool with the text it replaces. Descriptions you wrote, or edited after the tool wrote them, are never touched, and rejected ones stay rejected.
- `vision.title` proposes titles the same way, with its own `instructions`. `replace` decides which titles it may replace: `generic` (camera file names, blank), `generic_or_place` (also titles that just name the place, like "Seattle, WA" or "Bellagio 175"), or `all`. Titles with words of your own are kept unless it's `all`, and every proposal goes through review. A model title wins over the rename section's "City, ST".
- In the review page you can edit each proposed description or skip it. Each photo gets one proposal: once you apply or reject it, it isn't proposed again.
- When applying, a photo whose description changed on Flickr since the proposal (say, you wrote one) is left alone.

### Standard tags

Tags you add by convention, each group with its own flag under `standard_tags`: date (`y2019 m07 d17`), place (`unitedstates americas california ca bakersfield`), camera (`nikon d90`), album tags (every photo in Style gets `style`), coordinates (`lat300 lon950`), your name/handles, and filler words.

- They only add tags a photo doesn't already have, and they share the photo's single `add_tags` row, so it's still one call per photo. Flickr allows 75 tags per photo; if a photo would go over, the groups lower in the config list drop off first.
- Date tags skip photos that already have a year tag (`y2019` or `2019`) and photos where Flickr doesn't know the date taken. That flag comes from `sync`, so run `sync` once after upgrading.
- Camera tags need each photo's EXIF: `python -m flickr_organizer camera` fetches it once (one call per photo, cached, safe to stop and resume) and `process` keeps it up to date. `camera.albums` also adds photos to your camera albums.

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
