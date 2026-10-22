"""Labels, help text, sections and sub-headings for the admin page, by setting path. The
page shows a section's settings in the order they're listed here, starting a new
sub-heading whenever `group` changes. A setting missing here still appears (under its
own path), and tests check every setting has an entry."""
from __future__ import annotations

PROPOSALS = ("Every photo gets a proposed title, description, tags and albums, and you accept or decline each "
             "in the reviewer. ")

SECTIONS = [
    ("photos", "Photos", PROPOSALS + "Titles, descriptions, tags and albums are proposed for each photo, and you accept "
                              "or decline them in the reviewer."),
    ("weather", "Weather", "WeatherFlow Tempest stations. History comes from their REST API, and the live feed "
                           "from their WebSocket. Every station on the token's account is found by itself."),
    ("aprs", "APRS", "Callsigns to follow on the APRS page. Their positions are heard live from APRS-IS (read-only, "
                     "nothing is transmitted) and kept, so the tracks start from when the app began listening."),
    ("keys", "API keys", "Stored encrypted. Changing one asks for your password."),
    ("sync", "Sync", "How often the app syncs with Flickr, and what happens to the photos that arrive."),
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
    "weatherflow.token": _m("keys", "WeatherFlow token", "A personal access token, from tempestwx.com → Settings → Data Authorizations."),

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

    # ---- Weather
    "display.units": _m("weather", "Units", "How the Weather and APRS pages show values. Everything is stored metric, "
                        "so changing this changes nothing saved.", group="Display",
                        options={"metric": "Metric (°C, km/h or m/s, hPa, mm, m)",
                                 "imperial": "Imperial (°F, mph, inHg, in, ft)"}),
    "weatherflow.backfill_days": _m("weather", "History to fetch (days)", "How far back the REST sync reaches, one request "
                                    "per day per station. Raise it later to fill in older days; it only fetches what's missing.",
                                    group="History"),
    "weatherflow.calls_per_second": _m("weather", "REST calls per second", group="History"),

    # ---- APRS
    "aprs.callsigns": _m("aprs", "Callsigns", "With the SSID if it has one, e.g. KJ4TFD-10. A change reconnects "
                         "within a few seconds."),

    # ---- Titles
    "vision.title.max_words": _m("photos", "Length (words)", group="Titles"),
    "vision.title.instructions": _m("photos", "Brief", "{max_words} is filled in. Empty uses the built-in brief. "
                                    "The model is always told never to guess a location or name a person.",
                                    group="Titles"),

    # ---- Descriptions
    "vision.describe.max_words": _m("photos", "Length (words)", group="Descriptions"),
    "vision.describe.instructions": _m("photos", "Brief",
                                       "{max_words} is filled in. Empty uses the built-in brief. The model is always "
                                       "told never to guess a location or name a person.", group="Descriptions"),

    # ---- Tags
    "vision.extra_tags.max": _m("photos", "Descriptive tags per photo",
                                "What the model sees: landmarks, aircraft types, species, things in view.",
                                group="From the model"),
    "vision.extra_tags.prefix": _m("photos", "Prefix for them", "e.g. ai: to tell them apart. Usually empty.",
                                   group="From the model"),
    "geo.add_tags": _m("photos", "Tag the country, state and city", "e.g. japan, texas, austin, from the location "
                       "albums below.", group="From the location"),
    "standard_tags.max_tags_per_photo": _m("photos", "Most tags per photo", "Flickr allows 75. When a photo would "
                                           "go over, the kinds lower on this page drop off first.", group="Limits"),
    "standard_tags.date.enabled": _m("photos", "Date tags", group="Your conventions: dates"),
    "standard_tags.date.formats": _m("photos", "Formats", "{year} {month:02d} {day:02d} {season}",
                                     group="Your conventions: dates"),
    "standard_tags.date.skip_if_dated": _m("photos", "Skip photos that already have a year tag",
                                           group="Your conventions: dates"),
    "standard_tags.place.enabled": _m("photos", "Place tags", group="Your conventions: places"),
    "standard_tags.place.country": _m("photos", "Country", group="Your conventions: places"),
    "standard_tags.place.regions": _m("photos", "Region (americas, westerneurope …)", group="Your conventions: places"),
    "standard_tags.place.state": _m("photos", "State / province", group="Your conventions: places"),
    "standard_tags.place.state_abbr": _m("photos", "US state abbreviation", group="Your conventions: places"),
    "standard_tags.place.city": _m("photos", "Town", group="Your conventions: places"),
    "standard_tags.place.extra": _m("photos", "Extra tags", "Country code, country or state → tags.",
                                    group="Your conventions: places"),
    "standard_tags.place.region_overrides": _m("photos", "Region overrides", "Country code → region tags.",
                                               group="Your conventions: places"),
    "standard_tags.camera.enabled": _m("photos", "Camera tags", "From each photo's EXIF (fetched once).",
                                       group="Your conventions: cameras"),
    "standard_tags.camera.name_words": _m("photos", "Words of the camera name", group="Your conventions: cameras"),
    "standard_tags.camera.rules": _m("photos", "Extra tags", "When the camera name matches (regex).",
                                     group="Your conventions: cameras"),
    "standard_tags.album_tags.enabled": _m("photos", "Album tags", "Every photo in an album gets its tags.",
                                           group="Your conventions: albums"),
    "standard_tags.album_tags.from_categories": _m("photos", "Use the categories' tags",
                                                   group="Your conventions: albums"),
    "standard_tags.album_tags.albums": _m("photos", "More album tags", "Album title → tags.",
                                          group="Your conventions: albums"),

    # ---- Albums
    "vision.categories": _m("photos", "Categories", "Each category's album and tags, for the photos the model "
                            "scores it on. The description is what the model reads.", group="By subject"),
    "vision.min_confidence": _m("photos", "Minimum confidence",
                                "0-1. Categories scored below this aren't proposed. No re-run needed to change it.",
                                group="By subject"),
    "geo.cities": _m("photos", "City albums", "Photos within radius_km of a point go in the album.",
                     group="By location"),
    "geo.countries.auto_match": _m("photos", "Match country names to albums automatically", group="By location"),
    "geo.countries.aliases": _m("photos", "Country albums by code",
                                "Country code → your album title, e.g. TH → \"Thailand, Vietnam, Laos\".",
                                group="By location"),
    "geo.us_states.enabled": _m("photos", "US state albums", group="By location"),
    "geo.us_states.title": _m("photos", "US state album title", "{state} is filled in, e.g. \"{state}, USA\".",
                              group="By location"),
    "geo.min_accuracy.country": _m("photos", "Geotag precision for countries", "1 world … 16 street.",
                                   group="By location"),
    "geo.min_accuracy.state": _m("photos", "Geotag precision for states", group="By location"),
    "geo.min_accuracy.city": _m("photos", "Geotag precision for cities and towns", group="By location"),
    "standard_tags.camera.albums": _m("photos", "Camera albums", "Camera name (regex) → album.", group="By camera"),
    "albums.create_missing": _m("photos", "Create missing albums",
                                "Off: only add photos to albums you already have. On: also propose new albums.",
                                group="New albums"),

    # ---- Map
    "map.carto_key": _m("photos", "CARTO API key", "Free from carto.com/basemaps/apikey. Without it the map tiles "
                        "say \"API KEY REQUIRED\". It goes in the address of every map tile, so anyone who can "
                        "open the map can see it (that's how CARTO keys work): restrict it to your sites in "
                        "CARTO's dashboard. Reload the map after changing it.", group="Map"),
    "map.expand_at": _m("photos", "Show thumbnails from (photos)", "Clicking a cluster zooms in one level; once it "
                        "holds this many photos or fewer, it opens into thumbnails instead.", group="Map"),

    # ---- Colors
    "theme.color_low": _m("photos", "Low", "e.g. a cluster of a few photos, or a short bar.", group="Colors"),
    "theme.color_high": _m("photos", "High", "e.g. the biggest cluster, or the longest bar.", group="Colors"),

    # ---- Vision model
    "vision.provider": _m("photos", "Provider", "claude / anthropic, openai, or ollama (local).", group="Vision model"),
    "vision.model": _m("photos", "Model", "e.g. claude-haiku-5-5 (cheapest) or claude-sonnet-5-5.", group="Vision model"),
    "vision.base_url": _m("photos", "Server URL", "Only for Ollama or another OpenAI-compatible server.", group="Vision model"),
    "vision.thumbnail": _m("photos", "Image size sent", "url_m (500 px) is plenty.", group="Vision model"),
    "vision.concurrency": _m("photos", "Photos at once", group="Vision model"),
    "vision.use_photo_text": _m("photos", "Show the model your title, description and tags", group="What it's told"),
    "vision.use_location": _m("photos", "Show the model the geotag's town and country", "Helps it name landmarks.",
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
