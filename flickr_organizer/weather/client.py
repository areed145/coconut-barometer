"""The WeatherFlow REST API. The token goes in the query string (their way), so it's kept out of the log."""
from __future__ import annotations

import time
from collections import deque

import requests

BASE = "https://swd.weatherflow.com/swd/rest"


class WeatherFlowError(RuntimeError):
    pass


class WeatherFlowClient:
    def __init__(self, token: str, calls_per_second: float = 2.0, session=None):
        self.token = token
        self.interval = 1.0 / calls_per_second if calls_per_second > 0 else 0.0
        self.session = session or requests.Session()
        self.call_count = 0
        self.log: deque[dict] = deque(maxlen=50)   # recent requests, newest last: {at, path, status, rows, ms}
        self._last = 0.0

    def get(self, path: str, **params) -> dict:
        wait = self._last + self.interval - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        self.call_count += 1
        entry = {"at": int(self._last), "path": path, "params": params, "status": None, "rows": None, "ms": None}
        self.log.append(entry)
        try:
            r = self.session.get(f"{BASE}/{path}", params={**params, "token": self.token}, timeout=30)
            entry["status"] = r.status_code
            if r.status_code in (401, 403):
                raise WeatherFlowError("WeatherFlow refused the token")
            if r.status_code >= 400:
                raise WeatherFlowError(f"WeatherFlow answered {r.status_code}")
            data = r.json()
        except requests.RequestException as e:
            entry["status"] = "error"
            raise WeatherFlowError(f"couldn't reach WeatherFlow: {type(e).__name__}") from e
        finally:
            entry["ms"] = int((time.time() - self._last) * 1000)
        status = data.get("status") or {}
        if status.get("status_code", 0) != 0:
            raise WeatherFlowError(status.get("status_message") or "WeatherFlow reported an error")
        return data

    def stations(self) -> list[dict]:
        """Each station with its devices: Tempests are device_type "ST", the hub is "HB"."""
        data = self.get("stations")
        self.log[-1]["rows"] = len(data.get("stations") or [])
        return data.get("stations") or []

    def device_obs(self, device_id: int, start: int, end: int) -> list[list]:
        """Raw obs_st arrays for [start, end] (epoch seconds)."""
        data = self.get(f"observations/device/{device_id}", time_start=int(start), time_end=int(end))
        obs = data.get("obs") or []
        self.log[-1]["rows"] = len(obs)
        return obs
