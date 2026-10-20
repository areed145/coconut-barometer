"""Turn what's known about each photo (geotag, vision results, EXIF, albums) into
proposed Changes.

Proposals only ever add, apart from titles and descriptions the rules allow replacing.
Anything already true on Flickr, or settled before (see changes.Settled), is skipped.
"""
from __future__ import annotations

from collections import Counter

from .changes import Change, Settled
from .classify import fingerprint
from .geo import GeoMatcher
from .rename import Renamer
from .standard_tags import StandardTagger
from .util import clean_tag, norm_tag, norm_title


def photo_page(nsid: str, photo_id: str) -> str:
    return f"https://www.flickr.com/photos/{nsid}/{photo_id}"


def build_plan(cfg: dict, store, provider_key: str | None, geocoder=None,
               photo_ids: set[str] | None = None) -> list[Change]:
    """photo_ids: plan only these photos (the per-photo queue); default is the whole library."""
    albums = store.albums()
    title_to_id = {norm_title(a["title"]): a["id"] for a in albums}
    title_display = {norm_title(a["title"]): a["title"] for a in albums}
    members = store.memberships()
    settled = Settled(store)
    create_missing = cfg["albums"].get("create_missing", False)
    photos = {p["id"]: p for p in store.photos() if photo_ids is None or p["id"] in photo_ids}

    # photo_id -> {norm album title: (display title, source, conf, reason)}
    album_wants: dict[str, dict[str, tuple]] = {}
    # photo_id -> {norm tag: (tag, source)}
    tag_wants: dict[str, dict[str, tuple]] = {}

    def want_album(pid, title, source, conf, reason):
        album_wants.setdefault(pid, {}).setdefault(norm_title(title), (title, source, conf, reason))

    def want_tag(pid, tag, source):
        tag = clean_tag(tag)
        if tag:
            tag_wants.setdefault(pid, {}).setdefault(norm_tag(tag), (tag, source))

    # ---- geo --------------------------------------------------------------
    gcfg = cfg["geo"]
    rcfg = cfg.get("rename", {})
    places: dict[str, dict] = {}
    matcher = GeoMatcher(gcfg, title_to_id, geocoder=geocoder, create_missing=create_missing)
    scfg = cfg["standard_tags"]
    std = StandardTagger(scfg, gcfg, {a["id"]: a["title"] for a in albums}, cfg["vision"]["categories"])
    if gcfg.get("enabled", True) or rcfg.get("enabled") or std.needs_places() or cfg["vision"]["title"].get("enabled"):
        places = matcher.locate(list(photos.values()))
    if gcfg.get("enabled", True):
        for s in matcher.suggest(list(photos.values()), places):
            want_album(s.photo_id, s.album_title, f"geo:{s.level}", "", s.reason)
            if gcfg.get("add_tags") and s.tag:
                want_tag(s.photo_id, s.tag, f"geo:{s.level}")

    # ---- vision -----------------------------------------------------------
    vcfg = cfg["vision"]
    describe = vcfg["describe"].get("enabled")
    fp = None
    descriptions: dict[str, str] = {}   # photo_id -> model-written description
    vision_titles: dict[str, str] = {}   # photo_id -> model-proposed title
    if vcfg.get("enabled", True) and provider_key and vcfg["categories"]:
        fp = fingerprint(vcfg, provider_key)
        min_conf = float(vcfg.get("min_confidence", 0.75))
        prefix = vcfg["extra_tags"].get("prefix", "") or ""
        for pid in photos:
            res = store.classification(pid, fp)
            if not res:
                continue
            scores = res.get("scores", {})
            for cat, c in vcfg["categories"].items():
                conf = float(scores.get(cat, 0) or 0)
                if conf < min_conf:
                    continue
                if c.get("album"):
                    want_album(pid, c["album"], f"vision:{cat}", f"{conf:.2f}", res.get("caption", ""))
                for t in c.get("tags", []) or []:
                    want_tag(pid, t, f"vision:{cat}")
            if vcfg["extra_tags"].get("enabled"):
                for t in (res.get("tags") or [])[: vcfg["extra_tags"]["max"]]:
                    want_tag(pid, prefix + t, "vision:tags")
            if describe and (res.get("description") or "").strip():
                descriptions[pid] = res["description"].strip()
            if vcfg["title"].get("enabled") and (res.get("title") or "").strip(" \"'.\n"):
                vision_titles[pid] = res["title"].strip(" \"'.\n")

    # ---- standard tags (date, place, camera, album, coords, owner, filler) --
    if std.any_enabled():
        cameras = store.cameras() if scfg["camera"].get("enabled") else {}
        for pid, p in photos.items():
            camera = cameras.get(pid) or ""
            for t, source in std.tags_for(p, places.get(pid), camera, members.get(pid, set())):
                want_tag(pid, t, source)
            for title in std.camera_albums(camera) if camera else []:
                want_album(pid, title, "std:camera", "", f"camera: {camera}")

    # ---- diff against current state --------------------------------------
    rows: list[Change] = []
    for pid, wants in album_wants.items():
        have = members.get(pid, set())
        for key, (title, source, conf, reason) in wants.items():
            album_id = title_to_id.get(key, "")
            if not album_id and not create_missing:
                continue
            display = title_display.get(key, title)
            if album_id and album_id in have:
                continue
            if settled.album(pid, display):
                continue
            rows.append(Change("add_to_album", pid, display, album_id=album_id, source=source, confidence=conf,
                               reason=reason if album_id else f"{reason} (NEW ALBUM)"))

    for pid, wants in tag_wants.items():
        p = photos[pid]
        have = {norm_tag(t) for t in (p["tags"] or "").split()}
        new = [(t, s) for k, (t, s) in wants.items() if k not in have and not settled.tag(pid, t)]
        # Flickr caps tags per photo; wants are in priority order, so the least useful drop off.
        new = new[:max(0, int(scfg.get("max_tags_per_photo", 75)) - len(have))]
        if not new:
            continue
        rows.append(Change("add_tags", pid, [t for t, _ in new], source=",".join(sorted({s for _, s in new}))))

    # ---- titles: the model's proposal where allowed, else the location rename ---
    renamer = Renamer(rcfg, gcfg)
    new_titles: dict[str, tuple[str, str]] = {}   # photo_id -> (title, source)
    if rcfg.get("enabled"):
        for pid, old_title, new_title in renamer.suggest(list(photos.values()), places):
            if not settled.title(pid, new_title):
                new_titles[pid] = (new_title, "geo:rename")
    mode = vcfg["title"].get("replace", "generic_or_place")
    for pid, title in vision_titles.items():
        p = photos[pid]
        if title == p["title"] or settled.title(pid, title):
            continue
        if renamer.replaceable(p, places.get(pid), mode):
            new_titles[pid] = (title, "vision:title")
    for pid, (title, source) in new_titles.items():
        p = photos[pid]
        rows.append(Change("set_title", pid, title, old=p["title"] or "", source=source,
                           reason=f"was: {p['title'] or '(blank)'}"))

    # ---- descriptions -------------------------------------------------------
    # Blank ones get one proposal per photo; with describe.reword, one this tool wrote and you
    # haven't edited gets re-worded once per brief change (see changes.Settled).
    reword = vcfg["describe"].get("reword", True)
    for pid, text in descriptions.items():
        current = (photos[pid]["description"] or "").strip()
        if not current:
            if settled.blank_description(pid):
                continue
            reason = ""
        elif reword and settled.may_reword(pid, current, text, store.classified_at(pid, fp) or 0):
            reason = "re-worded with the current brief (the old text was written by this tool)"
        else:
            continue
        rows.append(Change("set_description", pid, text, old=current, source="vision:describe", reason=reason))

    rows.sort(key=lambda c: (c.action, c.new.lower() if c.action == "add_to_album" else "", c.photo_id))
    return rows


def summarize(changes: list[Change]) -> Counter:
    """How many of each: an album's title, or tags / renames / descriptions."""
    label = {"add_tags": "tags", "set_title": "titles", "set_description": "descriptions"}
    return Counter(label.get(c.action, c.new) for c in changes)
