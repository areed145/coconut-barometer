"""What the web app holds for APRS: the hub and the APRS-IS thread."""
from __future__ import annotations

import threading

from ..store import Store
from .stream import AprsStream, Hub, callsigns, igate


class AprsService:
    def __init__(self, cfg: dict, db_path: str):
        self.cfg, self.db_path = cfg, db_path
        self.hub = Hub()
        self._stream: AprsStream | None = None
        self._local = threading.local()

    def callsigns(self) -> list[str]:
        return callsigns(self.cfg)

    def igate(self) -> str:
        return igate(self.cfg)

    def store(self) -> Store:
        s = getattr(self._local, "store", None)
        if s is None:
            s = self._local.store = Store(self.db_path)
        return s

    def start_stream(self, stop: threading.Event) -> None:
        if not self._stream:
            self._stream = AprsStream(self, stop)
            self._stream.start()
