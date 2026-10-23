"""What the web app holds for APRS: the hub, the APRS-IS thread (receive-only) and the CWOP reporter."""
from __future__ import annotations

import threading

from ..store import Store
from .cwop import CwopReporter, configured
from .stream import AprsStream, Hub, callsigns, igate


class AprsService:
    def __init__(self, cfg: dict, db_path: str):
        self.cfg, self.db_path = cfg, db_path
        self.hub = Hub()
        self._stream: AprsStream | None = None
        self._reporter: CwopReporter | None = None
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
        if not self._reporter:                 # idle until Settings → APRS gives a station a callsign
            self._reporter = CwopReporter(self, stop)
            self._reporter.start()

    def cwop_status(self) -> dict:
        """For Settings → APRS: which stations report as what, and the last round (callsigns only)."""
        last = self._reporter.last if self._reporter else {"at": None, "sent": [], "error": None}
        return {"reporting": [{"station": s["name"], "callsign": c} for s, c in configured(self.cfg, self.store().wx_stations())],
                "every_minutes": self.cfg["aprs"].get("cwop_minutes"), "last_at": last["at"], "error": last["error"],
                "last_sent": [p.split(">")[0] for p in last["sent"]]}
