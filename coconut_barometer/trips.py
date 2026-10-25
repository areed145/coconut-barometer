"""Trips (#125): runs of photos close together in time and place, found by themselves, shown as album-like cards.

Photos in the order taken (known dates only) start a new trip after a gap of more than trips.gap_hours, after a move
of more than trips.jump_km across a pause of over PAUSE_HOURS (somewhere far away the next day: the flight home,
not the next stop on a road trip, nor a day trip from the hotel),
or once a trip would run past MAX_DAYS (a month of everyday photos at home is a few "moments", not one trip). Runs of
fewer than trips.min_photos are dropped.

Each is named from where most of its photos were taken, by the offline geocoder's towns: the town when most are in
one, else the state or region, else the country. Visitors get a second name, from public photos with public
locations only, so a hidden location never shows through a name.

Built after each sync and whenever the photos or these settings change (`ensure`), and kept in the trips and
trip_photos tables, so pages never cluster on a request.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .geo import country_names, haversine_km, offline_geocoder

logger = logging.getLogger(__name__)

PAUSE_HOURS = 20
MAX_DAYS = 14


@dataclass
class Trip:
    photos: list = field(default_factory=list)       # rows: id, lat, lon, date_taken, ispublic, geo_public
    first: datetime | None = None
    last: datetime | None = None
    where: tuple[float, float] | None = None          # the last located photo's position


def _taken(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _located(r) -> bool:
    return r["lat"] is not None and r["lon"] is not None and not (r["lat"] == 0 and r["lon"] == 0)


def cluster(rows, gap_hours: float, jump_km: float, min_photos: int) -> list[Trip]:
    """Rows in the order taken -> the trips, each with at least min_photos."""
    trips: list[Trip] = []
    cur: Trip | None = None
    for r in rows:
        t = _taken(r["date_taken"])
        if t is None:
            continue
        here = (r["lat"], r["lon"]) if _located(r) else None
        if cur:
            gap = (t - cur.last).total_seconds() / 3600
            moved = here and cur.where and haversine_km(*cur.where, *here) > jump_km
            if gap > gap_hours or (moved and gap > PAUSE_HOURS) or (t - cur.first).days >= MAX_DAYS:
                cur = None
        if cur is None:
            cur = Trip(first=t)
            trips.append(cur)
        cur.photos.append(r)
        cur.last = t
        cur.where = here or cur.where
    return [t for t in trips if len(t.photos) >= min_photos]


def _country(cc: str) -> str:
    return country_names(cc)[0] if cc else ""


def place_name(places: list[dict]) -> str:
    """A name for where these geocoder results ({cc, admin1, name}) mostly are: "Big Sur, California" (a US town and its
    state), "Kyoto, Japan", "California, United States", "Japan"; "" for none."""
    if not places:
        return ""
    half = len(places) / 2
    (town, admin1, cc), n = Counter((p.get("name", ""), p.get("admin1", ""), p.get("cc", "")) for p in places).most_common(1)[0]
    if town and n >= half:
        return f"{town}, {admin1 if cc == 'US' and admin1 else _country(cc)}"
    (admin1, cc), n = Counter((p.get("admin1", ""), p.get("cc", "")) for p in places).most_common(1)[0]
    if admin1 and n >= half:
        return f"{admin1}, {_country(cc)}"
    return _country(Counter(p.get("cc", "") for p in places).most_common(1)[0][0])


def build(store, gap_hours: float, jump_km: float, min_photos: int, geocoder=offline_geocoder) -> list[dict]:
    """Every trip, named: [{id, place, place_public, photo_ids}]. A trip's id is its first photo's, so it stays the same
    from one build to the next while that photo still starts it (links to it keep working)."""
    rows = store.db.execute(
        "SELECT id, lat, lon, date_taken, ispublic, geo_public FROM photos WHERE date_taken IS NOT NULL "
        "AND COALESCE(date_taken_unknown, 0) = 0 ORDER BY date_taken, id").fetchall()
    trips = cluster(rows, gap_hours, jump_km, min_photos)
    located = [r for t in trips for r in t.photos if _located(r)]
    try:
        found = geocoder([(r["lat"], r["lon"]) for r in located]) if located else []
    except Exception as e:                             # no names rather than no trips
        logger.warning("trips: geocoding failed: %s", e)
        found = [{} for _ in located]
    place = {r["id"]: p for r, p in zip(located, found)}
    out = []
    for t in trips:
        here = [place[r["id"]] for r in t.photos if r["id"] in place]
        public = [place[r["id"]] for r in t.photos if r["id"] in place and r["ispublic"] == 1 and r["geo_public"] == 1]
        out.append({"id": t.photos[0]["id"], "place": place_name(here), "place_public": place_name(public),
                    "photo_ids": [r["id"] for r in t.photos]})
    return out


_LOCK = threading.Lock()


def ensure(cfg: dict, store, geocoder=offline_geocoder, force: bool = False) -> bool:
    """Build the trips if the photos (how many, the latest change) or the settings changed since the last build.
    Returns whether it built them."""
    tc = cfg["trips"]
    with _LOCK:
        n, latest, newest = store.db.execute("SELECT COUNT(*), MAX(last_update), MAX(date_taken) FROM photos").fetchone()
        key = f"{n}:{latest}:{newest}:{tc['gap_hours']}:{tc['jump_km']}:{tc['min_photos']}"
        if not force and store.get_meta("trips_key") == key:
            return False
        started = time.time()
        trips = build(store, tc["gap_hours"], tc["jump_km"], tc["min_photos"], geocoder)
        store.save_trips(trips)
        store.set_meta("trips_key", key)
        logger.info("trips: %d from %d photos in %.1fs", len(trips), n, time.time() - started)
        return True
