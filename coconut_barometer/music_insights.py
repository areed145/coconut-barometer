"""What the Music page builds from plays beyond the counts: cover art for the top lists, the listening strip and
monthly palettes from the covers' colors, a trip's soundtrack, and what's played more on rainy days.

Colors are Apple's bgColor for each cover. Plain averages of many covers turn to brown, so a blend counts each by how
colorful it is, and the strip stays recognisably the music of that week."""
from __future__ import annotations

import colorsys
import datetime as dt
import json
from collections import Counter, defaultdict

RAIN_MM = 1.0                    # a rainy day, at the wettest station
MIN_RAINY_PLAYS = 30             # fewer than this says nothing
MIN_LABEL_PLAYS = 6
WEEKLY_AFTER_DAYS = 370          # longer ranges are drawn a week to a band


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def blend(counts: Counter) -> str | None:
    """counts: hex color -> plays."""
    total = [0.0, 0.0, 0.0]
    weight = 0.0
    for color, n in counts.items():
        r, g, b = _rgb(color)
        saturation = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[1]
        w = n * (saturation ** 2 + 0.03)
        weight += w
        total = [t + c * w for t, c in zip(total, (r, g, b))]
    return "%02x%02x%02x" % tuple(round(t / weight) for t in total) if weight else None


def top(rows: list[dict], key, limit: int) -> list[dict]:
    """The most played by key(row): plays, and the cover and colors of its most played track that has one."""
    plays: Counter = Counter()
    tracks: dict = defaultdict(Counter)
    for r in rows:
        k = key(r)
        if k:
            plays[k] += 1
            if r["has_art"]:
                tracks[k][r["track_id"]] += 1
    by_id = {r["track_id"]: r for r in rows}
    out = []
    for label, n in plays.most_common(limit):
        best = by_id.get(tracks[label].most_common(1)[0][0]) if tracks[label] else None
        out.append({"label": label, "plays": n, "art": best and best["track_id"], "bg": best and best["bg_color"],
                    "text": best and best["text_color"]})
    return out


def album_key(r: dict) -> str:
    return f"{r['album']} · {r['artist']}" if r["album"] else ""


def track_key(r: dict) -> str:
    return f"{r['name']} · {r['artist']}"


def wall(rows: list[dict], limit: int = 40) -> list[dict]:
    """The top albums with covers, and each one's most played songs."""
    songs: dict = defaultdict(Counter)
    for r in rows:
        if r["album"]:
            songs[album_key(r)][r["name"]] += 1
    tiles = [t for t in top(rows, album_key, limit * 2) if t["art"]][:limit]
    return [{**t, "songs": [{"name": n, "plays": c} for n, c in songs[t["label"]].most_common(8)]} for t in tiles]


def strip(rows: list[dict], zone, days: int) -> list[dict]:
    """A band of color per day, or per week over long ranges, oldest first."""
    weekly = days > WEEKLY_AFTER_DAYS
    bands: dict = defaultdict(Counter)
    for r in rows:
        if r["bg_color"]:
            d = dt.datetime.fromtimestamp(r["played_at"], zone).date()
            bands[d - dt.timedelta(d.weekday()) if weekly else d][r["bg_color"]] += 1
    return [{"start": d.isoformat(), "color": blend(c), "plays": sum(c.values())} for d, c in sorted(bands.items())]


def _families(counts: Counter, limit: int) -> list[dict]:
    """The main colors: near ones (same 3 bits each of red, green and blue) count as one."""
    groups: dict = defaultdict(Counter)
    for color, n in counts.items():
        groups[tuple(c >> 5 for c in _rgb(color))][color] += n
    total = sum(counts.values())
    ranked = sorted(groups.values(), key=lambda c: -sum(c.values()))[:limit]
    return [{"color": c.most_common(1)[0][0], "share": round(sum(c.values()) / total, 3)} for c in ranked]


def palettes(rows: list[dict], zone, limit: int = 12) -> list[dict]:
    """The latest months: their main colors and top album."""
    months: dict = defaultdict(list)
    for r in rows:
        months[dt.datetime.fromtimestamp(r["played_at"], zone).strftime("%Y-%m")].append(r)
    out = []
    for month in sorted(months, reverse=True)[:limit]:
        counts = Counter(r["bg_color"] for r in months[month] if r["bg_color"])
        if counts:
            out.append({"month": month, "colors": _families(counts, 5), "album": next(iter(top(months[month], album_key, 1)), None)})
    return out


def soundtrack(rows: list[dict]) -> dict:
    return {"plays": len(rows), "minutes": round(sum(r["ms"] for r in rows) / 60000), "albums": wall(rows, 8),
            "tracks": top(rows, track_key, 5)}


def _lift(rainy: Counter, dry: Counter) -> tuple[list[dict], list[dict]]:
    """What's over-represented each way, as how many times more likely."""
    n_rainy, n_dry = sum(rainy.values()), sum(dry.values())
    if not n_rainy or not n_dry:
        return [], []
    scored = []
    for label in set(rainy) | set(dry):
        if rainy[label] + dry[label] >= MIN_LABEL_PLAYS:
            wet = (rainy[label] + 1) / n_rainy
            other = (dry[label] + 1) / n_dry
            scored.append({"label": label, "rainy": rainy[label], "dry": dry[label], "lift": round(wet / other, 2)})
    scored.sort(key=lambda s: -s["lift"])
    return ([s for s in scored if s["lift"] > 1][:6],
            sorted(({**s, "lift": round(1 / s["lift"], 2)} for s in scored if s["lift"] < 1), key=lambda s: -s["lift"])[:6])


def weather(rows: list[dict], rain: list[tuple], first: int | None, last: int | None, zone) -> dict:
    """Rainy days against dry ones, over the days the stations have covered. rain: (device, ts, mm), wet minutes only."""
    if first is None or last is None:
        return {"enough": False, "rainy_days": 0, "plays": 0}
    per_device: Counter = Counter()
    for device, ts, mm in rain:
        per_device[(device, dt.datetime.fromtimestamp(ts, zone).date())] += mm or 0
    wet_days = {d for (_, d), mm in per_device.items() if mm >= RAIN_MM}
    artists = (Counter(), Counter())
    genres = (Counter(), Counter())
    rainy_plays = 0
    for r in rows:
        if first <= r["played_at"] <= last:
            wet = dt.datetime.fromtimestamp(r["played_at"], zone).date() in wet_days
            rainy_plays += wet
            if r["artist"]:
                artists[0 if wet else 1][r["artist"]] += 1
            for g in json.loads(r["genres"] or "[]"):
                genres[0 if wet else 1][g] += 1
    wet_artists, dry_artists = _lift(*artists)
    wet_genres, dry_genres = _lift(*genres)
    return {"enough": rainy_plays >= MIN_RAINY_PLAYS, "rainy_days": len(wet_days), "plays": rainy_plays,
            "rainy": wet_artists, "dry": dry_artists, "rainy_genres": wet_genres, "dry_genres": dry_genres}
