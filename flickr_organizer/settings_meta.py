"""Labels, help text, sections and sub-headings for the admin page, by setting path. The
page shows a section's settings in the order they're listed here, starting a new
sub-heading whenever `group` changes. A setting missing here still appears (under its
own path), and tests check every setting has an entry."""
from __future__ import annotations

PROPOSALS = ("Every photo gets a proposed title, description, tags and albums, and you accept or decline each "
             "in the reviewer. ")

SECTIONS = [
    ("keys", "API keys", "Stored encrypted. Changing one asks for your password."),
    ("sync", "Sync", "How often the app syncs with Flickr, and what happens to the photos that arrive."),
    ("titles", "Titles", PROPOSALS + "This is what the model is asked for. Changing the brief or length writes "
                         "the title again (one model call per photo it applies to)."),
    ("descriptions", "Descriptions", PROPOSALS + "This is what the model is asked for. Changing the brief or length "
                                     "writes the description again (one model call per photo it applies to)."),
    ("tags", "Tags", PROPOSALS + "Tags come from the model, the location and your own conventions."),
    ("albums", "Albums", PROPOSALS + "Albums come from what's in the photo, where it was taken, and the camera."),
    ("map", "Map", "The Map page: photos clustered by where they were taken."),
    ("vision", "Vision model", "The model that looks at each photo and writes the proposals."),
    ("backup", "Backups", "Compressed copies of the database, safe while the app runs. API keys stay encrypted "
                          "in them; keep the encryption key somewhere else."),
    ("web", "Server", "The settings in grey protect the server itself and stay in config.yaml or the environment."),
]


def _m(section, label, help="", group="", options=None):
    out = {"section": section, "label": label, "help": help, "group": group}
    if options:
        out["options"] = options
    return out


META = {
    # ---- API keys
    "flickr.api_key": _m("keys", "Flickr API key", "From flickr.com/services/apps/create."),
    "flickr.api_secret": _m("keys", "Flickr API secret"),
    "vision.api_key": _m("keys", "Vision API key", "Anthropic (or OpenAI) key. Not needed for Ollama."),

    # ---- Sync
    "schedule.new_photos": _m("sync", "New photos", "Either way they're under Review → To review; nothing changes "
                              "on Flickr until you approve it.", group="After a sync",
                              options={"process": "Process them right away (costs vision calls)",
                                       "queue": "Queue them; each is processed when you open it"}),
    "web.sync_every_minutes": _m("sync", "Sync every (minutes)", "Whatever is due below. 0 turns syncing off.",
                                 group="How often"),
    "schedule.full_every_days": _m("sync", "Full sync every (days)", "Everything, ~180 calls for 10k photos.",
                                   group="How often"),
    "schedule.photo_list_every_hours": _m("sync", "Re-read every photo every (hours)",
                                          "View counts and the daily snapshot, ~22 calls.", group="How often"),
    "schedule.stats": _m("sync", "Per-day stats", "Needs Flickr Pro.", group="Stats"),
    "schedule.backfill_days_per_run": _m("sync", "Past days fetched per sync", "Until Flickr has none (~a year).",
                                         group="Stats"),
    "flickr.calls_per_second": _m("sync", "Flickr calls per second",
                                  "Shared by everything using the key. 1 = Flickr's 3600/hour limit.", group="Limits"),

    # ---- Titles
    "vision.title.max_words": _m("titles", "Length (words)"),
    "vision.title.instructions": _m("titles", "Brief", "{max_words} is filled in. Empty uses the built-in brief. "
                                    "The model is always told never to guess a location or name a person."),

    # ---- Descriptions
    "vision.describe.max_words": _m("descriptions", "Length (words)"),
    "vision.describe.instructions": _m("descriptions", "Brief",
                                       "{max_words} is filled in. Empty uses the built-in brief. The model is always "
                                       "told never to guess a location or name a person."),

    # ---- Tags
    "vision.extra_tags.max": _m("tags", "Descriptive tags per photo",
                                "What the model sees: landmarks, aircraft types, species, things in view.",
                                group="From the model"),
    "vision.extra_tags.prefix": _m("tags", "Prefix for them", "e.g. ai: to tell them apart. Usually empty.",
                                   group="From the model"),
    "geo.add_tags": _m("tags", "Tag the country, state and city", "e.g. japan, texas, austin, from the location "
                       "albums below.", group="From the location"),
    "standard_tags.max_tags_per_photo": _m("tags", "Most tags per photo", "Flickr allows 75. When a photo would "
                                           "go over, the kinds lower on this page drop off first.", group="Limits"),
    "standard_tags.date.enabled": _m("tags", "Date tags", group="Your conventions: dates"),
    "standard_tags.date.formats": _m("tags", "Formats", "{year} {month:02d} {day:02d} {season}",
                                     group="Your conventions: dates"),
    "standard_tags.date.skip_if_dated": _m("tags", "Skip photos that already have a year tag",
                                           group="Your conventions: dates"),
    "standard_tags.place.enabled": _m("tags", "Place tags", group="Your conventions: places"),
    "standard_tags.place.country": _m("tags", "Country", group="Your conventions: places"),
    "standard_tags.place.regions": _m("tags", "Region (americas, westerneurope …)", group="Your conventions: places"),
    "standard_tags.place.state": _m("tags", "State / province", group="Your conventions: places"),
    "standard_tags.place.state_abbr": _m("tags", "US state abbreviation", group="Your conventions: places"),
    "standard_tags.place.city": _m("tags", "Town", group="Your conventions: places"),
    "standard_tags.place.split_words": _m("tags", "Also each word (united, states)", group="Your conventions: places"),
    "standard_tags.place.extra": _m("tags", "Extra tags", "Country code, country or state → tags.",
                                    group="Your conventions: places"),
    "standard_tags.place.region_overrides": _m("tags", "Region overrides", "Country code → region tags.",
                                               group="Your conventions: places"),
    "standard_tags.camera.enabled": _m("tags", "Camera tags", "From each photo's EXIF (fetched once).",
                                       group="Your conventions: cameras"),
    "standard_tags.camera.name_words": _m("tags", "Words of the camera name", group="Your conventions: cameras"),
    "standard_tags.camera.rules": _m("tags", "Extra tags", "When the camera name matches (regex).",
                                     group="Your conventions: cameras"),
    "standard_tags.album_tags.enabled": _m("tags", "Album tags", "Every photo in an album gets its tags.",
                                           group="Your conventions: albums"),
    "standard_tags.album_tags.from_categories": _m("tags", "Use the categories' tags",
                                                   group="Your conventions: albums"),
    "standard_tags.album_tags.albums": _m("tags", "More album tags", "Album title → tags.",
                                          group="Your conventions: albums"),
    "standard_tags.coords.enabled": _m("tags", "Coordinate tags", "e.g. lat300 lon950",
                                       group="Your conventions: coordinates"),
    "standard_tags.coords.formats": _m("tags", "Formats", "{lat} {lon}", group="Your conventions: coordinates"),
    "standard_tags.coords.min_accuracy": _m("tags", "Geotag precision needed", "1 world … 16 street.",
                                            group="Your conventions: coordinates"),
    "standard_tags.owner.enabled": _m("tags", "Your name and handles", group="Your conventions: you"),
    "standard_tags.owner.tags": _m("tags", "Name and handle tags", group="Your conventions: you"),
    "standard_tags.filler.enabled": _m("tags", "Filler words", group="Your conventions: you"),
    "standard_tags.filler.tags": _m("tags", "Filler tags", group="Your conventions: you"),

    # ---- Albums
    "vision.categories": _m("albums", "Categories", "Each category's album and tags, for the photos the model "
                            "scores it on. The description is what the model reads.", group="By subject"),
    "vision.min_confidence": _m("albums", "Minimum confidence",
                                "0-1. Categories scored below this aren't proposed. No re-run needed to change it.",
                                group="By subject"),
    "geo.cities": _m("albums", "City albums", "Photos within radius_km of a point go in the album.",
                     group="By location"),
    "geo.countries.auto_match": _m("albums", "Match country names to albums automatically", group="By location"),
    "geo.countries.aliases": _m("albums", "Country albums by code",
                                "Country code → your album title, e.g. TH → \"Thailand, Vietnam, Laos\".",
                                group="By location"),
    "geo.us_states.enabled": _m("albums", "US state albums", group="By location"),
    "geo.us_states.title": _m("albums", "US state album title", "{state} is filled in, e.g. \"{state}, USA\".",
                              group="By location"),
    "geo.min_accuracy.country": _m("albums", "Geotag precision for countries", "1 world … 16 street.",
                                   group="By location"),
    "geo.min_accuracy.state": _m("albums", "Geotag precision for states", group="By location"),
    "geo.min_accuracy.city": _m("albums", "Geotag precision for cities and towns", group="By location"),
    "standard_tags.camera.albums": _m("albums", "Camera albums", "Camera name (regex) → album.", group="By camera"),
    "albums.create_missing": _m("albums", "Create missing albums",
                                "Off: only add photos to albums you already have. On: also propose new albums.",
                                group="New albums"),

    # ---- Map
    "map.carto_key": _m("map", "CARTO API key", "Free from carto.com/basemaps/apikey. Without it the map tiles "
                        "say \"API KEY REQUIRED\". It goes in the address of every map tile, so anyone who can "
                        "open the map can see it (that's how CARTO keys work): restrict it to your sites in "
                        "CARTO's dashboard. Reload the map after changing it.", group="Map tiles"),
    "map.color_low": _m("map", "Colour for small clusters", group="Clusters"),
    "map.color_high": _m("map", "Colour for the biggest clusters", "In between, the colour and size follow the "
                         "number of photos on a log scale.", group="Clusters"),
    "map.expand_at": _m("map", "Show thumbnails from (photos)", "Clicking a cluster zooms in one level; once it "
                        "holds this many photos or fewer, it opens into thumbnails instead.", group="Clusters"),

    # ---- Vision model
    "vision.provider": _m("vision", "Provider", "claude / anthropic, openai, or ollama (local)."),
    "vision.model": _m("vision", "Model", "e.g. claude-haiku-5-5 (cheapest) or claude-sonnet-5-5."),
    "vision.base_url": _m("vision", "Server URL", "Only for Ollama or another OpenAI-compatible server."),
    "vision.thumbnail": _m("vision", "Image size sent", "url_m (500 px) is plenty."),
    "vision.concurrency": _m("vision", "Photos at once"),
    "vision.use_photo_text": _m("vision", "Show the model your title, description and tags", group="What it's told"),
    "vision.use_location": _m("vision", "Show the model the geotag's town and country", "Helps it name landmarks.",
                              group="What it's told"),

    # ---- Backups
    "backup.every_hours": _m("backup", "Back up every (hours)", "0 turns automatic backups off."),
    "backup.keep": _m("backup", "Backups kept", "The newest are kept; older ones deleted."),
    "backup.dir": _m("backup", "Backup folder", "Empty: backups/ next to the database (on the disk)."),

    # ---- Server
    "web.session_days": _m("web", "Stay signed in for (days)", "Applies to new sign-ins."),
    "web.api_docs": _m("web", "API docs at /api/docs", "Behind the login. Takes effect after a restart."),
    "web.host": _m("web", "Listen address", group="Protected"),
    "web.port": _m("web", "Port", group="Protected"),
    "web.allowed_hosts": _m("web", "Allowed host names", group="Protected"),
    "web.secure_cookies": _m("web", "Secure cookies (HTTPS only)", group="Protected"),
    "web.trusted_proxies": _m("web", "Trusted proxies", group="Protected"),
    "database": _m("web", "Database file", group="Protected"),
}
