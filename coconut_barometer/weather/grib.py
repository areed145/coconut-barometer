"""Just enough GRIB2 for NOAA's grids, read with numpy (no eccodes): the radar's MRMS and the surface analysis's RTMA.

    grids    template 3.0 (regular lat/lon: MRMS) and 3.30 (Lambert conformal: RTMA, HRRR)
    packing  template 5.0 (simple: RTMA, NOMADS's grib filter) and 5.41 (PNG: MRMS; Pillow decodes the PNG)

A message's data is (R + X·2^E) / 10^D. Signed numbers in GRIB2 are sign and magnitude, not two's complement: the top
bit is the sign (0x8004 is -4), so they're never read with struct's signed codes. A file can hold several messages
back to back (a Range request across neighbouring fields of an RTMA file: messages())."""
from __future__ import annotations

import calendar
import struct

import numpy as np

# The Earth's shape (section 3, octet 15) -> the sphere's radius in m; 1 is a radius given in the section itself.
EARTH = {0: 6367470.0, 6: 6371229.0, 8: 6371200.0}
CHUNK = 1 << 18          # values unpacked at a time (unpack_bits())


def signed(x: int, bits: int) -> int:
    """A sign-and-magnitude integer of `bits` bits as a Python int."""
    top = 1 << (bits - 1)
    return -(x & (top - 1)) if x & top else x


def _u(s: bytes, at: int, n: int = 4) -> int:
    return int.from_bytes(s[at:at + n], "big")


def _s(s: bytes, at: int, n: int = 4) -> int:
    return signed(_u(s, at, n), 8 * n)


def messages(data: bytes) -> list[dict]:
    """Each GRIB2 message in `data`, in order (message())."""
    out, at = [], 0
    while at + 16 <= len(data) and data[at:at + 4] == b"GRIB":
        total = struct.unpack(">Q", data[at + 8:at + 16])[0]
        out.append(message(data[at:at + total]))
        at += total
    if not out:
        raise ValueError("not a GRIB2 file")
    return out


def message(grib: bytes) -> dict:
    """One GRIB2 message -> {t, grid, field, pack, data}:
        t      the reference time, epoch seconds
        grid   {template, ...}: 0: ni, nj, la1, lo1, di, dj (degrees; la1, lo1 the first point)
                                30: nx, ny, la1, lo1, lad, lov, latin1, latin2, dx, dy (m), radius (m), grid_relative
                                    (u/v along the grid's axes, not east and north), south_first (rows go north)
        field  (discipline, category, number): (0, 0, 0) is temperature, (0, 2, 2) and (0, 2, 3) wind u and v
        pack   {template, ref, e, d, bits, n}: value = (ref + X·e) / d
        data   section 7's bytes (for 5.41, a PNG)"""
    if grib[:4] != b"GRIB" or grib[7] != 2:
        raise ValueError("not a GRIB2 file")
    out, discipline, i = {}, grib[6], 16
    while grib[i:i + 4] != b"7777":
        n, sec = _u(grib, i), grib[i + 4]
        s = grib[i:i + n]
        if sec == 1:
            y, mo, d, h, mi, se = struct.unpack(">H5B", s[12:19])
            out["t"] = calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0))
        elif sec == 3:
            out["grid"] = _grid(s)
        elif sec == 4:
            out["field"] = (discipline, s[9], s[10])
        elif sec == 5:
            out["pack"] = _pack(s)
        elif sec == 6:
            if s[5] != 255:
                raise ValueError("a bitmap isn't read here")
        elif sec == 7:
            out["data"] = s[5:]
        i += n
    return out


def head(prefix: bytes) -> dict:
    """The start of one simple-packed GRIB2 message (its sections up to 7's header: a few hundred bytes) ->
    {t, grid, field, pack, data_at}: data_at is where section 7's values start, counted from the message's start, so
    single values can be read with a Range request (point_bytes()) instead of the whole field (station QC, #105)."""
    if prefix[:4] != b"GRIB" or prefix[7] != 2:
        raise ValueError("not a GRIB2 message")
    out, discipline, i = {}, prefix[6], 16
    while True:
        if i + 5 > len(prefix):
            raise ValueError("the prefix ends before section 7")
        n, sec = _u(prefix, i), prefix[i + 4]
        if sec == 7:
            out["data_at"] = i + 5
            break
        s = prefix[i:i + n]
        if len(s) < n:
            raise ValueError("the prefix ends before section 7")
        if sec == 1:
            y, mo, d, h, mi, se = struct.unpack(">H5B", s[12:19])
            out["t"] = calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0))
        elif sec == 3:
            out["grid"] = _grid(s)
        elif sec == 4:
            out["field"] = (discipline, s[9], s[10])
        elif sec == 5:
            out["pack"] = _pack(s)
        elif sec == 6 and s[5] != 255:
            raise ValueError("a bitmap isn't read here")
        i += n
    if out["pack"]["template"] != 0:
        raise ValueError("single values are read from simple packing (5.0) only")
    return out


def point_bytes(h: dict, first: int, last: int) -> tuple[int, int]:
    """The byte range (from the message's start, both ends included) holding values `first` to `last` (indexes in
    the file's order) of a head()."""
    bits = h["pack"]["bits"]
    return h["data_at"] + first * bits // 8, h["data_at"] + ((last + 1) * bits + 7) // 8 - 1


def point_values(h: dict, data: bytes, start: int, indexes) -> np.ndarray:
    """Values at `indexes` from `data`, the bytes of a head()'s message from offset `start` (point_bytes()'s)."""
    p, bits = h["pack"], h["pack"]["bits"]
    raw = bytes(data) + b"\0\0\0\0"
    out = []
    for k in indexes:
        at = k * bits - (start - h["data_at"]) * 8
        if bits == 0:
            x = 0
        else:
            word = int.from_bytes(raw[at >> 3:(at >> 3) + 4], "big")
            x = (word >> (32 - bits - (at & 7))) & ((1 << bits) - 1)
        out.append((p["ref"] + x * p["e"]) / p["d"])
    return np.array(out, dtype=np.float64)


def _pack(s: bytes) -> dict:
    template = _u(s, 9, 2)
    if template not in (0, 41):
        raise ValueError(f"packing template 5.{template} isn't read here")
    return {"template": template, "ref": struct.unpack(">f", s[11:15])[0], "e": 2.0 ** _s(s, 15, 2),
            "d": 10.0 ** _s(s, 17, 2), "bits": s[19], "n": _u(s, 5)}


def _grid(s: bytes) -> dict:
    template = _u(s, 12, 2)
    if template == 0:
        lo1 = _u(s, 50) / 1e6
        return {"template": 0, "ni": _u(s, 30), "nj": _u(s, 34), "la1": _s(s, 46) / 1e6, "lo1": (lo1 + 180) % 360 - 180,
                "di": _u(s, 63) / 1e6, "dj": _u(s, 67) / 1e6}
    if template == 30:
        shape = s[14]
        radius = _u(s, 16) / 10 ** s[15] if shape == 1 else EARTH.get(shape)
        if not radius:
            raise ValueError(f"earth shape {shape} isn't read here")
        return {"template": 30, "nx": _u(s, 30), "ny": _u(s, 34), "la1": _s(s, 38) / 1e6, "lo1": (_u(s, 42) / 1e6 + 180) % 360 - 180,
                "lad": _s(s, 47) / 1e6, "lov": (_u(s, 51) / 1e6 + 180) % 360 - 180, "dx": _u(s, 55) / 1e3, "dy": _u(s, 59) / 1e3,
                "latin1": _s(s, 65) / 1e6, "latin2": _s(s, 69) / 1e6, "radius": radius,
                "grid_relative": bool(s[46] & 0x08), "south_first": bool(s[64] & 0x40)}
    raise ValueError(f"grid template 3.{template} isn't read here")


def values(msg: dict) -> np.ndarray:
    """A simple-packed (5.0) message's values, float32, one row per grid row in the file's order."""
    p = msg["pack"]
    if p["template"] != 0:
        raise ValueError("values() reads simple packing (5.0)")
    g = msg["grid"]
    shape = (g["ny"], g["nx"]) if g["template"] == 30 else (g["nj"], g["ni"])
    v = unpack_bits(msg["data"], p["bits"], p["n"]).astype(np.float32)
    v *= np.float32(p["e"])
    v += np.float32(p["ref"])
    v /= np.float32(p["d"])
    return v.reshape(shape)


def unpack_bits(data: bytes, bits: int, n: int) -> np.ndarray:
    """n unsigned integers of `bits` bits each, packed big-end first with no padding between them -> uint32.
    Each is read from the 4 bytes it starts in (so up to 25 bits), CHUNK at a time (all at once, an RTMA field's
    index arrays alone would be ~150 MB)."""
    out = np.zeros(n, np.uint32)
    if bits == 0:
        return out
    if bits > 25:
        raise ValueError(f"{bits}-bit packing isn't read here")
    raw = np.frombuffer(bytes(data) + b"\0\0\0\0", np.uint8)
    mask = np.uint32((1 << bits) - 1)
    for k in range(0, n, CHUNK):
        at = np.arange(k, min(n, k + CHUNK), dtype=np.int64) * bits
        byte, shift = at >> 3, (32 - bits - (at & 7)).astype(np.uint32)
        word = (raw[byte].astype(np.uint32) << 24) | (raw[byte + 1].astype(np.uint32) << 16) \
            | (raw[byte + 2].astype(np.uint32) << 8) | raw[byte + 3].astype(np.uint32)
        out[k:k + len(at)] = (word >> shift) & mask
    return out
