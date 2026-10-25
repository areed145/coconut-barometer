"""Perceptual image fingerprints for spotting re-uploads in the Flickr library."""
from __future__ import annotations

import io
import math

from PIL import Image, ImageOps

from .planner import photo_page

# Separable DCT-II basis: the first eight frequencies over 32 samples.
_COS = [[math.cos((2 * x + 1) * u * math.pi / 64) for x in range(32)] for u in range(8)]


def phash(data: bytes) -> int:
    """A compact 63-bit perceptual hash (32x32 DCT, low-frequency 8x8 coefficients less the DC term)."""
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    pixels = list(image.getdata())
    # Keeping the first eight coefficients makes this quick and robust to Flickr's thumbnail
    # resizing and JPEG recompression.
    rows = [[sum(pixels[y * 32 + x] * _COS[u][x] for x in range(32)) for u in range(8)] for y in range(32)]
    coeff = [[sum(rows[y][u] * _COS[v][y] for y in range(32)) for u in range(8)] for v in range(8)]
    low = sorted(coeff[y][x] for y in range(8) for x in range(8) if (x, y) != (0, 0))
    median = low[len(low) // 2]
    out = 0
    for y in range(8):
        for x in range(8):
            if (x, y) != (0, 0):
                out = (out << 1) | (coeff[y][x] > median)
    return out


def find_duplicate(store, photo_id: str, downloader=None, threshold: int = 6) -> dict | None:
    """The closest convincing match among photos uploaded before this one, if any. Library photos
    without a fingerprint yet are fingerprinted on the way (once: they're kept). Raises if this
    photo's own thumbnail can't be fetched or read."""
    if downloader is None:
        from .classify import _download
        downloader = _download
    current = store.photo(photo_id)
    if not current or not current["thumb_url"] or current["media"] == "video":
        return None
    cache = store.photo_fingerprint(photo_id)
    if cache is None:
        current_hash = phash(downloader(current["thumb_url"])[0])
        store.set_photo_fingerprint(photo_id, f"{current_hash:016x}")
    else:
        current_hash = int(cache, 16)

    uploaded = current["date_upload"]
    matches = []
    for p in store.photos():
        if p["id"] == photo_id or not p["thumb_url"] or p["media"] == "video":
            continue
        if uploaded is not None and (p["date_upload"] is None or p["date_upload"] >= uploaded):
            continue        # the original is the earlier one; this is the copy
        other = store.photo_fingerprint(p["id"])
        if other is None:
            try:
                other_hash = phash(downloader(p["thumb_url"])[0])
            except Exception:
                continue  # inaccessible/removed thumbnails do not stop review
            store.set_photo_fingerprint(p["id"], f"{other_hash:016x}")
        else:
            other_hash = int(other, 16)
        distance = (current_hash ^ other_hash).bit_count()
        if distance <= threshold:
            matches.append((distance, p))
    if not matches:
        return None
    distance, p = min(matches, key=lambda pair: pair[0])
    return {"photo_id": p["id"], "title": p["title"], "thumb": p["thumb_url"],
            "url": photo_page(store.get_meta("nsid", "me"), p["id"]),
            "distance": distance}
