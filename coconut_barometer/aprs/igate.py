"""The igate in Settings → APRS: which packets APRS-IS credits it with, where it is, and how far it hears.

APRS-IS marks every packet an igate gates from RF with a q construct followed by the igate's callsign
(…,qAR,WA5DTR-15:…), and the `e/` filter asks the server for exactly those. A packet heard direct has no
used hop before the q construct; a digipeated one has its last used hop marked with "*" (WIDE1*,
KJ4TFD-3*), so distance counts direct packets only unless asked otherwise.

Effective range comes from a Weibull fit to the distances' cumulative share: ln(-ln(1 - F)) against ln(r)
is a straight line for a Weibull, so a least-squares line gives the shape k and the scale λ, and the
distance holding a share q of the packets is λ·(-ln(1 - q))^(1/k). Effective range is the fitted 90%.
The digis that relayed a packet (#117) are read from its path before the q construct: everything up to the last
hop marked "*" was used. A callsign there is a digi (one tracing a WIDEn-N puts its call before the alias it used);
a used generic alias with no callsign before it is a digi that didn't say who it was.
The long-distance log (#116) leaves out what can't be believed: a distance past PLAUSIBLE_KM unless the station
sent CONFIRM or more that far, and a fixed station's (home, weather, digi, igate) position more than MOVED_KM from
where it now says it is (an old position, from before it moved).
By direction (#115), the same fit for each sector of bearing from the igate, sector 0 centred on north.

What kind of station a sender is comes from its symbol (table + code): the code alone for the ones any table
draws the same (# a digi, & an igate, _ weather), the primary table's vehicles and houses, and the alternate
table's few vehicles. Overlays (a letter or digit in place of "\\") count as the alternate table."""
from __future__ import annotations

import math
import re

GATED = {"qAR", "qAr", "qAO", "qAo"}         # gated from RF (verified or not); qAC/qAS and the like came over TCP
LOCATION = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
ALIAS = re.compile(r"^(WIDE\d?|RELAY|TRACE\d?|TEMP\d?|[A-Z]{2}\d)(-\d{1,2})?$")   # generic, or a state's (TX2-2)
DIGI = re.compile(r"^(?=.*\d)[A-Z0-9]{3,6}(-\d{1,2})?$")
UNKNOWN = "?"
GRID = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$", re.I)
EFFECTIVE = 0.9
MIN_FIT = 30            # packets a sector (or a day, an hour) needs for a fit of its own
MIN_SECTOR = MIN_FIT
PLAUSIBLE_KM, CONFIRM, MOVED_KM = 400, 3, 50
HIGHEST_M = 60_000      # an altitude above this is a typo or a broken encoder, not a balloon

# Station types, in the order the page lists them.
TYPES = ("mobile", "home", "weather", "digi", "igate", "balloon", "object", "other")
MOBILE = set(">kuvj<bRUafPsYC'^X=[")       # primary table: cars, trucks, bikes, boats, aircraft, a person…
MOBILE_ALT = set(">kuvs^")                   # alternate table: the vehicles (with an overlay)
HOME = set("-y")                             # primary: a house, a house with a yagi
BY_CODE = {"#": "digi", "&": "igate", "_": "weather", "O": "balloon"}   # O: a balloon, or (alternate) a rocket


def details(packet: dict) -> dict:
    """What aprs_igate keeps beyond where and how: altitude (m, if believable), symbol, comment and tocall.
    Mic-E hides its tocall in the destination's latitude, so it has none."""
    alt = packet.get("altitude")
    return {"altitude": alt if alt is not None and -500 < alt < HIGHEST_M else None,
            "symbol": ((packet.get("symbol_table") or "") + (packet.get("symbol") or "")) or None,
            "comment": ((packet.get("comment") or packet.get("status") or "").strip()[:200]) or None,
            "tocall": None if packet.get("format") == "mic-e" else (packet.get("to") or header_tocall(packet.get("raw") or "")) or None}


def header_tocall(line: str) -> str:
    head = line.split(":", 1)[0]
    return head.split(">", 1)[1].split(",")[0] if ">" in head else ""


def station_type(symbol: str | None, weather: bool = False, objects_only: bool = False) -> str:
    """One of TYPES, from a station's newest symbol (its objects' and items' aside). `weather`: it sent a
    positionless weather report; `objects_only`: all it sent were objects and items."""
    if objects_only:
        return "object"
    if not symbol or len(symbol) != 2:
        return "weather" if weather else "other"
    table, code = symbol
    if code in BY_CODE:
        return BY_CODE[code]
    if code in (MOBILE if table == "/" else MOBILE_ALT):
        return "mobile"
    if (code in HOME and table == "/") or code == "-":      # an alternate-table house carries an overlay
        return "home"
    return "weather" if weather else "other"


def digi_hops(path: list[str]) -> list[str]:
    """The digis that relayed a packet, in order, from its path (q construct and igate included or not); UNKNOWN for
    a used alias no callsign claimed. [] when heard direct."""
    before = []
    for hop in path:
        if hop.startswith("q"):
            break
        before.append(hop)
    used = max((i for i, h in enumerate(before) if h.endswith("*")), default=-1)
    out, claimed = [], False
    for hop in before[:used + 1]:
        h = hop.rstrip("*").upper()
        if ALIAS.match(h):
            if not claimed:
                out.append(UNKNOWN)
            claimed = False
        elif DIGI.match(h):
            out.append(h)
            claimed = True
    return out


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The initial great-circle bearing from 1 to 2, degrees clockwise from north (0 ≤ b < 360)."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def sector_of(deg: float, sectors: int) -> int:
    """Which of `sectors` equal sectors a bearing falls in, sector 0 centred on north."""
    width = 360 / sectors
    return int(((deg + width / 2) % 360) // width)


def home(cfg: dict, store, gate: str) -> dict | None:
    """Where the igate is, for distances: the location in Settings → APRS, else its own last beacon."""
    try:
        fixed = parse_location(cfg["aprs"]["igate_location"])
    except ValueError:
        fixed = None
    beacon = store.igate_placed(gate)
    return ({"lat": fixed[0], "lon": fixed[1], "source": "setting"} if fixed
            else {"lat": beacon["lat"], "lon": beacon["lon"], "source": "beacon", "ts": beacon["ts"]} if beacon else None)


def gated_by(path: list[str]) -> tuple[str, bool] | None:
    """(the igate the path credits, whether it was heard direct), or None if no igate gated it from RF."""
    for i, hop in enumerate(path):
        if hop in GATED and i + 1 < len(path):
            return path[i + 1].upper(), not any(h.endswith("*") for h in path[:i])
    return None


def header_path(line: str) -> list[str]:
    """The path of a packet aprslib couldn't read: SRC>DEST,PATH…:info."""
    head = line.split(":", 1)[0]
    return head.split(">", 1)[1].split(",")[1:] if ">" in head else []


def grid_center(grid: str) -> tuple[float, float]:
    """The middle of a Maidenhead square (EM10) or subsquare (EM10dg), as (lat, lon)."""
    g = grid.upper()
    lon = (ord(g[0]) - 65) * 20 + int(g[2]) * 2 - 180
    lat = (ord(g[1]) - 65) * 10 + int(g[3]) - 90
    if len(g) == 6:
        return lat + (ord(g[5]) - 65) / 24 + 1 / 48, lon + (ord(g[4]) - 65) / 12 + 1 / 24
    return lat + 0.5, lon + 1


def parse_location(text: str) -> tuple[float, float] | None:
    """Settings → APRS → Igate location: "lat, lon" or a grid locator. None when empty; ValueError when unreadable."""
    text = (text or "").strip()
    if not text:
        return None
    if GRID.match(text):
        return grid_center(text)
    m = LOCATION.match(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    raise ValueError(text)


def bin_width(far_km: float, bins: int = 25) -> float:
    """A round width (1, 2 or 5 × 10^n km, at least 0.5) that splits 0..far_km into about `bins` bins."""
    raw = max(far_km, 1) / bins
    step = 10 ** math.floor(math.log10(raw))
    return max(0.5, next(m * step for m in (1, 2, 5, 10) if m * step >= raw))


def weibull_fit(width: float, counts: list[int], total: int) -> dict | None:
    """Fit to a histogram: counts[i] in [i·width, (i+1)·width), out of `total` (which includes any beyond the
    last bin). None with fewer than three usable points."""
    xs, ys, seen = [], [], 0
    for i, n in enumerate(counts):
        seen += n
        f = seen / total if total else 0
        if n and 0 < f <= 0.98:          # the last few percent are the noisiest: a stray far packet swings them
            xs.append(math.log((i + 1) * width))
            ys.append(math.log(-math.log(1 - f)))
    if len(xs) < 3:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        return None
    k = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    if k <= 0:
        return None
    scale = math.exp(mx - my / k)
    ss = sum((y - my) ** 2 for y in ys)
    resid = sum((y - (k * (x - math.log(scale)))) ** 2 for x, y in zip(xs, ys))
    cdf = lambda r: 1 - math.exp(-((r / scale) ** k))
    at = lambda q: scale * (-math.log(1 - q)) ** (1 / k)
    return {"k": round(k, 3), "scale_km": round(scale, 2), "r2": round(1 - resid / ss, 3) if ss else 1.0,
            "r50_km": round(at(0.5), 2), "effective_km": round(at(EFFECTIVE), 2), "r95_km": round(at(0.95), 2),
            "curve": [round(total * (cdf((i + 1) * width) - cdf(i * width)), 2) for i in range(len(counts))]}
