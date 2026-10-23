"""What the web app holds for the weather: the REST client (rebuilt when the token changes), the
live hub, and the WebSocket thread."""
from __future__ import annotations

import threading

from ..config import WEATHERFLOW_CALLS_PER_SECOND
from ..store import Store
from .client import WeatherFlowClient
from .stream import Hub, WeatherStream
from . import sync as wxsync


class WeatherService:
    def __init__(self, cfg: dict, db_path: str):
        self.cfg, self.db_path = cfg, db_path
        self.hub = Hub()
        self._client: WeatherFlowClient | None = None
        self._stream: WeatherStream | None = None
        self._local = threading.local()

    def token(self) -> str | None:
        return self.cfg["weatherflow"].get("token") or None

    def store(self) -> Store:
        s = getattr(self._local, "store", None)
        if s is None:
            s = self._local.store = Store(self.db_path)
        return s

    @property
    def client(self) -> WeatherFlowClient | None:
        token = self.token()
        if not token:
            self._client = None
        elif self._client is None or self._client.token != token:
            old = self._client
            self._client = WeatherFlowClient(token, WEATHERFLOW_CALLS_PER_SECOND)
            if old:                                   # the Status page's counter carries on across a token change
                self._client.call_count, self._client.log = old.call_count, old.log
        return self._client

    def sync(self, store, log=lambda *a: None) -> dict:
        client = self.client
        if client is None:
            return {"status": "no_token"}
        return {"status": "done", **wxsync.sync(client, store, self.cfg["weatherflow"]["backfill_days"], log=log)}

    def start_stream(self, stop: threading.Event, on_connect) -> None:
        if not self._stream:
            self._stream = WeatherStream(self, stop, on_connect)
            self._stream.start()
