"""Where a visitor is, from their IP (#83): DB-IP's free IP to City Lite database (CC BY 4.0, https://db-ip.com),
updated monthly. The file is ~130 MB: it's kept in geoip/ next to the database, read in place (mmap, so it
isn't loaded into memory), and never goes in a backup (backups copy the database only); a new server
downloads its own. Only the city, region and country it gives are kept, never the IP.

Phones on a carrier's network often come out as the carrier's hub city, not where they are."""
from __future__ import annotations

import datetime as dt
import gzip
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import maxminddb
import requests

from . import activity
from .errors import caught

logger = logging.getLogger(__name__)

URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"
FILE = "dbip-city-lite.mmdb"
MAX_AGE = 32 * 86400          # DB-IP publishes at the start of each month
RETRY = 6 * 3600              # after a failed download


def _months(today: dt.date) -> list[str]:
    """This month's file, then last month's (early in a month, this one may not be out yet)."""
    last = today.replace(day=1) - dt.timedelta(days=1)
    return [today.strftime("%Y-%m"), last.strftime("%Y-%m")]


class GeoIP:
    def __init__(self, folder: str | os.PathLike):
        self.path = Path(folder) / FILE
        self._reader = None
        self._opened_at = 0.0           # the file's mtime when it was opened
        self._lock = threading.Lock()
        self.updating = False
        self.error: str | None = None
        self.tried_at = 0.0

    def _open(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return None
        with self._lock:
            if self._reader is None or mtime != self._opened_at:
                old, self._reader = self._reader, maxminddb.open_database(str(self.path), maxminddb.MODE_MMAP)
                self._opened_at = mtime
                if old:
                    old.close()
            return self._reader

    def lookup(self, ip: str) -> dict:
        """{city, region, country, country_code}, or {} (no file yet, a private address, not in it)."""
        try:
            reader = self._open()
            rec = reader.get(ip) if reader else None
        except (ValueError, maxminddb.InvalidDatabaseError, OSError):
            return {}
        if not rec:
            return {}
        name = lambda d: ((d or {}).get("names") or {}).get("en")
        subs = rec.get("subdivisions") or [{}]
        return {"city": name(rec.get("city")), "region": name(subs[0]), "country": name(rec.get("country")),
                "country_code": (rec.get("country") or {}).get("iso_code")}

    def age(self, now: float | None = None) -> float | None:
        try:
            return (now or time.time()) - self.path.stat().st_mtime
        except OSError:
            return None

    def due(self, now: float | None = None) -> bool:
        now = now or time.time()
        age = self.age(now)
        return not self.updating and (age is None or age > MAX_AGE) and now - self.tried_at > RETRY

    def refresh(self, get=requests.get, today: dt.date | None = None) -> bool:
        """Download the newest file and swap it in. True if it did."""
        self.updating, self.tried_at = True, time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        gz, tmp = self.path.with_suffix(".mmdb.gz.part"), self.path.with_suffix(".mmdb.part")
        try:
            for month in _months(today or dt.date.today()):
                url = URL.format(month=month)
                with activity.log.call("geoip", "city database", month) as a:
                    with get(url, stream=True, timeout=60) as r:
                        if r.status_code == 404:
                            a["status"] = "not out yet"
                            continue
                        r.raise_for_status()
                        with open(gz, "wb") as f:
                            for chunk in r.iter_content(1 << 20):
                                f.write(chunk)
                    with gzip.open(gz, "rb") as src, open(tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    maxminddb.open_database(str(tmp)).close()       # it opens: it's whole
                    os.replace(tmp, self.path)
                    a["detail"] = f"{self.path.stat().st_size / 1e6:.0f} MB"
                self.error = None
                logger.info("GeoIP: DB-IP city database for %s", month)
                return True
            self.error = "no file for this month or last"
            return False
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[:300]
            caught(e, "geoip")
            return False
        finally:
            self.updating = False
            for f in (gz, tmp):
                f.unlink(missing_ok=True)

    def close(self) -> None:
        with self._lock:
            if self._reader:
                self._reader.close()
                self._reader = None
