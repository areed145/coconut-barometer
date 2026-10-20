"""Combine geo + vision results into a reviewable plan of additive changes.

The plan never removes anything. It skips changes that already exist on Flickr,
and anything you previously applied or rejected.
"""
from __future__ import annotations

import csv
import os
from dataclasses import asdict, dataclass, field

from .classify import fingerprint
from .geo import GeoMatcher
from .rename import Renamer
from .standard_tags import StandardTagger
from .util import clean_tag, norm_tag, norm_title, same_text

PLAN_FIELDS = ["approve", "action", "photo_id", "target", "album_id", "source", "confidence",
               "reason", "photo_title", "photo_url", "thumb_url", "original_target"]


@dataclass
class PlanRow:
    action: str            # add_to_album | add_tags | set_title | set_description
    photo_id: str
    target: str            # album title, " | "-separated tags, the new title, or the new description
    album_id: str = ""     # blank = album will be created
    source: str = ""       # geo:country, vision:food, vision:tags, ...
    confidence: str = ""
    reason: str = ""
    photo_title: str = ""
    photo_url: str = ""
    thumb_url: str = ""
    approve: str = "y"
    original_target: str = field(default="")


def photo_page(nsid: str, photo_id: str) -> str:
    return f"https://www.flickr.com/photos/{nsid}/{photo_id}"


def build_plan(cfg: dict, store, provider_key: str | None, geocoder=None,
               photo_ids: set[str] | None = None) -> list[PlanRow]:
    """photo_ids: plan only these photos (the per-photo queue); default is the whole library."""
    nsid = store.get_meta("nsid", "me")
    albums = store.albums()
    title_to_id = {norm_title(a["title"]): a["id"] for a in albums}
    title_display = {norm_title(a["title"]): a["title"] for a in albums}
    members = store.memberships()
    decisions = store.decisions()
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
    rows: list[PlanRow] = []
    for pid, wants in album_wants.items():
        p = photos[pid]
        have = members.get(pid, set())
        for key, (title, source, conf, reason) in wants.items():
            album_id = title_to_id.get(key, "")
            if not album_id and not create_missing:
                continue
            display = title_display.get(key, title)
            if album_id and album_id in have:
                continue
            if (pid, "add_to_album", display) in decisions:
                continue
            rows.append(PlanRow("add_to_album", pid, display, album_id, source, conf,
                                reason if album_id else f"{reason} (NEW ALBUM)",
                                p["title"], photo_page(nsid, pid), p["thumb_url"] or ""))

    for pid, wants in tag_wants.items():
        p = photos[pid]
        have = {norm_tag(t) for t in (p["tags"] or "").split()}
        new = [(t, s) for k, (t, s) in wants.items()
               if k not in have and (pid, "add_tag", t) not in decisions]
        # Flickr caps tags per photo; wants are in priority order, so the least useful drop off.
        new = new[:max(0, int(scfg.get("max_tags_per_photo", 75)) - len(have))]
        if not new:
            continue
        tags = [t for t, _ in new]
        target = " | ".join(tags)
        rows.append(PlanRow("add_tags", pid, target, "", ",".join(sorted({s for _, s in new})), "", "",
                            p["title"], photo_page(nsid, pid), p["thumb_url"] or "", original_target=target))

    # ---- titles: the model's proposal where allowed, else the location rename ---
    renamer = Renamer(rcfg, gcfg)
    new_titles: dict[str, tuple[str, str]] = {}   # photo_id -> (title, source)
    if rcfg.get("enabled"):
        for pid, old_title, new_title in renamer.suggest(list(photos.values()), places):
            if (pid, "set_title", new_title) not in decisions:
                new_titles[pid] = (new_title, "geo:rename")
    mode = vcfg["title"].get("replace", "generic_or_place")
    for pid, title in vision_titles.items():
        p = photos[pid]
        if title == p["title"] or (pid, "set_title", title) in decisions:
            continue
        if renamer.replaceable(p, places.get(pid), mode):
            new_titles[pid] = (title, "vision:title")
    for pid, (title, source) in new_titles.items():
        p = photos[pid]
        rows.append(PlanRow("set_title", pid, title, "", source, "", f"was: {p['title'] or '(blank)'}",
                            p["title"], photo_page(nsid, pid), p["thumb_url"] or "", original_target=p["title"]))

    # ---- descriptions -------------------------------------------------------
    # Blank ones get one proposal: once applied or rejected (even after editing), never again.
    # With describe.reword, a description this tool wrote that's still exactly as written is
    # re-worded once after the brief changes (its new text is newer than when it was applied);
    # anything you wrote or edited yourself is left alone. original_target = what it replaces.
    described = {pid for (pid, action, _), status in decisions.items()
                 if action == "set_description" and status != "failed"}
    written = store.applied("set_description") if vcfg["describe"].get("reword", True) else {}
    for pid, text in descriptions.items():
        p = photos[pid]
        current = (p["description"] or "").strip()
        if not current:
            if pid in described:
                continue
            reason = ""
        else:
            ours = written.get(pid)
            if (not ours or not same_text(ours[0], current) or same_text(text, current)
                    or (store.classified_at(pid, fp) or 0) <= ours[1] or (pid, "set_description", text) in decisions):
                continue
            reason = "re-worded with the current brief (the old text was written by this tool)"
        rows.append(PlanRow("set_description", pid, text, "", "vision:describe", "", reason,
                            p["title"], photo_page(nsid, pid), p["thumb_url"] or "", original_target=current))

    rows.sort(key=lambda r: (r.action, r.target.lower() if r.action == "add_to_album" else "", r.photo_id))
    return rows


def write_plan_csv(rows: list[PlanRow], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: asdict(r)[k] for k in PLAN_FIELDS})


def read_plan_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def split_tags(target: str) -> list[str]:
    return [clean_tag(t) for t in (target or "").split("|") if clean_tag(t)]
