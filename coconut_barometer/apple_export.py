"""Apple's data export (privacy.apple.com) reduced to what the Music page uses, on your own machine, so the IP
addresses, locations and account numbers beside it never reach the server. `coconut-barometer music-export` writes
the small file; Settings → Music uploads it (store/music.py reads it).

Apple Music Play Activity.csv has a row per event with exact times and how long was heard, but no artist.
Apple Music - Play History Daily Tracks.csv has "Artist - Song" and the track's reference for each day and hour, so a
play takes its artist from the same day and hour if it can (match "hour"), the same day ("day"), or else whichever
artist that title has most often ("title"). No match leaves the artist empty ("none").

A play is a PLAY_END of catalog audio that either finished or lasted MIN_LISTENED_MS; the rest are starts, lyric views
and skips."""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
from collections import Counter, defaultdict

MIN_LISTENED_MS = 30000
FINISHED = "NATURAL_END_OF_TRACK"
COLUMNS = ("started", "track_id", "song", "artist", "album", "duration_ms", "listened_ms", "end_reason", "utc_offset",
           "container_type", "feature", "match")


def _rows(path: str):
    csv.field_size_limit(10 ** 9)
    with open(path, newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def track_key(reference: str, identifier: str = "") -> str:
    """Apple's catalog number when the export has one."""
    return next((x for x in (reference, identifier) if x and x.isdigit()), "")


class Tracks:
    """Daily Tracks, to look a play up in."""

    def __init__(self, rows):
        self.day: dict = defaultdict(list)
        self.title: dict = defaultdict(Counter)
        self.ref: dict = {}
        for r in rows:
            artist, sep, song = (r.get("Track Description") or "").partition(" - ")
            if not sep:
                continue
            key, song_key = track_key(r.get("Track Reference"), r.get("Track Identifier")), song.strip().casefold()
            hours = {_int(h.strip(), -1) for h in (r.get("Hours") or "").split(",")}
            self.day[(r.get("Date Played"), song_key)].append((hours, artist.strip(), key))
            self.title[song_key][artist.strip()] += 1
            if key:
                self.ref.setdefault((song_key, artist.strip()), key)

    def find(self, song: str, when: dt.datetime, offset: int) -> tuple[str, str, str]:
        """(artist, track key, how it was matched). Apple's days and hours may be UTC or local: UTC is tried first."""
        key = song.strip().casefold()
        moments = [when, when + dt.timedelta(seconds=offset)]
        for m in moments:
            for hours, artist, ref in self.day.get((m.strftime("%Y%m%d"), key), ()):
                if m.hour in hours:
                    return artist, ref, "hour"
        for m in moments:
            for _, artist, ref in self.day.get((m.strftime("%Y%m%d"), key), ())[:1]:
                return artist, ref, "day"
        artists = self.title.get(key)
        if artists:
            artist = artists.most_common(1)[0][0]
            return artist, self.ref.get((key, artist), ""), "title"
        return "", "", "none"


def slim(activity: str, daily: str, out: str, log=print) -> Counter:
    """Writes the small file (gzipped CSV), and returns how many plays matched each way."""
    tracks = Tracks(_rows(daily))
    matched: Counter = Counter()
    seen: set = set()
    with gzip.open(out, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in _rows(activity):
            song, start = r.get("Song Name"), r.get("Event Start Timestamp")
            listened = _int(r.get("Play Duration Milliseconds"))
            if (r.get("Event Type") != "PLAY_END" or not song or not start or r.get("Media Type") != "AUDIO"
                    or r.get("Item Type") != "ITUNES_STORE_CONTENT"
                    or (listened < MIN_LISTENED_MS and r.get("End Reason Type") != FINISHED) or (song, start) in seen):
                continue
            seen.add((song, start))
            when = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
            offset = _int(r.get("UTC Offset In Seconds"))
            artist, ref, how = tracks.find(song, when, offset)
            matched[how] += 1
            track_id = ref or "x-" + hashlib.sha1(f"{song}\0{artist}".encode()).hexdigest()[:16]
            w.writerow((int(when.timestamp()), track_id, song, artist, r.get("Album Name") or "",
                        _int(r.get("Media Duration In Milliseconds")), listened, r.get("End Reason Type") or "", offset,
                        r.get("Container Type") or "", r.get("Feature Name") or "", how))
    log(f"{sum(matched.values()):,} plays written to {out}: artist by hour {matched['hour']:,}, by day {matched['day']:,}, "
        f"by title {matched['title']:,}, none {matched['none']:,}")
    return matched
