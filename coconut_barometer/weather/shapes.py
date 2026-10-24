"""Polygons for the Weather map, compactly (#70): radar's echoes (radar.py encode()) and NWS alerts (hazards.py).

A poly is [west, south, east, north, text]: its bounds in whole 1/q degrees, and its JSON text `[w,s,e,n,ring,ring...]`
(the outer ring first, then its holes), each ring flat `[x0,y0,dx1,dy1,...]`, every point after the first the step
from the one before. The text is made once; a request picks the polys in view (in_view()) and joins their texts.
radar_layer.js decodePoly() reads one."""
from __future__ import annotations

import json

import numpy as np


def ring(pts, q: int) -> np.ndarray | None:
    """A ring's (x, y) points in degrees -> whole 1/q degrees (int64, n × 2), with the points rounding repeated and
    the closing point (the drawing closes it) dropped; None if fewer than 3 are left."""
    qi = np.round(np.asarray(pts, dtype=np.float64) * q).astype(np.int64)
    if len(qi) < 3:
        return None
    step = np.diff(qi, axis=0)
    qi = np.vstack([qi[:1], qi[1:][np.any(step != 0, axis=1)]])
    if len(qi) > 1 and (qi[0] == qi[-1]).all():
        qi = qi[:-1]
    return qi if len(qi) >= 3 else None


def poly(rings: list[np.ndarray]) -> list:
    """ring()s, the outer one first -> [w, s, e, n, text]."""
    (w, s), (e, n) = rings[0].min(axis=0), rings[0].max(axis=0)
    bounds = [int(w), int(s), int(e), int(n)]
    flat = [np.concatenate([r[:1], np.diff(r, axis=0)]).ravel().tolist() for r in rings]
    return [*bounds, json.dumps([*bounds, *flat], separators=(",", ":"))]


def in_view(p: list, bbox: tuple | None, q: int) -> bool:
    """Whether poly p touches bbox (west, south, east, north degrees); every poly with no bbox."""
    if not bbox:
        return True
    w, s, e, n = (x * q for x in bbox)
    return p[0] <= e and p[2] >= w and p[1] <= n and p[3] >= s
