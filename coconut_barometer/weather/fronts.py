"""Surface fronts and pressure centers for the Weather map (#69): the Weather Prediction Center's high-resolution
coded surface bulletin, its official surface analysis every 3 hours, as text:

    VALID 102409Z
    HIGHS 1033 6431100 1037 6361329 ...         central pressure (mb), then where
    LOWS 1006 5101350 993 5431017 ...
    COLD 5101351 4941351 4821373 4751415         the points along a front, in order
    WARM ... / STNRY ... / OCFNT ... / TROF ...

A place is seven digits: latitude in tenths of a degree north (3), then longitude in tenths west (4); the older
bulletin has five, whole degrees (2 + 3). A line that doesn't start with a keyword carries on the one before.
Walking a front's points in order, the side it's moving toward (where its triangles and half circles go) is on the
left: checked against fronts around their lows, and WPC's own example.

Fetched when the map asks and what's kept is older than TTL, only if it changed (If-Modified-Since); while WPC
can't be reached, the last analysis is kept (with the error). One per process (`fronts`)."""
from __future__ import annotations

import calendar
import re
import threading
import time

import requests

from .. import activity
from ..errors import caught

URL = "https://www.wpc.ncep.noaa.gov/discussions/codsus_hr"
TTL = 15 * 60
TYPES = {"COLD": "cold", "WARM": "warm", "STNRY": "stationary", "OCFNT": "occluded", "TROF": "trough"}
CENTERS = {"HIGHS": "H", "LOWS": "L"}
_PLACE = re.compile(r"\d{5}|\d{7}")


def place(code: str) -> tuple[float, float]:
    """'5101350' -> (51.0, -135.0); '51135' -> (51.0, -135.0)."""
    if len(code) == 7:
        return int(code[:3]) / 10, -int(code[3:]) / 10
    if len(code) == 5:
        return float(int(code[:2])), -float(int(code[2:]))
    raise ValueError(f"not a place: {code!r}")


def valid_time(code: str, now: float) -> int:
    """'102409Z' (month, day, hour UTC) -> epoch seconds, in whichever year puts it nearest `now`."""
    m = re.fullmatch(r"(\d\d)(\d\d)(\d\d)Z?", code)
    if not m:
        raise ValueError(f"not a valid time: {code!r}")
    month, day, hour = (int(x) for x in m.groups())
    year = time.gmtime(now).tm_year
    options = [calendar.timegm((y, month, day, hour, 0, 0, 0, 0, 0)) for y in (year - 1, year, year + 1)]
    return min(options, key=lambda t: abs(t - now))


def parse(text: str, now: float | None = None) -> dict:
    """A bulletin -> {valid, fronts: [{type, points: [[lat, lon], ...]}], centers: [{kind: H|L, mb, lat, lon}]}."""
    now = now or time.time()
    valid, fronts, centers, current = None, [], [], None
    for word in text.replace("$$", " ").split():
        if word == "VALID":
            current = "VALID"
        elif word in TYPES:
            current = {"type": TYPES[word], "points": []}
            fronts.append(current)
        elif word in CENTERS:
            current = {"kind": CENTERS[word], "mb": None}
        elif current == "VALID":
            valid, current = valid_time(word, now), None
        elif isinstance(current, dict) and "points" in current and _PLACE.fullmatch(word):
            current["points"].append(list(place(word)))
        elif isinstance(current, dict) and "kind" in current and word.isdigit():
            if current["mb"] is None and len(word) in (3, 4):              # a center's pressure comes first ...
                current["mb"] = int(word)
            elif current["mb"] is not None and _PLACE.fullmatch(word):     # ... then where it is
                lat, lon = place(word)
                centers.append({"kind": current["kind"], "mb": current["mb"], "lat": lat, "lon": lon})
                current["mb"] = None
    if valid is None:
        raise ValueError("no VALID time: not a coded surface bulletin")
    return {"valid": valid, "fronts": [f for f in fronts if len(f["points"]) >= 2], "centers": centers}


class Fronts:
    def __init__(self, get=requests.get, clock=time.time, ttl: int = TTL):
        self._get, self._clock, self._ttl = get, clock, ttl
        self._lock = threading.Lock()
        self._modified: str | None = None
        self.analysis: dict | None = None
        self.checked_at: float | None = None
        self.error: str | None = None

    def latest(self) -> dict:
        """The newest analysis (fetched if what's kept is older than TTL), with when it was checked and any error."""
        with self._lock:
            if self.checked_at is None or self._clock() - self.checked_at >= self._ttl:
                self._refresh()
            return {**(self.analysis or {"valid": None, "fronts": [], "centers": []}),
                    "checked_at": self.checked_at, "error": self.error}

    def _refresh(self) -> None:
        self.checked_at = self._clock()
        try:
            with activity.log.call("imagery", "surface fronts", "WPC coded surface bulletin") as a:
                r = self._get(URL, headers={"If-Modified-Since": self._modified} if self._modified else {}, timeout=20)
                if r.status_code == 304:
                    a["status"], a["detail"] = "304", "no new analysis"
                    self.error = None
                    return
                r.raise_for_status()
                analysis = parse(r.text, self.checked_at)
                a["detail"] = f"{len(analysis['fronts'])} fronts, {len(analysis['centers'])} highs and lows"
            self.analysis, self._modified, self.error = analysis, r.headers.get("Last-Modified"), None
        except Exception as e:  # noqa: BLE001 - WPC being down keeps the last analysis
            caught(e, "fronts")
            self.error = f"{type(e).__name__}: {e}"[:200]


fronts = Fronts()
