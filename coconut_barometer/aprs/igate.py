"""The igate in Settings → APRS: which packets APRS-IS credits it with, where it is, and how far it hears.

APRS-IS marks every packet an igate gates from RF with a q construct followed by the igate's callsign
(…,qAR,WA5DTR-15:…), and the `e/` filter asks the server for exactly those. A packet heard direct has no
used hop before the q construct; a digipeated one has its last used hop marked with "*" (WIDE1*,
KJ4TFD-3*), so distance counts direct packets only unless asked otherwise.

Effective range comes from a Weibull fit to the distances' cumulative share: ln(-ln(1 - F)) against ln(r)
is a straight line for a Weibull, so a least-squares line gives the shape k and the scale λ, and the
distance holding a share q of the packets is λ·(-ln(1 - q))^(1/k). Effective range is the fitted 90%."""
from __future__ import annotations

import math
import re

GATED = {"qAR", "qAr", "qAO", "qAo"}         # gated from RF (verified or not); qAC/qAS and the like came over TCP
LOCATION = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
GRID = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$", re.I)
EFFECTIVE = 0.9


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
