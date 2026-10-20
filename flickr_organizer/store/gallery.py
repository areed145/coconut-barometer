"""Photostream and album listings for the web gallery."""
from __future__ import annotations

import json

from .db import Database

SORTS = {"newest": "p.date_upload DESC", "oldest": "p.date_upload ASC", "taken": "p.date_taken DESC",
         "views": "p.views DESC", "faves": "p.faves DESC", "comments": "p.comments DESC", "title": "p.title COLLATE NOCASE"}


def sizes(p) -> dict:
    """Image URLs by Flickr size suffix. q n w z c b share the photo's secret; l h k need their own."""
    base = f"https://live.staticflickr.com/{p['server']}/{p['id']}_{p['secret']}" if p["server"] and p["secret"] else None
    out = {k: f"{base}_{k}.jpg" for k in ("q", "n", "w", "z", "c", "b")} if base else {}
    out.update({k: p[f"url_{k}"] for k in ("l", "h", "k") if p[f"url_{k}"]})
    if not out and p["thumb_url"]:
        out["m"] = p["thumb_url"]
    return out


def photo_card(p) -> dict:
    return {"id": p["id"], "title": p["title"], "media": p["media"], "date_taken": p["date_taken"],
            "date_upload": p["date_upload"], "views": p["views"], "faves": p["faves"], "comments": p["comments"],
            "width": p["width"], "height": p["height"], "ispublic": p["ispublic"], "sizes": sizes(p)}


class GalleryStore(Database):
    def photo_page(self, album_id: str | None = None, q: str = "", sort: str = "newest", media: str | None = None,
                   limit: int = 60, offset: int = 0, tag: str = "", year: int | None = None, camera: str = "") -> dict:
        """One page of photos, by any mix of filters: album, search text, tag (Flickr's
        normalized form), year taken, camera, photo or video."""
        from ..util import norm_tag

        where, args = [], []
        joins = ""
        if album_id:
            joins = "JOIN memberships m ON m.photo_id = p.id AND m.album_id = ?"
            args.append(album_id)
        if camera:
            joins += " JOIN cameras c ON c.photo_id = p.id AND c.camera = ?"
            args.append(camera)
        if tag:
            where.append("(' ' || p.tags || ' ') LIKE ?")
            args.append(f"% {norm_tag(tag)} %")
        if year:
            where.append("substr(p.date_taken, 1, 4) = ?")
            args.append(f"{year:04d}")
        if q:
            where.append("(p.title LIKE ? OR p.description LIKE ? OR (' ' || p.tags || ' ') LIKE ?)")
            args += [f"%{q}%", f"%{q}%", f"% {q.lower().replace(' ', '')} %"]
        if media in ("photo", "video"):
            where.append("p.media = ?")
            args.append(media)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.db.execute(f"SELECT COUNT(*) FROM photos p {joins}{clause}", args).fetchone()[0]
        rows = self.db.execute(f"SELECT p.* FROM photos p {joins}{clause} ORDER BY {SORTS.get(sort, SORTS['newest'])} "
                               f"LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return {"total": total, "offset": offset, "photos": [photo_card(r) for r in rows]}

    GEOTAGGED = "p.lat IS NOT NULL AND p.lon IS NOT NULL AND NOT (p.lat = 0 AND p.lon = 0)"

    def map_points(self, album_id: str | None = None, year_from: int | None = None,
                   year_to: int | None = None) -> list[list]:
        """[id, lat, lon, server, secret] per geotagged photo (Flickr's 0,0 means none),
        compact because the map takes all of them at once."""
        joins, where, args = "", [self.GEOTAGGED], []
        if album_id:
            joins = "JOIN memberships m ON m.photo_id = p.id AND m.album_id = ?"
            args.append(album_id)
        year = "CAST(substr(p.date_taken, 1, 4) AS INTEGER)"
        if year_from:
            where.append(f"{year} >= ?")
            args.append(year_from)
        if year_to:
            where.append(f"{year} <= ?")
            args.append(year_to)
        rows = self.db.execute(f"SELECT p.id, p.lat, p.lon, p.server, p.secret FROM photos p {joins} "
                               f"WHERE {' AND '.join(where)} ORDER BY p.date_upload DESC", args)
        return [[r[0], round(r[1], 5), round(r[2], 5), r[3], r[4]] for r in rows]

    def map_years(self) -> list[int]:
        return [r[0] for r in self.db.execute(
            f"SELECT DISTINCT CAST(substr(p.date_taken, 1, 4) AS INTEGER) AS y FROM photos p "
            f"WHERE {self.GEOTAGGED} AND p.date_taken IS NOT NULL AND y > 1800 ORDER BY y")]

    def years_taken(self) -> list[dict]:
        return [{"year": int(r[0]), "count": r[1]} for r in self.db.execute(
            "SELECT substr(date_taken, 1, 4) y, COUNT(*) FROM photos WHERE date_taken IS NOT NULL "
            "AND y GLOB '[12][0-9][0-9][0-9]' GROUP BY y ORDER BY y DESC")]

    def cards(self, photo_ids: list[str]) -> dict[str, dict]:
        """photo_id -> card (title, counts, image sizes) for a list of photos."""
        if not photo_ids:
            return {}
        marks = ",".join("?" * len(photo_ids))
        return {r["id"]: photo_card(r) for r in self.db.execute(f"SELECT * FROM photos WHERE id IN ({marks})", photo_ids)}

    def album_list(self) -> list[dict]:
        return [{"id": r["id"], "title": r["title"], "description": r["description"], "count": r["count"],
                 "date_update": r["date_update"], "cover_url": r["cover_url"]}
                for r in self.db.execute("SELECT * FROM albums ORDER BY date_update DESC, title COLLATE NOCASE")]

    def album(self, album_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
        return dict(r) if r else None

    def photo_detail(self, photo_id: str) -> dict | None:
        p = self.db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not p:
            return None
        albums = [dict(r) for r in self.db.execute(
            "SELECT a.id, a.title FROM memberships m JOIN albums a ON a.id = m.album_id WHERE m.photo_id=? "
            "ORDER BY a.title COLLATE NOCASE", (photo_id,))]
        cam = self.db.execute("SELECT camera, lens, exif FROM cameras WHERE photo_id=?", (photo_id,)).fetchone()
        exif = json.loads(cam["exif"] or "{}") if cam else {}
        keep = ("ExposureTime", "FNumber", "ISO", "FocalLength", "FocalLengthIn35mmFormat", "DateTimeOriginal")
        return {**photo_card(p), "description": p["description"], "tags": (p["tags"] or "").split(),
                "lat": p["lat"], "lon": p["lon"], "license": p["license"], "albums": albums,
                "camera": cam["camera"] if cam else None, "lens": cam["lens"] if cam else None,
                "exif": {k: exif[k] for k in keep if k in exif}}
