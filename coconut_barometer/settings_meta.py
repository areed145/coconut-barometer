"""Labels, help text, sections and headings for the admin page, by setting path. The page
shows a section's settings in the order they're listed here, starting a new heading whenever
`part` changes (Photos: Sync, Writing, Subjects ...) and a sub-heading whenever `group` changes.
A setting missing here still appears (under its own path), and tests check every setting has an entry."""
from __future__ import annotations

SECTIONS = [
    ("connections", "Connections", "Accounts and API keys for the services the app talks to. Keys are stored "
                                   "encrypted and never shown again once saved."),
    ("photos", "Photos", "Every photo gets a proposed title, description, tags and albums, and you accept or "
                         "decline each in the reviewer. Nothing changes on Flickr until you approve it."),
    ("weather", "Weather", "WeatherFlow Tempest stations. History comes from their REST API, and the live feed "
                           "from their WebSocket. Every station on the token's account is found by itself."),
    ("aprs", "APRS", "Callsigns to follow on the APRS page, and the igate whose packets the Igate page counts. Both are "
                     "heard live from APRS-IS (read-only, nothing is transmitted) and kept, so tracks and counts start "
                     "from when the app began listening."),
    ("display", "Display", "How the app looks: theme, units, colors and maps."),
    ("backup", "Backups", "Compressed copies of the database, safe while the app runs. API keys stay encrypted "
                          "in them; keep the encryption key somewhere else."),
    ("web", "Server", "The settings in grey protect the server itself and stay in config.yaml or the environment."),
]


def _m(section, label, help="", group="", options=None, part=""):
    out = {"section": section, "label": label, "help": help, "group": group, "part": part}
    if options:
        out["options"] = options
    return out


def _p(part):
    """A Photos setting under the heading `part`."""
    return lambda label, help="", group="", options=None: _m("photos", label, help, group, options, part)


_sync, _review, _tags, _writing, _subjects, _location, _cameras, _albums, _groups = (
    _p("Sync"), _p("Review"), _p("Tags"), _p("Writing"), _p("Subjects"), _p("Location"), _p("Cameras & dates"),
    _p("Albums"), _p("Groups"))

META = {
    # ---- Connections
    "flickr.api_key": _m("connections", "API key", "From flickr.com/services/apps/create.", group="Flickr"),
    "flickr.api_secret": _m("connections", "API secret", group="Flickr"),
    "vision.provider": _m("connections", "Provider", "claude / anthropic, openai, or ollama (local).", group="Vision model"),
    "vision.model": _m("connections", "Model", "e.g. claude-haiku-5-5 (cheapest) or claude-sonnet-5-5.",
                       group="Vision model"),
    "vision.api_key": _m("connections", "API key", "Anthropic (or OpenAI) key. Not needed for Ollama.",
                         group="Vision model"),
    "vision.base_url": _m("connections", "Server URL", "Only for Ollama or another OpenAI-compatible server.",
                          group="Vision model"),
    "weatherflow.token": _m("connections", "Token", "A personal access token, from tempestwx.com → Settings → "
                            "Data Authorizations.", group="WeatherFlow"),
    "map.carto_key": _m("connections", "CARTO key", "Free from carto.com/basemaps/apikey. Without it the map tiles "
                        "say \"API KEY REQUIRED\". It goes in the address of every map tile, so anyone who can "
                        "open a map can see it (that's how CARTO keys work): restrict it to your sites in "
                        "CARTO's dashboard. Reload the map after changing it.", group="Maps"),

    # ---- Photos: Sync
    "web.sync_every_minutes": _sync("Sync every (minutes)", "Whatever is due below. 0 turns syncing off.",
                                    group="How often"),
    "schedule.full_every_days": _sync("Full sync every (days)", "Everything, ~180 calls for 10k photos.",
                                      group="How often"),
    "schedule.photo_list_every_hours": _sync("Re-read every photo every (hours)",
                                             "View counts and the daily snapshot, ~22 calls.", group="How often"),
    "schedule.stats": _sync("Per-day stats", "Needs Flickr Pro.", group="Stats"),
    "schedule.backfill_days_per_run": _sync("Past days fetched per sync", "Until Flickr has none (~a year).",
                                            group="Stats"),

    # ---- Photos: Review
    "review.when_queued": _review("When a photo is sent to review",
                                  "New photos from a sync, and photos sent from the gallery. Either way nothing "
                                  "changes on Flickr until you apply it.",
                                  options={"process": "Process it right away (costs vision calls)",
                                           "queue": "Wait; process it when I open it"}),
    "review.missing_location": _review("Review photos without a location",
                                       "Each daily re-read of every photo sends the ones with no geotag that "
                                       "aren't in review yet, with a guessed location: from photos taken around "
                                       "the same time, place names in the title and tags, and the vision model. "
                                       "With no clue, you pick the place on the map. Off: locations are never "
                                       "proposed."),

    # ---- Photos: Tags
    "tags.always_add": _tags("Always add", "Put on every photo's proposed tags: your name, your site, anything "
                             "you want on all of them. A photo's tags are proposed as one list that replaces "
                             "them, so a tag of yours that nothing proposes would otherwise be taken off."),
    "tags.always_remove": _tags("Always remove", "Never on a photo: taken off its tags when it's proposed, even if "
                                "the model or a rule suggests it. * and ? are wildcards (www* matches "
                                "wwwadammreedercom). Applies to proposals made after the change: re-plan to update "
                                "the ones in review."),

    # ---- Photos: Writing
    "vision.title.max_words": _writing("Length (words)", group="Titles"),
    "vision.title.instructions": _writing("Brief", "{max_words} is filled in. Empty uses the built-in brief. "
                                          "The model is always told never to guess a location or name a person.",
                                          group="Titles"),
    "vision.describe.max_words": _writing("Length (words)", group="Descriptions"),
    "vision.describe.instructions": _writing("Brief", "{max_words} is filled in. Empty uses the built-in brief. "
                                             "The model is always told never to guess a location or name a person.",
                                             group="Descriptions"),
    "vision.use_photo_text": _writing("Show the model your title, description and tags", group="What the model sees"),
    "vision.use_location": _writing("Show the model the geotag's town and country", "Helps it name landmarks.",
                                    group="What the model sees"),

    # ---- Photos: Subjects
    "vision.categories": _subjects("Categories", "Each category's album and tags, for the photos the model "
                                   "scores it on. The description is what the model reads."),
    "vision.min_confidence": _subjects("Minimum confidence",
                                       "0-1. Categories scored below this aren't proposed. No re-run needed to change it."),
    "vision.extra_tags.max": _subjects("Descriptive tags per photo",
                                       "What the model sees: landmarks, aircraft types, species, things in view."),

    # ---- Photos: Location
    "geo.min_accuracy.country": _location("Countries", "Flickr's geotag accuracy, 1 world … 16 street. Less precise "
                                          "geotags get no album or tag at that level.", group="Geotag precision"),
    "geo.min_accuracy.state": _location("States", group="Geotag precision"),
    "geo.min_accuracy.city": _location("Cities and towns", group="Geotag precision"),
    "geo.countries.aliases": _location("Country albums by code",
                                       "Country code → your album title, e.g. TH → \"Thailand, Vietnam, Laos\". "
                                       "Albums named after the country are found without one.", group="Countries"),
    "geo.us_states.enabled": _location("US state albums", group="US states"),
    "geo.us_states.title": _location("Album title", "{state} is filled in, e.g. \"{state}, USA\".", group="US states"),
    "geo.cities": _location("City albums", "Photos within radius_km of a point go in the album. With place tags on, "
                            "they're also tagged with the city (its tag, or the album title).", group="Cities"),
    "standard_tags.place.enabled": _location("Place tags", "Country, region (americas, westerneurope …), state, "
                                             "US state abbreviation and town, each only as precise as the geotag.",
                                             group="Place tags"),

    # ---- Photos: Cameras & dates
    "standard_tags.camera.albums": _cameras("Camera albums", "Camera name (regex) → album.", group="Cameras"),
    "standard_tags.camera.enabled": _cameras("Camera tags", "From each photo's EXIF (fetched once).", group="Cameras"),
    "standard_tags.camera.name_words": _cameras("Words of the camera name", group="Cameras"),
    "standard_tags.date.enabled": _cameras("Date tags", group="Dates"),
    "standard_tags.date.formats": _cameras("Formats", "{year} {month:02d} {day:02d} {season}", group="Dates"),

    # ---- Photos: Albums
    "albums.create_missing": _albums("Create missing albums",
                                     "Off: only add photos to albums you already have. On: also propose new albums."),

    # ---- Photos: Groups
    "groups.suggest": _groups("Suggest groups", "Propose adding each public photo to the groups it fits best "
                              "among those you're in, judged by the photos you've already put in each (a group "
                              "needs 3 of yours to learn from)."),
    "groups.max_per_photo": _groups("Groups per photo", "At most this many suggested for a photo."),
    "groups.min_match": _groups("Minimum match", "How well a photo must match a group's photos: about 3 suggests "
                                "2-3 groups for most photos, 5 about one for the closer matches. Group rules and "
                                "posting limits still apply when it's added."),

    # ---- Weather
    "weatherflow.backfill_days": _m("weather", "History to fetch (days)", "How far back the REST sync reaches, one "
                                    "request per day per station. Raise it later to fill in older days; it only "
                                    "fetches what's missing."),

    # ---- APRS
    "aprs.callsigns": _m("aprs", "Callsigns", "With the SSID if it has one, e.g. KJ4TFD-10. A change reconnects "
                         "within a few seconds."),
    "aprs.igate": _m("aprs", "Igate", "The igate whose gated packets the Igate page maps and counts, e.g. WA5DTR-15. "
                     "Empty: none. A change reconnects; packets already counted stay under the igate that gated them."),
    "aprs.igate_location": _m("aprs", "Igate location", "Where it is, for distances: \"lat, lon\" (30.27, -97.74) or "
                              "a grid locator (EM10dg). Empty: wherever its own position beacons put it."),
    "aprs.cwop": _m("aprs", "Stations that report", "The callsign-SSID each station reports as, e.g. KK6GPV-13 (-13 is "
                    "the usual SSID for weather; each station needs its own). It sends its current readings and its exact "
                    "position to CWOP (shown on aprs.fi and CWOP's maps; NOAA uses them). Blank: that station doesn't.",
                    group="Weather reports (CWOP)"),
    "aprs.passcode": _m("aprs", "APRS-IS passcode", "For your callsign (the same for every SSID): the one sending "
                        "connection needs it. Tracking stays receive-only.", group="Weather reports (CWOP)"),
    "aprs.cwop_minutes": _m("aprs", "Report every (minutes)", "CWOP asks for every 5 to 10 minutes.",
                            group="Weather reports (CWOP)"),

    # ---- Display
    "display.units": _m("display", "Units", "How the Weather and APRS pages show values. Everything is stored metric, "
                        "so changing this changes nothing saved.", group="Units",
                        options={"metric": "Metric (°C, km/h or m/s, hPa, mm, m)",
                                 "imperial": "Imperial (°F, mph, inHg, in, ft)"}),
    "theme.color_low": _m("display", "Low", "e.g. a cluster of a few photos, or a short bar.", group="Colors"),
    "theme.color_high": _m("display", "High", "e.g. the biggest cluster, or the longest bar.", group="Colors"),
    "map.expand_at": _m("display", "Show thumbnails from (photos)", "On the photo map, clicking a cluster zooms in one "
                        "level; once it holds this many photos or fewer, it opens into thumbnails instead.", group="Map"),

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
