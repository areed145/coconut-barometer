"""Photostream and album listings for the web gallery."""
from __future__ import annotations

import json
import math

from .db import Database

SORTS = {"newest": "p.date_upload DESC", "oldest": "p.date_upload ASC", "taken": "p.date_taken DESC",
         "views": "p.views DESC", "faves": "p.faves DESC", "comments": "p.comments DESC", "title": "p.title COLLATE NOCASE"}
SORTS = {k: v + ", p.id" for k, v in SORTS.items()}      # ties (a batch uploaded together) must page the same way every time
PUBLIC = "p.ispublic = 1"   # what visitors who aren't signed in may see (public_only)
GEO_PUBLIC = "p.geo_public = 1"   # ... and whose location they may see (Flickr's geo privacy)


# Presets for the Photos page (?is=a,b: all must hold). Fixed SQL only, chosen by id.
_CAMERA_NAME = " OR ".join(f"p.title GLOB '{g}'" for g in (
    "IMG_*", "IMG-*", "DSC*", "PXL_*", "MVI_*", "VID_*", "GOPR*", "P10[0-9][0-9]*", "[0-9][0-9][0-9][0-9][0-9][0-9]*"))
_TAG_COUNT = "(CASE WHEN COALESCE(p.tags, '') = '' THEN 0 ELSE length(p.tags) - length(replace(p.tags, ' ', '')) + 1 END)"
PRESETS = {
    "untitled": ("Untitled or camera names", "Needs work",
                 f"(COALESCE(p.title, '') = '' OR lower(p.title) = 'untitled' OR {_CAMERA_NAME})"),
    "no_description": ("No description", "Needs work", "COALESCE(p.description, '') = ''"),
    "few_tags": ("Fewer than 5 tags", "Needs work", f"{_TAG_COUNT} < 5"),
    "no_album": ("In no album", "Needs work", "NOT EXISTS (SELECT 1 FROM memberships mm WHERE mm.photo_id = p.id)"),
    "no_group": ("In no group", "Needs work", "NOT EXISTS (SELECT 1 FROM group_photos gp WHERE gp.photo_id = p.id)"),
    "no_geotag": ("No geotag", "Needs work", "(p.lat IS NULL OR (p.lat = 0 AND p.lon = 0))"),
    "popular": ("1,000+ views", "Engagement", "p.views >= 1000"),
    "faved": ("Faved", "Engagement", "p.faves > 0"),
    "commented": ("Commented", "Engagement", "p.comments > 0"),
    "unseen": ("Barely seen (under 10 views)", "Engagement", "COALESCE(p.views, 0) < 10"),
    "to_review": ("In review", "Status", "EXISTS (SELECT 1 FROM queue q WHERE q.photo_id = p.id)"),
    # Processed = looked at by the vision model at least once (with any settings).
    "not_processed": ("Not processed", "Status", "NOT EXISTS (SELECT 1 FROM classifications cl WHERE cl.photo_id = p.id)"),
    "recent": ("Uploaded in the last 30 days", "Status", "p.date_upload > epoch(current_timestamp)::BIGINT - 30 * 86400"),
    "private": ("Private", "Status", "p.ispublic = 0"),
}


NEAR_M = 500                # "nearby": within this many meters (#124)


def near_sql(lat: float, lon: float, meters: float = NEAR_M) -> tuple[str, list, str, list]:
    """Photos within `meters` of a point: a WHERE clause (a box first, for speed, then the circle) and the distance
    to order by, each with its arguments. Equirectangular, in degrees of latitude: exact enough at these distances."""
    dlat = meters / 111_320
    cos2 = max(0.01, math.cos(math.radians(lat))) ** 2
    dlon = dlat / math.sqrt(cos2)
    dist = "((p.lat - ?) * (p.lat - ?) + (p.lon - ?) * (p.lon - ?) * ?)"
    return (f"p.lat BETWEEN ? AND ? AND p.lon BETWEEN ? AND ? AND {dist} <= ?",
            [lat - dlat, lat + dlat, lon - dlon, lon + dlon, lat, lat, lon, lon, cos2, dlat ** 2], dist, [lat, lat, lon, lon, cos2])


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
    def photo_page(self, sort: str = "newest", limit: int = 60, offset: int = 0, **filters) -> dict:
        """One page of photos, by any mix of the filters (see `filtered`)."""
        joins, clause, args = self.filtered(**filters)
        total = self.db.execute(f"SELECT COUNT(*) FROM photos p {joins}{clause}", args).fetchone()[0]
        rows = self.db.execute(f"SELECT p.* FROM photos p {joins}{clause} ORDER BY {SORTS.get(sort, SORTS['newest'])} "
                               f"LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return {"total": total, "offset": offset, "photos": [photo_card(r) for r in rows]}

    # The month each date sort goes by, as "YYYY-MM" (#126). Grouped in the sort's own order, so the counts add up
    # to each month's offset in it. Uploads by UTC month, the same order as by the second.
    MONTHS = {"taken": ("substr(p.date_taken, 1, 7)", "DESC"),
              "newest": ("strftime(to_timestamp(p.date_upload), '%Y-%m')", "DESC"),
              "oldest": ("strftime(to_timestamp(p.date_upload), '%Y-%m')", "ASC")}

    def photo_months(self, sort: str = "newest", **filters) -> list[dict]:
        """For the timeline: [{month, count}] for the filtered photos in `sort` order, or [] for a sort that isn't by date.
        Photos without a date (month None) come last, as they do in the sort."""
        if sort not in self.MONTHS:
            return []
        month, order = self.MONTHS[sort]
        joins, clause, args = self.filtered(**filters)
        return [{"month": r[0], "count": r[1]} for r in self.db.execute(
            f"SELECT {month} AS mo, COUNT(*) FROM photos p {joins}{clause} GROUP BY mo ORDER BY mo {order} NULLS LAST", args)]

    def filtered(self, album_id: str | None = None, q: str = "", media: str | None = None, tag: str = "",
                 year: int | None = None, camera: str = "", presets: list[str] | None = None, public_only: bool = False,
                 on: str = "", near: tuple[float, float] | None = None, day: str = "") -> tuple[str, str, list]:
        """(joins, WHERE clause, arguments) for any mix of filters: album, search text, tag (Flickr's
        normalized form), year taken, day of the year taken (`on`, "MM-DD": "on this day"), the day taken
        (`day`, "YYYY-MM-DD"), within NEAR_M of a point (`near`, (lat, lon)), camera, photo or video,
        and presets (all must hold)."""
        from ..util import norm_tag

        where, args = [PUBLIC] if public_only else [], []
        joins = ""
        if album_id:
            joins = "JOIN memberships m ON m.photo_id = p.id AND m.album_id = ?"
            args.append(album_id)
        if camera:
            joins += " JOIN cameras c ON c.photo_id = p.id AND c.camera = ?"
            args.append(camera)
        if tag:
            where.append("(' ' || p.tags || ' ') ILIKE ?")
            args.append(f"% {norm_tag(tag)} %")
        if year:
            where.append("substr(p.date_taken, 1, 4) = ?")
            args.append(f"{year:04d}")
        if on:
            where.append("substr(p.date_taken, 6, 5) = ? AND COALESCE(p.date_taken_unknown, 0) = 0")
            args.append(on)
        if day:
            where.append("substr(p.date_taken, 1, 10) = ? AND COALESCE(p.date_taken_unknown, 0) = 0")
            args.append(day)
        if near:
            sql, near_args, _, _ = near_sql(*near)
            where.append(sql + (f" AND {GEO_PUBLIC}" if public_only else ""))
            args += near_args
        for name in presets or []:
            where.append(PRESETS[name][2])          # KeyError for an unknown one: the API checks first
        if q:
            where.append("(p.title ILIKE ? OR p.description ILIKE ? OR (' ' || p.tags || ' ') ILIKE ?)")
            args += [f"%{q}%", f"%{q}%", f"% {q.lower().replace(' ', '')} %"]
        if media in ("photo", "video"):
            where.append("p.media = ?")
            args.append(media)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return joins, clause, args

    def photo_highlights(self, year: int, month_start: int, public_only: bool = False) -> dict:
        """For Home: how many photos were taken in `year`, and the most viewed one uploaded since `month_start`."""
        public = f" AND {PUBLIC}" if public_only else ""
        taken = self.db.execute("SELECT COUNT(*) FROM photos p WHERE substr(p.date_taken, 1, 4) = ? "
                                f"AND COALESCE(p.date_taken_unknown, 0) = 0{public}", (f"{year:04d}",)).fetchone()[0]
        top = self.db.execute(f"SELECT p.* FROM photos p WHERE p.date_upload >= ?{public} ORDER BY p.views DESC, p.id LIMIT 1",
                              (month_start,)).fetchone()
        return {"year": year, "taken": taken, "top_upload": photo_card(top) if top else None}

    def related(self, photo_id: str, limit: int = 12, public_only: bool = False) -> dict | None:
        """For the lightbox (#124): photos taken within NEAR_M of this one, nearest first, and photos taken the same day,
        nearest in time first (then in the order taken). Visitors: public photos only, and "nearby" only when this
        photo's location is theirs to see, from photos whose locations are too."""
        public = f" AND {PUBLIC}" if public_only else ""
        p = self.db.execute(f"SELECT lat, lon, geo_public, date_taken, date_taken_unknown FROM photos p WHERE id=?{public}",
                            (photo_id,)).fetchone()
        if not p:
            return None
        nearby, same_day = [], []
        if p["lat"] is not None and p["lon"] is not None and not (p["lat"] == 0 and p["lon"] == 0) \
                and not (public_only and p["geo_public"] != 1):
            where, args, dist, dist_args = near_sql(p["lat"], p["lon"])
            nearby = [photo_card(r) for r in self.db.execute(
                f"SELECT p.* FROM photos p WHERE p.id <> ? AND {where}{public}{f' AND {GEO_PUBLIC}' if public_only else ''} "
                f"ORDER BY {dist}, p.date_taken DESC, p.id LIMIT ?", [photo_id, *args, *dist_args, limit])]
        if p["date_taken"] and not p["date_taken_unknown"]:
            same_day = [photo_card(r) for r in self.db.execute(
                f"SELECT * FROM (SELECT p.* FROM photos p WHERE p.id <> ? AND substr(p.date_taken, 1, 10) = ? "
                f"AND COALESCE(p.date_taken_unknown, 0) = 0{public} "
                "ORDER BY abs(epoch(TRY_CAST(p.date_taken AS TIMESTAMP)) - epoch(TRY_CAST(? AS TIMESTAMP))), p.id LIMIT ?) p "
                "ORDER BY p.date_taken, p.id", [photo_id, p["date_taken"][:10], p["date_taken"], limit])]
        day = p["date_taken"][:10] if same_day else None
        return {"nearby": nearby, "same_day": same_day, "day": day,
                "near": [round(p["lat"], 5), round(p["lon"], 5)] if nearby else None}

    GEOTAGGED = "p.lat IS NOT NULL AND p.lon IS NOT NULL AND NOT (p.lat = 0 AND p.lon = 0)"

    def map_points(self, album_id: str | None = None, year_from: int | None = None,
                   year_to: int | None = None, public_only: bool = False) -> list[list]:
        """[id, lat, lon, server, secret] per geotagged photo (Flickr's 0,0 means none),
        compact because the map takes all of them at once."""
        joins, where, args = "", [self.GEOTAGGED, *([PUBLIC, GEO_PUBLIC] if public_only else [])], []
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
                               f"WHERE {' AND '.join(where)} ORDER BY p.date_upload DESC, p.id", args)
        return [[r[0], round(r[1], 5), round(r[2], 5), r[3], r[4]] for r in rows]

    def map_years(self, public_only: bool = False) -> list[int]:
        return [r[0] for r in self.db.execute(
            f"SELECT DISTINCT CAST(substr(p.date_taken, 1, 4) AS INTEGER) AS y FROM photos p "
            f"WHERE {self.GEOTAGGED}{f' AND {PUBLIC} AND {GEO_PUBLIC}' if public_only else ''} AND p.date_taken IS NOT NULL "
            f"AND y > 1800 ORDER BY y")]

    def preset_counts(self, public_only: bool = False) -> list[dict]:
        """Each preset with how many photos it matches (over the whole library), in one query.
        public_only: only public photos, and not the Status presets (they're about this app's work)."""
        presets = {k: v for k, v in PRESETS.items() if not (public_only and v[1] == "Status")}
        cols = ", ".join(f"SUM(CASE WHEN {sql} THEN 1 ELSE 0 END)" for _, _, sql in presets.values())
        counts = self.db.execute(f"SELECT {cols} FROM photos p{' WHERE ' + PUBLIC if public_only else ''}").fetchone()
        return [{"id": k, "label": label, "group": group, "count": counts[i] or 0}
                for i, (k, (label, group, _)) in enumerate(presets.items())]

    def years_taken(self, public_only: bool = False) -> list[dict]:
        return [{"year": int(r[0]), "count": r[1]} for r in self.db.execute(
            f"SELECT substr(date_taken, 1, 4) y, COUNT(*) FROM photos p WHERE date_taken IS NOT NULL "
            f"{'AND ' + PUBLIC if public_only else ''} AND y GLOB '[12][0-9][0-9][0-9]' GROUP BY y ORDER BY y DESC")]

    def cards(self, photo_ids: list[str], public_only: bool = False) -> dict[str, dict]:
        """photo_id -> card (title, counts, image sizes) for a list of photos."""
        if not photo_ids:
            return {}
        marks = ",".join("?" * len(photo_ids))
        return {r["id"]: photo_card(r) for r in self.db.execute(
            f"SELECT * FROM photos p WHERE id IN ({marks}){' AND ' + PUBLIC if public_only else ''}", photo_ids)}

    def public_photo_ids(self, limit: int) -> list[tuple[str, int]]:
        """(id, last updated) of public photos, newest uploads first: the sitemap's photo pages."""
        return [tuple(r) for r in self.db.execute(
            f"SELECT p.id, COALESCE(p.last_update, p.date_upload) FROM photos p WHERE {PUBLIC} "
            f"ORDER BY p.date_upload DESC, p.id LIMIT ?", (limit,))]

    # An album visitors may see: one with a public photo in it.
    HAS_PUBLIC = f"EXISTS (SELECT 1 FROM memberships m JOIN photos p ON p.id = m.photo_id WHERE m.album_id = a.id AND {PUBLIC})"
    # ... and what they see of it: Flickr's count and cover include private photos, so count the public
    # ones, and if the cover is private (or not in the library), use the newest public photo instead.
    PUBLIC_ALBUM = (
        "a.id, a.title, a.description, a.date_update, "
        f"(SELECT COUNT(*) FROM memberships m JOIN photos p ON p.id = m.photo_id WHERE m.album_id = a.id AND {PUBLIC}) AS count, "
        f"CASE WHEN EXISTS (SELECT 1 FROM photos p WHERE p.id = regexp_extract(a.cover_url, '/([0-9]+)_', 1) AND {PUBLIC}) "
        "THEN a.cover_url ELSE (SELECT 'https://live.staticflickr.com/' || p.server || '/' || p.id || '_' || p.secret || '.jpg' "
        f"FROM memberships m JOIN photos p ON p.id = m.photo_id WHERE m.album_id = a.id AND {PUBLIC} "
        "AND p.server IS NOT NULL AND p.secret IS NOT NULL ORDER BY p.date_upload DESC, p.id LIMIT 1) END AS cover_url")

    def album_list(self, public_only: bool = False) -> list[dict]:
        return [{"id": r["id"], "title": r["title"], "description": r["description"], "count": r["count"],
                 "date_update": r["date_update"], "cover_url": r["cover_url"]}
                for r in self.db.execute(f"SELECT {self.PUBLIC_ALBUM if public_only else '*'} FROM albums a"
                                         f"{' WHERE ' + self.HAS_PUBLIC if public_only else ''} "
                                         f"ORDER BY date_update DESC, title COLLATE NOCASE")]

    def album(self, album_id: str, public_only: bool = False) -> dict | None:
        r = self.db.execute(f"SELECT {self.PUBLIC_ALBUM if public_only else '*'} FROM albums a "
                            f"WHERE id=?{' AND ' + self.HAS_PUBLIC if public_only else ''}", (album_id,)).fetchone()
        return dict(r) if r else None

    def photo_detail(self, photo_id: str, public_only: bool = False) -> dict | None:
        p = self.db.execute(f"SELECT * FROM photos p WHERE id=?{' AND ' + PUBLIC if public_only else ''}",
                            (photo_id,)).fetchone()
        if not p:
            return None
        albums = [dict(r) for r in self.db.execute(
            "SELECT a.id, a.title FROM memberships m JOIN albums a ON a.id = m.album_id WHERE m.photo_id=? "
            "ORDER BY a.title COLLATE NOCASE", (photo_id,))]
        cam = self.db.execute("SELECT camera, lens, exif FROM cameras WHERE photo_id=?", (photo_id,)).fetchone()
        exif = json.loads(cam["exif"] or "{}") if cam else {}
        keep = ("ExposureTime", "FNumber", "ISO", "FocalLength", "FocalLengthIn35mmFormat", "DateTimeOriginal")
        hide_geo = public_only and p["geo_public"] != 1
        return {**photo_card(p), "description": p["description"], "tags": (p["tags"] or "").split(),
                "lat": None if hide_geo else p["lat"], "lon": None if hide_geo else p["lon"], "license": p["license"], "albums": albums,
                "camera": cam["camera"] if cam else None, "lens": cam["lens"] if cam else None,
                "exif": {k: exif[k] for k in keep if k in exif}}
