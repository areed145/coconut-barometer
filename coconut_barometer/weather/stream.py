"""The WebSocket: asks WeatherFlow for every Tempest's observations, rapid wind and events as they
happen. Observations and events are stored; rapid wind (every 3 s) only lives in the hub, for the
Live page. On every (re)connect the REST gap-fill is asked for, so a restart or a dropped
connection leaves no hole."""
from __future__ import annotations

import json
import threading
import time
from collections import deque

from .. import activity

FILL_EVERY = 3600      # seconds between REST gap-fills while connected
WS_URL = "wss://ws.weatherflow.com/swd/data?token={token}"


class Hub:
    """What the Live page reads: the newest rapid wind per device, and the stream's state."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rapid: dict[int, dict] = {}
        self.status = {"state": "off", "since": None, "last_message": None, "messages": 0, "reconnects": 0, "error": ""}
        self.events: deque[dict] = deque(maxlen=20)       # strikes and rain starts, newest last

    def update(self, **kw) -> None:
        with self.lock:
            self.status.update(kw)

    def count(self) -> None:
        with self.lock:
            self.status["messages"] += 1
            self.status["last_message"] = int(time.time())

    def snapshot(self) -> dict:
        with self.lock:
            return {"stream": dict(self.status), "rapid": {str(k): dict(v) for k, v in self.rapid.items()},
                    "events": list(self.events)}


def _describe(message: dict) -> str:
    dev, kind = message.get("device_id"), message.get("type")
    if kind == "obs_st":
        o = (message.get("obs") or [[]])[0]
        return f"device {dev} · {o[7]} °C, wind {o[2]} m/s" if len(o) > 7 else f"device {dev}"
    if kind == "evt_strike":
        return f"device {dev} · {(message.get('evt') or [None, None])[1]} km away"
    return " ".join(str(x) for x in (f"device {dev}" if dev else "", message.get("id") or "") if x)


def handle(message: dict, store, hub: Hub) -> str | None:
    """One message from the socket. Returns its type."""
    kind, dev = message.get("type"), message.get("device_id")
    if kind == "rapid_wind":
        activity.log.skip("wx-stream")        # every 3 s per device: counted, not listed
    else:
        activity.log.record("wx-stream", kind or "message", _describe(message))
    if kind == "obs_st":
        store.wx_save_obs(dev, message.get("obs") or [])
    elif kind == "rapid_wind":
        ts, speed, direction = (message.get("ob") or [None, None, None])[:3]
        if ts is not None:
            with hub.lock:
                hub.rapid[dev] = {"ts": int(ts), "speed": speed, "dir": direction}
    elif kind == "evt_strike":
        ts, dist, energy = (message.get("evt") or [None, None, None])[:3]
        if ts is not None:
            store.wx_save_event(dev, int(ts), "strike", dist, energy)
            with hub.lock:
                hub.events.append({"device_id": dev, "ts": int(ts), "kind": "strike", "distance": dist})
    elif kind == "evt_precip":
        ts = int(time.time())
        store.wx_save_event(dev, ts, "rain_start")
        with hub.lock:
            hub.events.append({"device_id": dev, "ts": ts, "kind": "rain_start"})
    return kind


class WeatherStream(threading.Thread):
    """Runs until stop is set. `service` supplies the token, the hub and the Store to write with;
    `on_connect` is called after each successful connection (to queue the gap-fill)."""

    def __init__(self, service, stop: threading.Event, on_connect=lambda: None, connect=None):
        super().__init__(name="weather-stream", daemon=True)
        self.service, self.stop_event, self.on_connect = service, stop, on_connect
        self._connect = connect          # tests: a function (url) -> a connection with recv()/send()/close()

    def run(self) -> None:
        delay = 5
        while not self.stop_event.is_set():
            token = self.service.token()
            devices = [d["device_id"] for d in self.service.store().wx_devices()] if token else []
            if not devices:
                self.service.hub.update(state="waiting", error="" if token else "no token")
                if token:
                    self.on_connect()      # no stations known yet: the REST sync finds them
                self.stop_event.wait(120 if token else 10)
                continue
            try:
                self._session(token, devices)
                delay = 5
            except Exception as e:
                self.service.hub.update(state="reconnecting", error=f"{type(e).__name__}: {e}"[:200])
                activity.log.record("wx-stream", "disconnected", f"{type(e).__name__}: {e}"[:200], "reconnecting",
                                    error=True, kind="connect")
                self.service.hub.status["reconnects"] += 1
            self.stop_event.wait(delay)
            delay = min(delay * 2, 300)

    def _session(self, token: str, devices: list[int]) -> None:
        hub, store = self.service.hub, self.service.store()
        if self._connect:
            ws = self._connect(WS_URL.format(token=token))
        else:
            from websockets.sync.client import connect
            ws = connect(WS_URL.format(token=token), open_timeout=15, ping_interval=20)
        try:
            for i, dev in enumerate(devices):
                ws.send(json.dumps({"type": "listen_start", "device_id": dev, "id": f"obs-{dev}-{i}"}))
                ws.send(json.dumps({"type": "listen_rapid_start", "device_id": dev, "id": f"rapid-{dev}-{i}"}))
            hub.update(state="connected", since=int(time.time()), error="")
            activity.log.record("wx-stream", "connected", f"listening to {len(devices)} device{'s' * (len(devices) != 1)}", kind="connect")
            self.on_connect()
            filled = time.time()
            while not self.stop_event.is_set() and self.service.token() == token:
                if time.time() - filled > FILL_EVERY:      # a safety net behind the stream
                    self.on_connect()
                    filled = time.time()
                try:
                    raw = ws.recv(timeout=60)
                except TimeoutError:
                    continue            # quiet for a minute: pings keep the connection; look at stop and wait again
                hub.count()
                handle(json.loads(raw), store, hub)
        finally:
            ws.close()
            hub.update(state="disconnected")
