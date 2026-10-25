"""Local mode (#92): the app on a laptop, for development, without acting as the station. The live server (Render)
sends as the station; a copy running locally against the same settings would send too, twice over.

Local mode leaves out everything that sends: APRS-IS (the tracking stream logs in as the station's callsigns, and
CWOP sends its weather reports), push notifications (to the real phones), GitHub error reports (to the real repo),
Flickr (syncing and applying review changes write to the real account), and the scheduled backups. Station QC's
sampling is held too: it only reads, but it's the server's job (a month of RTMA backfilled into a local copy is work
for nothing). What's only read for the pages stays on: WeatherFlow, radar, satellite and the rest of the Weather map,
all that's needed to look at the site.
Each place that would send asks on() and logs what it held back instead; the Status page shows them "off".

`serve` decides once at start (decide()): --local or --production, else COCONUT_LOCAL=1/0, else local when
listening on this machine only (127.0.0.1) and not on Render. A server that listens on 127.0.0.1 behind its own
proxy (Caddy) passes --production."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

LOOPBACK = ("127.0.0.1", "localhost", "::1")
HELD_BACK = ("APRS-IS and CWOP, push notifications, GitHub error reports, Flickr (syncing and applying), backups, "
             "station QC sampling")
STATE = "local mode: off"            # how the Status page shows what's held back
_on = False


def on() -> bool:
    return _on


def set_on(value: bool) -> None:
    global _on
    _on = bool(value)


def decide(host: str, flag: bool | None = None, environ=os.environ) -> bool:
    """flag: True for --local, False for --production, None for neither."""
    if flag is not None:
        return flag
    env = (environ.get("COCONUT_LOCAL") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    return host in LOOPBACK and not environ.get("RENDER")


def held(what: str) -> None:
    """Log one thing local mode didn't send."""
    logger.info("Local mode: not %s", what)
