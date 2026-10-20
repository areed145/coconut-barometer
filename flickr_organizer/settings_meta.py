"""Labels, help text and sections for the admin page, by setting path. A setting missing
here still appears (under its own path), and tests check every setting has an entry."""
from __future__ import annotations

SECTIONS = [
    ("keys", "API keys", "Stored encrypted. Changing one asks for your password."),
    ("vision", "Vision model", "The model that looks at each photo: categories, tags, titles and descriptions."),
    ("writing", "Titles and descriptions", "What the model is asked to write. Editing a brief re-runs the model "
                                           "once for the photos it applies to."),
    ("categories", "Categories", "Albums filled by subject. The description is what the model reads."),
    ("location", "Location", "Albums and tags from the geotag."),
    ("renames", "Location titles", "Retitle camera-default titles (IMG_1234) as \"City, ST\". A model title wins when "
                                   "titles are on."),
    ("tags", "Standard tags", "Tags you add by convention. Each group has its own switch."),
    ("schedule", "Sync schedule", "How often each kind of sync runs (`sync` and the web app do whatever is due)."),
    ("web", "Web app", "The settings in grey protect the server itself and stay in config.yaml or the environment."),
    ("flickr", "Flickr", ""),
]


def _m(section, label, help=""):
    return {"section": section, "label": label, "help": help}


META = {
    # keys
    "flickr.api_key": _m("keys", "Flickr API key", "From flickr.com/services/apps/create."),
    "flickr.api_secret": _m("keys", "Flickr API secret"),
    "vision.api_key": _m("keys", "Vision API key", "Anthropic (or OpenAI) key. Not needed for Ollama."),
    # flickr
    "flickr.calls_per_second": _m("flickr", "Flickr calls per second",
                                  "Shared by every command using the key. 1 = Flickr's 3600/hour limit."),
    "database": _m("flickr", "Database file"),
    "albums.create_missing": _m("flickr", "Create missing albums",
                                "Off: only add photos to albums you already have. On: also propose new albums."),
    # vision
    "vision.enabled": _m("vision", "Use the vision model"),
    "vision.provider": _m("vision", "Provider", "claude / anthropic, openai, or ollama (local)."),
    "vision.model": _m("vision", "Model", "e.g. claude-haiku-5-5 (cheapest) or claude-sonnet-5-5."),
    "vision.base_url": _m("vision", "Server URL", "Only for Ollama or another OpenAI-compatible server."),
    "vision.thumbnail": _m("vision", "Image size sent", "url_m (500 px) is plenty for classifying."),
    "vision.concurrency": _m("vision", "Photos at once"),
    "vision.min_confidence": _m("vision", "Minimum confidence",
                                "0-1. Categories scored below this aren't proposed. No re-run needed to change it."),
    "vision.use_photo_text": _m("vision", "Show the model your title, description and tags"),
    "vision.use_location": _m("vision", "Show the model the geotag's town and country", "Helps it name landmarks."),
    "vision.include_videos": _m("vision", "Classify videos", "From their preview frame."),
    "vision.extra_tags.enabled": _m("vision", "Descriptive tags", "Landmarks, aircraft types, species, things in view."),
    "vision.extra_tags.max": _m("vision", "Descriptive tags per photo"),
    "vision.extra_tags.prefix": _m("vision", "Tag prefix", "e.g. ai: to tell model tags apart."),
    # writing
    "vision.describe.enabled": _m("writing", "Write descriptions", "For photos whose description is blank."),
    "vision.describe.max_words": _m("writing", "Description length (words)"),
    "vision.describe.instructions": _m("writing", "Description brief",
                                       "{max_words} is filled in. Empty uses the built-in brief. The model is always "
                                       "told never to guess a location or name a person."),
    "vision.describe.reword": _m("writing", "Re-word descriptions this tool wrote",
                                 "After the brief changes, if you haven't edited them."),
    "vision.title.enabled": _m("writing", "Propose titles"),
    "vision.title.max_words": _m("writing", "Title length (words)"),
    "vision.title.replace": _m("writing", "Which titles may be replaced",
                               "generic: camera file names; generic_or_place: also \"Seattle, WA\", \"Bellagio 175\"; "
                               "all: every photo (you still approve each)."),
    "vision.title.instructions": _m("writing", "Title brief", "{max_words} is filled in. Empty uses the built-in brief."),
    # categories
    "vision.categories": _m("categories", "Categories"),
    # location
    "geo.enabled": _m("location", "Location albums"),
    "geo.min_accuracy.country": _m("location", "Geotag precision for countries", "1 world … 16 street."),
    "geo.min_accuracy.state": _m("location", "Geotag precision for states"),
    "geo.min_accuracy.city": _m("location", "Geotag precision for cities and towns"),
    "geo.countries.auto_match": _m("location", "Match country names to albums automatically"),
    "geo.countries.aliases": _m("location", "Country albums by code",
                                "Country code → your album title, e.g. TH → \"Thailand, Vietnam, Laos\"."),
    "geo.us_states.enabled": _m("location", "US state albums"),
    "geo.us_states.title": _m("location", "US state album title", "{state} is filled in, e.g. \"{state}, USA\"."),
    "geo.cities": _m("location", "City albums", "Photos within radius_km of a point go in the album."),
    "geo.add_tags": _m("location", "Also tag country / state / city"),
    # renames
    "rename.enabled": _m("renames", "Retitle camera-default titles with the location"),
    "rename.min_accuracy": _m("renames", "Geotag precision needed"),
    "rename.us_format": _m("renames", "US title", "{city} {state} {state_abbr} {region} {country} {cc}"),
    "rename.intl_format": _m("renames", "Elsewhere title"),
    "rename.fallback_to_region": _m("renames", "Use the region when the town isn't precise enough"),
    "rename.us_region_format": _m("renames", "US region title"),
    "rename.intl_region_format": _m("renames", "Elsewhere region title"),
    "rename.generic_patterns": _m("renames", "Generic title patterns", "Regexes; empty uses the built-in list."),
    "rename.extra_generic_patterns": _m("renames", "More generic title patterns", "Regexes added to the list."),
    # schedule
    "schedule.full_every_days": _m("schedule", "Full sync every (days)", "Everything, ~180 calls for 10k photos."),
    "schedule.photo_list_every_hours": _m("schedule", "Re-read every photo every (hours)",
                                          "View counts and the daily snapshot, ~22 calls."),
    "schedule.stats": _m("schedule", "Per-day stats", "Needs Flickr Pro."),
    "schedule.backfill_days_per_run": _m("schedule", "Past days fetched per sync", "Until Flickr has none (~a year)."),
    # web
    "web.host": _m("web", "Listen address"),
    "web.port": _m("web", "Port"),
    "web.allowed_hosts": _m("web", "Allowed host names"),
    "web.secure_cookies": _m("web", "Secure cookies (HTTPS only)"),
    "web.trusted_proxies": _m("web", "Trusted proxies"),
    "web.session_days": _m("web", "Stay signed in for (days)", "Applies to new sign-ins."),
    "web.sync_every_minutes": _m("web", "Sync every (minutes)", "0 turns the scheduler off."),
    "web.process_new": _m("web", "Queue new photos for review after each sync", "Costs vision calls."),
    "web.api_docs": _m("web", "API docs at /api/docs", "Behind the login. Takes effect after a restart."),
    # standard tags
    "standard_tags.max_tags_per_photo": _m("tags", "Most tags per photo", "Flickr allows 75."),
    "standard_tags.date.enabled": _m("tags", "Date tags"),
    "standard_tags.date.formats": _m("tags", "Date tag formats", "{year} {month:02d} {day:02d} {season}"),
    "standard_tags.date.skip_if_dated": _m("tags", "Skip photos that already have a year tag"),
    "standard_tags.place.enabled": _m("tags", "Place tags"),
    "standard_tags.place.country": _m("tags", "Place: country"),
    "standard_tags.place.regions": _m("tags", "Place: region (americas, westerneurope …)"),
    "standard_tags.place.state": _m("tags", "Place: state / province"),
    "standard_tags.place.state_abbr": _m("tags", "Place: US state abbreviation"),
    "standard_tags.place.city": _m("tags", "Place: town"),
    "standard_tags.place.split_words": _m("tags", "Place: also each word (united, states)"),
    "standard_tags.place.extra": _m("tags", "Place: extra tags", "Country code, country or state → tags."),
    "standard_tags.place.region_overrides": _m("tags", "Place: region overrides", "Country code → region tags."),
    "standard_tags.camera.enabled": _m("tags", "Camera tags", "Needs each photo's EXIF (fetched once)."),
    "standard_tags.camera.name_words": _m("tags", "Camera: words of the camera name"),
    "standard_tags.camera.rules": _m("tags", "Camera: extra tags", "When the camera name matches (regex)."),
    "standard_tags.camera.albums": _m("tags", "Camera albums", "Camera name (regex) → album."),
    "standard_tags.album_tags.enabled": _m("tags", "Album tags", "Every photo in an album gets its tag."),
    "standard_tags.album_tags.from_categories": _m("tags", "Album tags from categories"),
    "standard_tags.album_tags.albums": _m("tags", "Album tags", "Album title → tags."),
    "standard_tags.coords.enabled": _m("tags", "Coordinate tags", "lat300 lon950"),
    "standard_tags.coords.formats": _m("tags", "Coordinate formats", "{lat} {lon}"),
    "standard_tags.coords.min_accuracy": _m("tags", "Coordinates: geotag precision needed"),
    "standard_tags.owner.enabled": _m("tags", "Your name and handles"),
    "standard_tags.owner.tags": _m("tags", "Name and handle tags"),
    "standard_tags.filler.enabled": _m("tags", "Filler words"),
    "standard_tags.filler.tags": _m("tags", "Filler tags"),
}
