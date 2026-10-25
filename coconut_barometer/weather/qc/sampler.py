"""Station QC's reference samples (#105): what each independent source says at each station, recorded whether or not
anyone has the map open. One thread (QcService.start), a round every TICK seconds; each source keeps its own pace:

    rtma     each 15-minute RTMA analysis at each station (rtma_points.py: a few small Range requests), once it's up;
             and, BACKFILL_PER_TICK at a time, the hourly analyses of the last qc.backfill_days from the archive
    radar    each new MRMS frame the radar service already has (radar.py's ~1 km field around the stations): the
             reflectivity at the station and the most within NEAR_KM
    glm      every GOES-East GLM flash within REACH_KM (glm.py, kept fetching while this asks), with distance and bearing
    alerts   the NWS alerts in effect at each station's point, every ALERTS_EVERY
    fronts   each WPC surface analysis (fronts.py), its fronts and highs and lows within FRONT_KM of a station

Then the checks (suites.py) every RUN_EVERY, and anything older than qc.keep_days dropped once a day. Only reads:
nothing is sent anywhere, so it runs in local mode too. Each source's last problem is kept for the Status page."""
from __future__ import annotations

import math
import threading
import time

import numpy as np
import requests

from ... import activity
from ...errors import caught
from ...gazetteer import USER_AGENT
from . import suites
from .rtma_points import RtmaPoints

TICK = 30
QUARTER, LOOK_BACK = 900, 4          # RTMA's analyses, and how many quarter hours back a new one is looked for
RETRY = 120                          # an analysis not up yet is asked for again after this long
BACKFILL_PER_TICK = 2
NEAR_KM = 3                          # radar: "near the station"
REACH_KM = 50                        # GLM flashes kept per station
GLM_LATE = 15 * 60                   # GLM files can come in late: each round looks this far back (duplicates are ignored)
ALERTS_EVERY, FRONTS_EVERY, RUN_EVERY, PRUNE_EVERY = 300, 900, 3600, 86400
FRONT_KM = 1500
ALERTS_URL = "https://api.weather.gov/alerts/active"
KM_PER_DEG = 111.2


def distance_bearing(lat0: float, lon0: float, lat, lon) -> tuple[np.ndarray, np.ndarray]:
    """Great-circle km and the bearing (degrees from north) from (lat0, lon0) to each point."""
    p0, p1 = math.radians(lat0), np.radians(lat)
    dl = np.radians(np.asarray(lon) - lon0)
    a = np.sin((p1 - p0) / 2) ** 2 + math.cos(p0) * np.cos(p1) * np.sin(dl / 2) ** 2
    km = 2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    bearing = np.degrees(np.arctan2(np.sin(dl) * np.cos(p1), math.cos(p0) * np.sin(p1) - math.sin(p0) * np.cos(p1) * np.cos(dl)))
    return km, bearing % 360


def radar_at(field: dict, lat: float, lon: float, near_km: float = NEAR_KM) -> tuple[float, float] | None:
    """A radar field's dBZ at (lat, lon) and the most within near_km; None outside it."""
    z = field["z"]
    r = int(round((field["north"] - lat) / field["dj"]))
    c = int(round((lon - field["west"]) / field["di"]))
    if not (0 <= r < z.shape[0] and 0 <= c < z.shape[1]):
        return None
    ry = max(1, int(math.ceil(near_km / KM_PER_DEG / field["dj"])))
    rx = max(1, int(math.ceil(near_km / (KM_PER_DEG * max(0.2, math.cos(math.radians(lat)))) / field["di"])))
    box = z[max(0, r - ry):r + ry + 1, max(0, c - rx):c + rx + 1]
    return float(z[r, c]), float(box.max())


class QcService:
    def __init__(self, cfg: dict, radar, points: RtmaPoints | None = None, get=requests.get, clock=time.time,
                 glm_feed=None, fronts_feed=None):
        self.cfg, self.radar, self._get, self._clock = cfg, radar, get, clock
        self.points = points or RtmaPoints(get)
        self._glm, self._fronts = glm_feed, fronts_feed
        self.errors: dict[str, str | None] = {k: None for k in ("rtma", "backfill", "radar", "glm", "alerts", "fronts", "checks")}
        self._tried: dict[int, float] = {}       # RTMA analyses asked for and not up yet -> when
        self._radar_t: int | None = None
        self._next = {"alerts": 0.0, "fronts": 0.0, "checks": 0.0, "prune": 0.0}
        self.backfill: dict = {"to": None, "done": False}
        self._thread: threading.Thread | None = None

    # ---- the loop ---------------------------------------------------------------------------------------------
    def start(self, stop: threading.Event, store_for) -> None:
        if self._thread:
            return

        def run():
            while not stop.wait(TICK):
                self.tick(store_for())
        self._thread = threading.Thread(target=run, name="station-qc", daemon=True)
        self._thread.start()

    def tick(self, store) -> None:
        if not self.cfg["qc"]["enabled"]:
            return
        stations = store.qc_stations()
        if not stations:
            return
        now = self._clock()
        for name, step in (("rtma", self._rtma), ("backfill", self._backfill), ("radar", self._radar_sample),
                           ("glm", self._glm_sample), ("alerts", self._alerts), ("fronts", self._fronts_sample),
                           ("checks", self._checks)):
            try:
                if step(store, stations, now) is not False:     # False: not due this round, so its last problem stands
                    self.errors[name] = None
            except Exception as e:  # noqa: BLE001 - one source down mustn't stop the others
                caught(e, f"station QC {name}")
                self.errors[name] = f"{type(e).__name__}: {e}"[:200]
        if now >= self._next["prune"]:
            self._next["prune"] = now + PRUNE_EVERY
            store.qc_prune(int(now - self.cfg["qc"]["keep_days"] * 86400))

    # ---- RTMA -------------------------------------------------------------------------------------------------
    def _sample_rtma(self, store, stations: list[dict], t: int, idx: str | None = None) -> bool:
        got = self.points.sample(t, [(s["lat"], s["lon"]) for s in stations], idx)
        if got is None:
            return False
        for s, p in zip(stations, got["points"]):
            if p is not None:
                store.qc_save_rtma(s["device_id"], got["t"], p)
        return True

    def _rtma(self, store, stations, now) -> None:
        """The newest analyses not sampled yet (each asked for at most every RETRY until it's up)."""
        newest = int(now) // QUARTER * QUARTER
        have = store.qc_rtma_times(newest - LOOK_BACK * QUARTER)
        for k in range(LOOK_BACK):
            t = newest - k * QUARTER
            if t in have or now - self._tried.get(t, 0) < RETRY:
                continue
            self._tried[t] = now
            with activity.log.call("qc", "RTMA at the stations", "NOAA RTMA (AWS)") as a:
                found = self._sample_rtma(store, stations, t)
                a["detail"] = f"{time.strftime('%H:%MZ', time.gmtime(t))} " + ("sampled" if found else "not up yet")
                if not found:
                    a["status"] = "404"
        self._tried = {t: at for t, at in self._tried.items() if t >= newest - LOOK_BACK * QUARTER}

    def _backfill(self, store, stations, now) -> None:
        """The hourly analyses of the last qc.backfill_days, newest first, BACKFILL_PER_TICK a round; where it's got
        to is kept (meta) so a restart carries on."""
        limit = int(now - self.cfg["qc"]["backfill_days"] * 86400)
        to = int(store.get_meta("qc_backfill_rtma") or (int(now) // 3600 * 3600))
        self.backfill = {"to": to, "done": to - 3600 < limit}
        if to - 3600 < limit:                     # the next hour back would be outside the window
            return False
        have = store.qc_rtma_times(limit)
        done = 0
        while done < BACKFILL_PER_TICK and to - 3600 >= limit:
            t = to - 3600
            if t not in have:
                with activity.log.call("qc", "RTMA backfill", "NOAA RTMA archive (AWS)") as a:
                    found = self._sample_rtma(store, stations, t)
                    a["detail"] = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(t)) + ("" if found else ": none in the archive")
                done += 1
            to = t
            store.set_meta("qc_backfill_rtma", to)
        self.backfill = {"to": to, "done": to - 3600 < limit}

    # ---- radar, GLM, alerts, fronts ---------------------------------------------------------------------------
    def _radar_sample(self, store, stations, now) -> None:
        with self.radar.lock:
            field = self.radar.field
        if not field or field["t"] == self._radar_t:
            return False
        rows = []
        for s in stations:
            got = radar_at(field, s["lat"], s["lon"])
            if got is not None:
                rows.append((s["device_id"], int(field["t"]), *got))
        store.qc_save_radar(rows)
        self._radar_t = field["t"]

    def _glm_sample(self, store, stations, now) -> None:
        if self._glm is None:
            from ..glm import glm
            self._glm = glm
        state = self._glm.current()                   # keeps GLM coming in while QC asks, map open or not
        data = state["data"]
        if not data or not len(data["t"]):
            return
        recent = data["t"] >= now - GLM_LATE
        t, lat, lon = data["t"][recent], data["lat"][recent] / 100, data["lon"][recent] / 100
        rows = []
        for s in stations:
            box = (np.abs(lat - s["lat"]) < REACH_KM / KM_PER_DEG + 0.1) & (np.abs(lon - s["lon"]) < 1.5)
            if not box.any():
                continue
            km, bearing = distance_bearing(s["lat"], s["lon"], lat[box], lon[box])
            for ts, la, lo, d, b in zip(t[box], lat[box], lon[box], km, bearing):
                if d <= REACH_KM:
                    rows.append((s["device_id"], int(ts), round(float(la), 2), round(float(lo), 2), round(float(d), 2), round(float(b), 1)))
        if rows:
            store.qc_save_glm(rows)

    def _alerts(self, store, stations, now) -> None:
        if now < self._next["alerts"]:
            return False
        self._next["alerts"] = now + ALERTS_EVERY
        from datetime import datetime

        def epoch(s):
            try:
                return int(datetime.fromisoformat(s).timestamp()) if s else None
            except ValueError:
                return None
        with activity.log.call("qc", "NWS alerts at the stations", "NWS API") as a:
            n = 0
            for s in stations:
                r = self._get(ALERTS_URL, params={"point": f"{s['lat']:.4f},{s['lon']:.4f}"},
                              headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"}, timeout=20)
                r.raise_for_status()
                alerts = [{"id": p.get("id"), "event": p.get("event"), "onset": epoch(p.get("onset")),
                           "ends": epoch(p.get("ends") or p.get("expires"))}
                          for p in (f.get("properties") or {} for f in r.json().get("features") or []) if p.get("id")]
                store.qc_save_alerts(s["device_id"], alerts, int(now))
                n += len(alerts)
            a["detail"] = f"{n} in effect"

    def _fronts_sample(self, store, stations, now) -> None:
        if now < self._next["fronts"]:
            return False
        self._next["fronts"] = now + FRONTS_EVERY
        if self._fronts is None:
            from ..fronts import fronts
            self._fronts = fronts
        latest = self._fronts.latest()
        if not latest.get("valid"):
            return

        def near(lat, lon):
            return any(distance_bearing(s["lat"], s["lon"], lat, lon)[0] <= FRONT_KM for s in stations)
        kept = {"fronts": [f for f in latest["fronts"] if any(near(la, lo) for la, lo in f["points"])],
                "centers": [c for c in latest["centers"] if near(c["lat"], c["lon"])]}
        store.qc_save_front(int(latest["valid"]), kept)

    def _checks(self, store, stations, now) -> None:
        if now < self._next["checks"]:
            return False
        self._next["checks"] = now + RUN_EVERY
        self.run_checks(store, now)

    def run_checks(self, store, now: float | None = None) -> list[dict]:
        with activity.log.call("qc", "station checks") as a:
            out = suites.run(store, self.cfg, now or self._clock())
            a["detail"] = ", ".join(f"{r['suite']}: {r['status']}" for r in out) or "no stations"
        return out
