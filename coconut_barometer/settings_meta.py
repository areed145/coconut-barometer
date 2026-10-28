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
    ("alerts", "Alerts", "Notifications on your phone or computer when something worth knowing happens. Turn them on for "
                         "each device below; the thresholds apply to all of them. 0 turns an alert off."),
    ("display", "Display", "How the app looks: theme, units, colors and maps."),
    ("backup", "Backups", "Compressed copies of the database, safe while the app runs. API keys stay encrypted "
                          "in them; keep the encryption key somewhere else."),
    ("visitors", "Visitors", "Who opens this site, shown on the Visitors page. No cookies are set for "
                             "visitors and no IP address is stored: each is counted by a fingerprint that changes "
                             "every day, with the city, region and country their address gives. Your own visits "
                             "aren't counted on any device you've signed in on."),
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


_sync, _review, _tags, _writing, _subjects, _location, _cameras, _albums, _groups, _trips = (
    _p("Sync"), _p("Review"), _p("Tags"), _p("Writing"), _p("Subjects"), _p("Location"), _p("Cameras & dates"),
    _p("Albums"), _p("Groups"), _p("Trips"))

META = {
    # ---- Connections
    "flickr.api_key": _m("connections", "API key", "From flickr.com/services/apps/create.", group="Flickr"),
    "flickr.api_secret": _m("connections", "API secret", group="Flickr"),
    "vision.provider": _m("connections", "Provider", "claude / anthropic, openai, or ollama (local).", group="Vision model"),
    "vision.model": _m("connections", "Model", "The models the provider offers that take images. Haiku is the "
                       "cheapest. A model the provider says it doesn't have isn't saved.", group="Vision model"),
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
    "github.token": _m("connections", "Token", "Set it and the app opens an issue for each unexpected error (one "
                       "per kind; a repeat adds a comment). A fine-grained token from github.com → Settings → "
                       "Developer settings, for the one repository, with Issues: read and write. Empty: off.",
                       group="Error reports"),
    "github.repo": _m("connections", "Repository", "owner/name.", group="Error reports"),

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

    # ---- Photos: Trips
    "trips.gap_hours": _trips("New trip after (hours)", "Photos → Trips groups photos taken close together. A gap this "
                              "long without a photo starts a new trip."),
    "trips.jump_km": _trips("... or a move of (km)", "Or a move this far across most of a day without photos (the flight "
                            "home, not the next stop on a road trip). A trip also ends after two weeks."),
    "trips.min_photos": _trips("Fewest photos", "Shorter runs aren't shown as trips."),

    # ---- Weather
    "weatherflow.backfill_days": _m("weather", "History to fetch (days)", "How far back the REST sync reaches, one "
                                    "request per day per station. Raise it later to fill in older days; it only "
                                    "fetches what's missing."),
    "qc.enabled": _m("weather", "Check the stations", "Compare each station with independent data around it "
                     "(NOAA's RTMA analysis, radar, satellite lightning, NWS alerts) every few minutes, and run the "
                     "checks on Weather → QC. Reading only: nothing is sent.", group="Station QC"),
    "qc.backfill_days": _m("weather", "Fill in the past (days)", "On first run, the RTMA analysis for this many days "
                           "back is read from NOAA's archive (hourly), so the checks have something to go on at once.",
                           group="Station QC"),
    "qc.keep_days": _m("weather", "Keep samples (days)", "The reference samples and check results are kept this "
                       "long.", group="Station QC"),
    "qc.window_days": _m("weather", "Check over (days)", "How far back each check looks.", group="Station QC"),
    "qc.min_pairs": _m("weather", "Readings needed", "A check with fewer readings to compare than this says \"not "
                       "enough data\" rather than pass or fail. RTMA is every 15 minutes: 96 is a day.", group="Station QC"),
    "qc.temp_bias_warn": _m("weather", "Temperature: warn at (°C)", "The station's average difference from RTMA, "
                            "after allowing for the height difference between the station and the analysis.",
                            group="Station QC"),
    "qc.temp_bias_fail": _m("weather", "Temperature: fail at (°C)", group="Station QC"),
    "qc.dew_bias_warn": _m("weather", "Dewpoint: warn at (°C)", "The station's average dewpoint (from its humidity) "
                           "against RTMA's.", group="Station QC"),
    "qc.dew_bias_fail": _m("weather", "Dewpoint: fail at (°C)", group="Station QC"),
    "qc.sun_warn": _m("weather", "Sun on the sensor: warn at (°C)", "How much warmer than RTMA the station reads in strong "
                      "sun with little wind than it does at night. Sunlight heating the sensor (a radiation shield that "
                      "needs cleaning or a better spot) shows here.", group="Station QC"),
    "qc.sun_fail": _m("weather", "Sun on the sensor: fail at (°C)", group="Station QC"),
    "qc.drift_warn": _m("weather", "Drift: warn at (°C)", "How far the last week's temperature difference from RTMA has "
                        "moved from the rest of the window's.", group="Station QC"),
    "qc.drift_fail": _m("weather", "Drift: fail at (°C)", group="Station QC"),
    "qc.coverage_warn": _m("weather", "Reporting: warn under (%)", "The share of minutes the station reported over "
                           "the window, from its first reading in it.", group="Station QC"),
    "qc.coverage_fail": _m("weather", "Reporting: fail under (%)", group="Station QC"),
    "qc.solar_warn": _m("weather", "Solar: warn under (share of clear sky)", "On clear days, the station's solar peak "
                        "over the clear-sky model's. A dirty or shaded sensor reads low.", group="Station QC"),
    "qc.solar_fail": _m("weather", "Solar: fail under (share of clear sky)", group="Station QC"),
    "qc.wind_dir_warn": _m("weather", "Wind direction: warn at (°)", "The station's average wind direction against "
                           "RTMA's, in wind of 3 m/s or more. The same offset from every direction is a station not "
                           "aligned to north.", group="Station QC"),
    "qc.wind_dir_fail": _m("weather", "Wind direction: fail at (°)", group="Station QC"),
    "qc.wind_sector_warn": _m("weather", "Wind from one side: warn under (share)", "The station's speed against RTMA's "
                              "for wind from each direction, over its speed against RTMA overall. Low from one side is "
                              "something blocking the wind there.", group="Station QC"),
    "qc.wind_sector_fail": _m("weather", "Wind from one side: fail under (share)", group="Station QC"),
    "qc.pressure_warn": _m("weather", "Pressure: warn at (hPa)", "The station's average pressure against RTMA's, moved "
                           "to the station's elevation. A steady offset is usually the elevation setting (1 hPa is "
                           "about 8 m).", group="Station QC"),
    "qc.pressure_fail": _m("weather", "Pressure: fail at (hPa)", group="Station QC"),
    "qc.pressure_drift_warn": _m("weather", "Pressure drift: warn at (hPa)", "How far the last week's pressure "
                                 "difference from RTMA has moved from the rest of the window's.", group="Station QC"),
    "qc.pressure_drift_fail": _m("weather", "Pressure drift: fail at (hPa)", group="Station QC"),
    "qc.lightning_false_warn": _m("weather", "Lightning, nothing there: warn at (share)", "The share of the station's "
                                  "strikes with no lightning from GOES-East within 50 km and 10 minutes: interference "
                                  "(a motor, a fence charger, a vibration).", group="Station QC"),
    "qc.lightning_false_fail": _m("weather", "Lightning, nothing there: fail at (share)", group="Station QC"),
    "qc.lightning_miss_warn": _m("weather", "Lightning missed: warn at (share)", "The share of the 15-minute periods "
                                 "GOES-East saw lightning within 20 km in which the station heard none. GLM sees "
                                 "in-cloud flashes too, so some are expected.", group="Station QC"),
    "qc.lightning_miss_fail": _m("weather", "Lightning missed: fail at (share)", group="Station QC"),
    "qc.fronts_warn": _m("weather", "Front passages: warn under (share)", "The share of fronts crossing the station "
                         "that brought the wind shift, pressure and temperature change expected. Analyzed fronts are "
                         "approximate, so this only warns.", group="Station QC"),

    # ---- APRS
    "aprs.callsigns": _m("aprs", "Callsigns", "With the SSID if it has one, e.g. KJ4TFD-10. A change reconnects "
                         "within a few seconds."),
    "aprs.igate": _m("aprs", "Igate", "The igate whose gated packets the Igate page maps and counts, e.g. WA5DTR-15. "
                     "Empty: none. A change reconnects; packets already counted stay under the igate that gated them."),
    "aprs.igate_location": _m("aprs", "Igate location", "Where it is, for distances: \"lat, lon\" (30.27, -97.74) or "
                              "a grid locator (EM10dg). Empty: wherever its own position beacons put it."),
    "aprs.trip_stop_minutes": _m("aprs", "A stop is at least (minutes)", "Staying within 100 m this long ends a trip on "
                                 "the APRS page's Trips. Shorter (a traffic light) is part of the trip.", group="Trips"),
    "aprs.trip_gap_minutes": _m("aprs", "A gap is at least (minutes)", "Nothing heard this long while it moved: the trip "
                                "ends there and the next begins, shown as a gap rather than a stop.", group="Trips"),
    "aprs.places": _m("aprs", "Places", "Named circles for the arrive and leave alerts (Settings → Alerts) and for naming "
                      "where a trip stopped: Home, Work, Cabin. Click the map to put the selected place there, or take "
                      "a tracked callsign's position. Only you see them.", group="Places"),
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
    "alerts.lightning_km": _m("alerts", "Lightning within (km)", "A strike this close to any station. The Tempest "
                              "senses up to about 40 km.", group="Weather"),
    "alerts.gust_kmh": _m("alerts", "Gust over (km/h)", "60 km/h is about 37 mph.", group="Weather"),
    "alerts.pressure_drop_hpa": _m("alerts", "Pressure falling (hPa in 3 hours)", "Down this much: a storm sign. "
                                   "2 hPa is about 0.06 inHg.", group="Weather"),
    "alerts.callsign_quiet_hours": _m("alerts", "Callsign heard after quiet (hours)", "A callsign from Settings → APRS "
                                      "heard again after this long without a packet.", group="APRS"),
    "alerts.places": _m("alerts", "Arrive and leave", "A notification when a callsign arrives at or leaves a place "
                        "(Settings → APRS → Places). on: arrive, leave or both; quiet: hours to say nothing, like "
                        "22:00-07:00 (the weather station's time). Leaving counts once it's 1.2 × the radius away, so "
                        "the edge doesn't flap.", group="APRS"),
    "alerts.place_dwell_minutes": _m("alerts", "Arrived or left after (minutes)", "Two positions in a row decide it, or "
                                     "one that has stood this long with nothing since (beacons are sparse when parked).",
                                     group="APRS"),
    "alerts.igate_quiet_minutes": _m("alerts", "Igate quiet for (minutes)", "The igate gating nothing this long when "
                                     "it usually gates 10 packets an hour or more, gating well under its usual for 3 "
                                     "hours, or its own beacon stopping; and a notice when it's back. Not while the "
                                     "app's own APRS-IS connection is down. 0 turns it off.", group="APRS"),
    "alerts.igate_record_days": _m("alerts", "Igate's farthest in (days)", "A station heard direct farther away than any "
                                   "in this many days: 30 for a monthly record, 3650 for all time. Unbelievable "
                                   "positions are left out, as in the long-distance log. 0 turns it off.", group="APRS"),
    "alerts.igate_altitude_km": _m("alerts", "Igate heard a station above (km)", "A packet the igate gated from this "
                                   "high: usually a pico balloon or a high-altitude flight. 5 km is about 16,000 ft. "
                                   "0 turns it off.", group="APRS"),
    "alerts.storm_km": _m("alerts", "Storm passing within (km)", "A storm cell the NWS radars are tracking whose path "
                          "comes this close to a station. 0 turns storm alerts off.", group="Radar"),
    "alerts.storm_minutes": _m("alerts", "Storm warning ahead (minutes)", "How far ahead to look along its path.", group="Radar"),
    "alerts.storm_dbz": _m("alerts", "Storm at least (dBZ)", "50 is a strong thunderstorm; 60 often means hail.",
                           group="Radar"),
    "alerts.rain_minutes": _m("alerts", "Rain due within (minutes)", "Rain on radar closing in on a station and due "
                              "within this long. 0 turns rain alerts off.", group="Radar"),
    "alerts.storm_max_kmh": _m("alerts", "Fastest believable storm (km/h)", "Storm cells and rain reported moving "
                               "faster than this are taken for tracking errors and never projected toward a station. "
                               "120 km/h is about 75 mph.", group="Radar"),
    "alerts.radar_per_hour": _m("alerts", "Radar alerts per hour (per station)", "Storm and rain alerts together, "
                                "at most this many an hour for each station.", group="Radar"),
    "alerts.cooldown_minutes": _m("alerts", "Repeat at most every (minutes)", "Per alert and station (or callsign), so "
                                  "one storm isn't fifty notifications.", group="All alerts"),
    "theme.color_low": _m("display", "Low", "e.g. a cluster of a few photos, or a short bar.", group="Colors"),
    "theme.color_mid": _m("display", "Medium", "Halfway along: the gradient runs low → medium → high.", group="Colors"),
    "theme.color_high": _m("display", "High", "e.g. the biggest cluster, or the longest bar.", group="Colors"),
    "theme.radar_colors": _m("display", "Radar colors", "The Weather map's radar, from light rain to hail: each "
                             "stop is a dBZ and its color, blended in between.", group="Radar"),
    "theme.lightning_colors": _m("display", "Lightning colors", "The Weather map's lightning, by how long ago: each "
                                 "stop is minutes before the time shown (0 is new, 15 the oldest shown) and its color, "
                                 "blended in between.", group="Radar"),
    "map.expand_at": _m("display", "Show thumbnails from (photos)", "On the photo map, clicking a cluster zooms in one "
                        "level; once it holds this many photos or fewer, it opens into thumbnails instead.", group="Map"),

    # ---- Backups
    "backup.every_hours": _m("backup", "Back up every (hours)", "0 turns automatic backups off."),
    "backup.keep": _m("backup", "Backups kept", "The newest are kept; older ones deleted."),
    "backup.dir": _m("backup", "Backup folder", "Empty: backups/ next to the database (on the disk)."),

    # ---- Visitors
    "visitors.enabled": _m("visitors", "Count visitors", "Off: nothing new is recorded; what's kept stays."),
    "visitors.keep_days": _m("visitors", "Keep page views for (days)", "Older days keep only their visitor and page "
                             "view totals."),
    "visitors.honor_gpc": _m("visitors", "Respect Global Privacy Control", "Browsers that ask not to be tracked "
                             "(Sec-GPC or Do Not Track) aren't counted."),

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
