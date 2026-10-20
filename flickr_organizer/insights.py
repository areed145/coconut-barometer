"""The analytics tabs beyond plain totals: photos worth a look, tags, places, gear,
audience, albums and groups, and whether this tool's changes helped.

Everything reads the local database only (no Flickr calls). Per-day numbers exist only
for the days collected (photo_daily); all-time numbers come from each photo's totals.
Comparisons use medians and "relative views" (a photo's views over the median for photos
uploaded the same year), so a few viral photos or the age of a photo don't skew them.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict

from .util import norm_tag

DAY = 86400
_DATED = re.compile(r"^(y?(18|19|20)\d\d|m\d\d|d\d\d|\d{4}s?|spring|summer|fall|autumn|winter)$")
_COORD = re.compile(r"^(lat|lon)\d+$")       # old coordinate tags: lat300 lon950
_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"}


def today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def _photos(db) -> list[dict]:
    return [dict(r) for r in db.db.execute(
        "SELECT id, title, tags, views, faves, comments, date_upload, date_taken, media, lat, lon, accuracy "
        "FROM photos")]


def relative_views(photos: list[dict]) -> dict[str, float]:
    """photo id -> views / median views of photos uploaded the same year (1.0 = typical)."""
    by_year = defaultdict(list)
    for p in photos:
        by_year[_year(p["date_upload"])].append(p["views"] or 0)
    med = {y: max(1.0, _median(v)) for y, v in by_year.items()}
    return {p["id"]: (p["views"] or 0) / med[_year(p["date_upload"])] for p in photos}


def _year(ts) -> int:
    return dt.datetime.fromtimestamp(ts or 0, dt.timezone.utc).year


def _daily_sums(db, since: str, until: str | None = None) -> dict[str, int]:
    """photo id -> views over [since, until]."""
    sql, args = "SELECT photo_id, SUM(views) FROM photo_daily WHERE day >= ?", [since]
    if until:
        sql += " AND day <= ?"
        args.append(until)
    return dict(db.db.execute(sql + " GROUP BY photo_id", args).fetchall())


# ---- photos worth a look ---------------------------------------------------------------
def photo_lists(db, kind: str, days: int, limit: int = 24, min_views: int = 100) -> list[dict]:
    """kind: views | faves | comments (for the period; all-time when days >= 3650),
    engagement (faves per 1,000 views), sleepers (old photos waking up), overlooked
    (well liked, little seen, in no group)."""
    photos = {p["id"]: p for p in _photos(db)}
    since = (today() - dt.timedelta(days=days)).isoformat()
    rows: list[dict] = []
    if kind in ("views", "faves", "comments"):
        if days >= 3650:
            ranked = sorted(photos.values(), key=lambda p: p[kind] or 0, reverse=True)[:limit]
            rows = [{"photo_id": p["id"], "value": p[kind] or 0} for p in ranked]
        else:
            col = {"views": "views", "faves": "favorites", "comments": "comments"}[kind]
            rows = [{"photo_id": r[0], "value": r[1]} for r in db.db.execute(
                f"SELECT photo_id, SUM({col}) n FROM photo_daily WHERE day >= ? GROUP BY photo_id "
                f"HAVING n > 0 ORDER BY n DESC LIMIT ?", (since, limit))]
    elif kind == "engagement":
        ranked = sorted((p for p in photos.values() if (p["views"] or 0) >= min_views),
                        key=lambda p: (p["faves"] or 0) / p["views"], reverse=True)[:limit]
        rows = [{"photo_id": p["id"], "value": round(1000 * (p["faves"] or 0) / p["views"], 1)} for p in ranked]
    elif kind == "sleepers":
        recent = _daily_sums(db, (today() - dt.timedelta(days=30)).isoformat())
        now, out = time.time(), []
        for pid, r in recent.items():
            p = photos.get(pid)
            if not p or not p["date_upload"] or r < 10 or now - p["date_upload"] < 365 * DAY:
                continue
            lifetime = (p["views"] or 0) / max(1.0, (now - p["date_upload"]) / DAY)
            out.append({"photo_id": pid, "value": round((r / 30) / max(lifetime, 0.01), 1), "recent": r})
        rows = sorted(out, key=lambda r: r["value"], reverse=True)[:limit]
    elif kind == "overlooked":
        in_groups = {r[0] for r in db.db.execute("SELECT DISTINCT photo_id FROM group_photos")}
        seen = [p for p in photos.values() if (p["views"] or 0) >= 20]
        typical = _median([p["views"] or 0 for p in seen])
        rates = sorted((p["faves"] or 0) / p["views"] for p in seen)
        high = rates[int(len(rates) * 0.75)] if rates else 0
        cands = [p for p in seen if p["views"] <= typical and p["id"] not in in_groups
                 and (p["faves"] or 0) / p["views"] >= high and (p["faves"] or 0) >= 2]
        ranked = sorted(cands, key=lambda p: (p["faves"] or 0) / p["views"], reverse=True)[:limit]
        rows = [{"photo_id": p["id"], "value": round(1000 * (p["faves"] or 0) / p["views"], 1)} for p in ranked]
    else:
        raise ValueError(kind)
    for r in rows:
        p = photos.get(r["photo_id"]) or {}
        r.update(title=p.get("title"), views=p.get("views"), faves=p.get("faves"), comments=p.get("comments"))
    return rows


# ---- tags ------------------------------------------------------------------------------
def _tag_kinds(db) -> dict[str, set[str]]:
    """Convention-like tags to hide on request: places and cameras (dates by pattern)."""
    from .geo import US_STATES, country_names
    from .standard_tags import REGIONS

    place = {norm_tag(n) for cc in REGIONS for n in country_names(cc)}
    place |= {norm_tag(t) for tags in REGIONS.values() for t in tags}
    words = {"me", "in", "or", "ok", "hi", "oh", "ma", "la", "de", "id", "al", "co", "pa", "mo"}   # also everyday words
    place |= {norm_tag(s) for s in US_STATES} | ({norm_tag(a) for a in US_STATES.values()} - words)
    camera = {norm_tag(w) for (c,) in db.db.execute("SELECT DISTINCT camera FROM cameras WHERE camera <> ''")
              for w in (c or "").split()}
    return {"place": place, "camera": camera}


def tag_kind(tag: str, kinds: dict[str, set[str]]) -> str:
    if _DATED.match(tag) or tag in _MONTHS:
        return "date"
    if tag in kinds["place"] or _COORD.match(tag):
        return "place"
    if tag in kinds["camera"]:
        return "camera"
    return "subject"


COMMON = 0.3   # a tag on more than this share of photos (your name, handles…) is "common"


def tag_table(db, source: str = "all", hide: tuple[str, ...] = ("date", "common"), min_photos: int = 3,
              limit: int = 400, sort: str = "photos") -> dict:
    """Per tag: photos, views, average and median views, faves per 1,000 views, comments,
    lift (median relative views of its photos), and the 14-day trend in daily views.
    source: all | yours (not added by this tool) | added (added here)."""
    photos = _photos(db)
    rel = relative_views(photos)
    added: dict[str, set[str]] = defaultdict(set)      # tag -> photos this tool tagged with it
    for pid, target in db.db.execute("SELECT photo_id, target FROM decisions WHERE action='add_tag' AND status='applied'"):
        added[norm_tag(target)].add(pid)
    end = today()
    recent = _daily_sums(db, (end - dt.timedelta(days=14)).isoformat())
    before = _daily_sums(db, (end - dt.timedelta(days=28)).isoformat(), (end - dt.timedelta(days=15)).isoformat())
    kinds = _tag_kinds(db)
    agg: dict[str, dict] = {}
    for p in photos:
        for t in set((p["tags"] or "").split()):
            if source == "yours" and p["id"] in added.get(t, ()):
                continue
            if source == "added" and p["id"] not in added.get(t, ()):
                continue
            a = agg.setdefault(t, {"tag": t, "photos": 0, "views": 0, "faves": 0, "comments": 0, "_v": [], "_rel": [],
                                   "recent": 0, "before": 0})
            a["photos"] += 1
            a["views"] += p["views"] or 0
            a["faves"] += p["faves"] or 0
            a["comments"] += p["comments"] or 0
            a["_v"].append(p["views"] or 0)
            a["_rel"].append(rel[p["id"]])
            a["recent"] += recent.get(p["id"], 0)
            a["before"] += before.get(p["id"], 0)
    out = []
    for t, a in agg.items():
        kind = "common" if a["photos"] > COMMON * len(photos) and a["photos"] >= 20 else tag_kind(t, kinds)
        if kind in hide or a["photos"] < min_photos:
            continue
        a["kind"] = kind
        a["avg_views"] = round(a["views"] / a["photos"], 1)
        a["median_views"] = _median(a.pop("_v"))
        a["lift"] = round(_median(a.pop("_rel")), 2)
        a["faves_per_1k"] = round(1000 * a["faves"] / a["views"], 1) if a["views"] else 0
        a["trend"] = round((a["recent"] - a["before"]) / max(a["before"], 1), 2) if a["recent"] + a["before"] >= 20 else None
        out.append(a)
    key = {"photos": "photos", "views": "views", "avg_views": "avg_views", "faves_per_1k": "faves_per_1k",
           "comments": "comments", "lift": "lift", "trend": "trend"}.get(sort, "photos")
    out.sort(key=lambda a: (a[key] is not None, a[key] or 0), reverse=True)
    return {"tags": out[:limit], "total": len(out), "days_collected": _days_collected(db)}


def tag_detail(db, tag: str, limit: int = 40) -> dict:
    tag = norm_tag(tag)
    photos = [p for p in _photos(db) if tag in set((p["tags"] or "").split())]
    ids = [p["id"] for p in photos]
    co = Counter(t for p in photos for t in set((p["tags"] or "").split()) if t != tag)
    series = []
    if ids:
        marks = ",".join("?" * len(ids))
        series = [dict(r) for r in db.db.execute(
            f"SELECT day, SUM(views) views, SUM(favorites) faves FROM photo_daily WHERE photo_id IN ({marks}) "
            f"GROUP BY day ORDER BY day", ids)]
    top = sorted(photos, key=lambda p: p["views"] or 0, reverse=True)[:limit]
    return {"tag": tag, "photos": len(photos), "views": sum(p["views"] or 0 for p in photos),
            "faves": sum(p["faves"] or 0 for p in photos), "top": [p["id"] for p in top],
            "often_with": [{"tag": t, "photos": n, "share": round(n / len(photos), 2)} for t, n in co.most_common(15)],
            "series": series}


# ---- places ------------------------------------------------------------------------------
_PLACES: dict = {"key": None, "places": {}}


def _located(db) -> dict[str, dict]:
    """photo id -> nearest place, cached until the photos change (the lookup takes ~1 s)."""
    from .config import DEFAULTS
    from .geo import GeoMatcher

    key = tuple(db.db.execute("SELECT COUNT(*), MAX(last_update) FROM photos WHERE lat IS NOT NULL").fetchone())
    if _PLACES["key"] != key:
        photos = [r for r in db.photos() if r["lat"] is not None and not (r["lat"] == 0 and r["lon"] == 0)]
        _PLACES.update(key=key, places=GeoMatcher(DEFAULTS["geo"], {}).locate(photos))
    return _PLACES["places"]


def places(db, level: str = "country", limit: int = 60) -> list[dict]:
    from .geo import country_names

    located = _located(db)
    agg: dict[tuple, dict] = {}
    for p in _photos(db):
        pl = located.get(p["id"])
        if not pl:
            continue
        acc = p["accuracy"] or 16
        country = country_names(pl["cc"])[0] if pl.get("cc") else "?"
        if level == "country":
            key, label = (pl["cc"],), country
        elif level == "state":
            if acc < 6 or not pl.get("admin1"):
                continue
            key, label = (pl["cc"], pl["admin1"]), f"{pl['admin1']}, {country}"
        else:
            if acc < 11:
                continue
            key, label = (pl["cc"], pl.get("admin1"), pl["name"]), f"{pl['name']}, {pl.get('admin1') or country}"
        a = agg.setdefault(key, {"label": label, "photos": 0, "views": 0, "faves": 0, "comments": 0, "lat": 0.0, "lon": 0.0})
        a["photos"] += 1
        a["views"] += p["views"] or 0
        a["faves"] += p["faves"] or 0
        a["comments"] += p["comments"] or 0
        a["lat"] += p["lat"]
        a["lon"] += p["lon"]
    out = []
    for a in agg.values():
        a["lat"], a["lon"] = round(a["lat"] / a["photos"], 4), round(a["lon"] / a["photos"], 4)
        a["avg_views"] = round(a["views"] / a["photos"], 1)
        out.append(a)
    return sorted(out, key=lambda a: a["views"], reverse=True)[:limit]


# ---- gear and technique -------------------------------------------------------------------
def _num(text, rx=r"([\d.]+)"):
    m = re.search(rx, str(text or ""))
    return float(m.group(1)) if m else None


def _bucket(v, edges: list[tuple[float, str]]) -> str | None:
    if v is None:
        return None
    for top, label in edges:
        if v <= top:
            return label
    return edges[-1][1]


FOCAL = [(19, "< 20 mm"), (28, "20–28 mm"), (40, "29–40 mm"), (60, "41–60 mm"), (105, "61–105 mm"),
         (200, "106–200 mm"), (math.inf, "> 200 mm")]
APERTURE = [(1.8, "f/1.8 or wider"), (2.8, "f/2–2.8"), (4, "f/3.2–4"), (5.6, "f/4.5–5.6"), (8, "f/6.3–8"),
            (math.inf, "f/9 or narrower")]
ISO = [(100, "≤ 100"), (200, "200"), (400, "400"), (800, "800"), (1600, "1600"), (math.inf, "3200+")]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def sun_altitude(lat: float, lon: float, local: dt.datetime) -> float:
    """Approximate solar altitude in degrees. The photo's clock time is local with no time
    zone, so the zone is guessed from the longitude (±1 h, e.g. daylight saving)."""
    utc = local - dt.timedelta(hours=round(lon / 15))
    n = utc.timetuple().tm_yday
    decl = math.radians(-23.44) * math.cos(2 * math.pi / 365 * (n + 10))
    solar = utc.hour + utc.minute / 60 + lon / 15                    # solar time, hours
    ha = math.radians(15 * (solar - 12))
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)))


def light(alt: float) -> str:
    return "night" if alt < -6 else "blue hour" if alt < 0 else "golden hour" if alt < 6 else "daylight"


def gear(db) -> dict:
    photos = {p["id"]: p for p in _photos(db)}
    rel = relative_views(list(photos.values()))
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for pid, camera, lens, exif in db.db.execute("SELECT photo_id, camera, lens, exif FROM cameras"):
        p = photos.get(pid)
        if not p:
            continue
        e = json.loads(exif or "{}")
        v = rel[pid]
        add = lambda dim, key: key is not None and groups[dim][key].append((p["views"] or 0, v))
        add("camera", camera or "(unknown)")
        add("lens", lens or None)
        add("focal", _bucket(_num(e.get("FocalLengthIn35mmFormat")) or _num(e.get("FocalLength")), FOCAL))
        add("aperture", _bucket(_num(e.get("FNumber"), r"f/([\d.]+)") or _num(e.get("FNumber")), APERTURE))
        add("iso", _bucket(_num(e.get("ISO")), ISO))
        taken = e.get("DateTimeOriginal") or ""
        try:
            t = dt.datetime.strptime(taken[:19], "%Y:%m:%d %H:%M:%S")
        except ValueError:
            t = None
        if t:
            add("hour", f"{t.hour:02d}")
            add("weekday", WEEKDAYS[t.weekday()])
            if p["lat"] is not None and not (p["lat"] == 0 and p["lon"] == 0):
                add("light", light(sun_altitude(p["lat"], p["lon"], t)))
    order = {"focal": [l for _, l in FOCAL], "aperture": [l for _, l in APERTURE], "iso": [l for _, l in ISO],
             "hour": [f"{h:02d}" for h in range(24)], "weekday": WEEKDAYS,
             "light": ["night", "blue hour", "golden hour", "daylight"]}
    out = {}
    for dim, by in groups.items():
        rows = [{"label": k, "photos": len(v), "views": sum(x for x, _ in v),
                 "avg_views": round(sum(x for x, _ in v) / len(v), 1), "lift": round(_median([r for _, r in v]), 2)}
                for k, v in by.items()]
        if dim in order:
            rows.sort(key=lambda r: order[dim].index(r["label"]) if r["label"] in order[dim] else 99)
        else:
            rows = sorted(rows, key=lambda r: r["photos"], reverse=True)[:20]
        out[dim] = rows
    return out


# ---- audience ------------------------------------------------------------------------------
def audience(db, limit: int = 25) -> dict:
    favers = [dict(r) for r in db.db.execute(
        "SELECT user_id, MAX(COALESCE(NULLIF(real_name, ''), username)) name, COUNT(*) faves, MIN(date_faved) first, "
        "MAX(date_faved) last FROM faves GROUP BY user_id ORDER BY faves DESC LIMIT ?", (limit,))]
    commenters = [dict(r) for r in db.db.execute(
        "SELECT author, MAX(COALESCE(NULLIF(real_name, ''), author_name)) name, COUNT(*) comments, "
        "MAX(date_created) last FROM comments GROUP BY author ORDER BY comments DESC LIMIT ?", (limit,))]
    first = dict(db.db.execute("SELECT user_id, MIN(date_faved) FROM faves GROUP BY user_id").fetchall())
    months: dict[str, dict] = defaultdict(lambda: {"new": set(), "returning": set()})
    for uid, ts in db.db.execute("SELECT user_id, date_faved FROM faves WHERE date_faved > 0"):
        m = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m")
        first_m = dt.datetime.fromtimestamp(first[uid], dt.timezone.utc).strftime("%Y-%m")
        months[m]["new" if m == first_m else "returning"].add(uid)
    monthly = [{"month": m, "new": len(v["new"]), "returning": len(v["returning"] - v["new"])}
               for m, v in sorted(months.items())]
    timing = Counter()
    for faved, uploaded in db.db.execute(
            "SELECT f.date_faved, p.date_upload FROM faves f JOIN photos p ON p.id = f.photo_id "
            "WHERE f.date_faved > 0 AND p.date_upload > 0"):
        age = (faved - uploaded) / DAY
        timing[_bucket(age, [(1, "first day"), (7, "first week"), (30, "first month"), (365, "first year"),
                             (math.inf, "after a year")])] += 1
    order = ["first day", "first week", "first month", "first year", "after a year"]
    week = [0] * 168     # faves by UTC hour of the week (Mon 00:00 = 0); the page shifts it to your time zone
    since = time.time() - 365 * DAY
    for (ts,) in db.db.execute("SELECT date_faved FROM faves WHERE date_faved > ?", (since,)):
        d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        week[d.weekday() * 24 + d.hour] += 1
    return {"favers": favers, "commenters": commenters, "monthly": monthly,
            "timing": [{"label": k, "faves": timing.get(k, 0)} for k in order], "week_utc": week,
            "people": len(first)}


# ---- albums and groups ------------------------------------------------------------------------
def albums_and_groups(db, limit: int = 40) -> dict:
    photos = {p["id"]: p for p in _photos(db)}
    rel = relative_views(list(photos.values()))

    def summary(ids: list[str]) -> dict:
        ps = [photos[i] for i in ids if i in photos]
        views = sum(p["views"] or 0 for p in ps)
        faves = sum(p["faves"] or 0 for p in ps)
        return {"photos": len(ps), "views": views, "avg_views": round(views / len(ps), 1) if ps else 0,
                "faves_per_1k": round(1000 * faves / views, 1) if views else 0,
                "lift": round(_median([rel[p["id"]] for p in ps]), 2)}

    members = defaultdict(list)
    for aid, pid in db.db.execute("SELECT album_id, photo_id FROM memberships"):
        members[aid].append(pid)
    albums = [{"id": a["id"], "title": a["title"], **summary(members[a["id"]])}
              for a in db.db.execute("SELECT id, title FROM albums")]
    albums = sorted((a for a in albums if a["photos"]), key=lambda a: a["views"], reverse=True)[:limit]
    in_group = defaultdict(list)
    for gid, pid in db.db.execute("SELECT group_id, photo_id FROM group_photos"):
        in_group[gid].append(pid)
    groups = [{"id": g["id"], "name": g["name"], "members": g["members"], "path_alias": g["path_alias"],
               **summary(in_group[g["id"]])} for g in db.db.execute("SELECT id, name, members, path_alias FROM flickr_groups")]
    groups = sorted((g for g in groups if g["photos"] >= 3), key=lambda g: (g["lift"], g["photos"]), reverse=True)[:limit]
    grouped = {pid for ids in in_group.values() for pid in ids}
    return {"albums": albums, "groups": groups,
            "group_lift": {"in_groups": summary(sorted(grouped)),
                           "not_in_groups": summary([i for i in photos if i not in grouped])}}


# ---- did it help? ------------------------------------------------------------------------------
IMPACT = {"set_title": "Titles", "set_description": "Descriptions", "add_tag": "Tags", "add_to_album": "Albums"}


def _days_collected(db) -> int:
    return db.db.execute("SELECT COUNT(DISTINCT day) FROM photo_daily").fetchone()[0]


def impact(db, window: int = 14) -> dict:
    """Average daily views of photos in the `window` days before vs after this tool changed
    them, next to photos it didn't change in that way over the same days (the control), so
    a busy or quiet week doesn't count as an effect. Days are those collected so far."""
    days = [r[0] for r in db.db.execute("SELECT DISTINCT day FROM photo_daily ORDER BY day")]
    if not days:
        return {"kinds": [], "days_collected": 0}
    first, last = dt.date.fromisoformat(days[0]), dt.date.fromisoformat(days[-1])
    daily = defaultdict(dict)                                            # photo -> day -> views
    for pid, day, v in db.db.execute("SELECT photo_id, day, views FROM photo_daily"):
        daily[pid][day] = v or 0
    all_ids = [r[0] for r in db.db.execute("SELECT id FROM photos")]
    changed: dict[str, dict[str, dt.date]] = defaultdict(dict)        # action -> photo -> first change day
    for action, pid, at in db.db.execute(
            "SELECT action, photo_id, MIN(at) FROM decisions WHERE status IN ('applied', 'edited') GROUP BY action, photo_id"):
        if action in IMPACT and at:
            changed[action][pid] = dt.datetime.fromtimestamp(at, dt.timezone.utc).date()

    def mean_daily(ids, start: dt.date, end: dt.date) -> float:
        n = (end - start).days + 1
        if not ids or n <= 0:
            return 0.0
        s, e = start.isoformat(), end.isoformat()
        return sum(v for pid in ids for d, v in daily.get(pid, {}).items() if s <= d <= e) / (len(ids) * n)

    kinds = []
    for action, label in IMPACT.items():
        photos = changed.get(action, {})
        cohorts = defaultdict(list)
        for pid, day in photos.items():
            cohorts[day].append(pid)
        untouched = [pid for pid in all_ids if pid not in photos]
        t_before = t_after = c_before = c_after = 0.0
        counted, after_days = 0, []
        for day0, ids in sorted(cohorts.items()):
            b0, b1 = max(first, day0 - dt.timedelta(days=window)), day0 - dt.timedelta(days=1)
            a0, a1 = day0 + dt.timedelta(days=1), min(last, day0 + dt.timedelta(days=window))
            if (b1 - b0).days + 1 < 3 or (a1 - a0).days + 1 < 1:
                continue
            w = len(ids)
            t_before += w * mean_daily(ids, b0, b1)
            t_after += w * mean_daily(ids, a0, a1)
            c_before += w * mean_daily(untouched, b0, b1)
            c_after += w * mean_daily(untouched, a0, a1)
            counted += w
            after_days.append((a1 - a0).days + 1)
        row = {"kind": label, "photos": len(photos), "counted": counted,
               "after_days": min(after_days) if after_days else 0, "control": len(untouched)}
        if counted:
            tb, ta, cb, ca = (x / counted for x in (t_before, t_after, c_before, c_after))
            change = (ta - tb) / tb if tb else None
            control = (ca - cb) / cb if cb else None
            row.update(before=round(tb, 3), after=round(ta, 3), change=change and round(change, 3),
                       control_change=control and round(control, 3),
                       effect=round(change - control, 3) if change is not None and control is not None and len(untouched) >= 30 else None)
        kinds.append(row)
    return {"kinds": kinds, "window": window, "days_collected": len(days), "first_day": days[0], "last_day": days[-1]}
