"""Results that cost something to get and are kept: vision classifications and EXIF."""
from __future__ import annotations

import json
import time

from .db import Database


class CacheStore(Database):
    def classification(self, photo_id: str, fingerprint: str) -> dict | None:
        row = self.db.execute(
            "SELECT result FROM classifications WHERE photo_id=? AND fingerprint=?", (photo_id, fingerprint)
        ).fetchone()
        return json.loads(row["result"]) if row else None

    def classified_at(self, photo_id: str, fingerprint: str) -> int | None:
        r = self.db.execute("SELECT created_at FROM classifications WHERE photo_id=? AND fingerprint=?",
                            (photo_id, fingerprint)).fetchone()
        return r[0] if r else None

    def classified_ids(self, fingerprint: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT photo_id FROM classifications WHERE fingerprint=?", (fingerprint,))}

    def save_classification(self, photo_id: str, fingerprint: str, result: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO classifications VALUES (?, ?, ?, ?)",
                (photo_id, fingerprint, json.dumps(result), int(time.time())),
            )

    def cameras(self) -> dict[str, str]:
        return {r[0]: r[1] for r in self.db.execute("SELECT photo_id, camera FROM cameras")}

    def camera_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT photo_id FROM cameras")}

    def save_camera(self, photo_id: str, camera: str, make: str, model: str,
                    lens: str = "", exif: dict | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cameras (photo_id, camera, make, model, lens, exif, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (photo_id, camera, make, model, lens, json.dumps(exif or {}), int(time.time())))
