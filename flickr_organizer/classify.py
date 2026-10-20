"""Run the vision model over photo thumbnails and cache the results."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

PROMPT_VERSION = 3

PROMPT = """You are helping organize a personal Flickr photo library into albums.

Score how well this photo fits each category below, from 0.0 (clearly not) to 1.0
(clearly yes). A category fits only if it is a MAIN subject of the photo, not a
minor background detail. A photo can fit several categories, or none.

Categories:
{categories}
{extra_tags}
Also write a short, plain caption (max 15 words) describing the photo.
{describe}{title}{context}"""

# The writing briefs are editable in config.yaml (vision.describe.instructions,
# vision.title.instructions); these are the defaults. {max_words} is filled in.
DEFAULT_DESCRIBE = """\
Write the description for the photo's Flickr page, like a short caption beside a print
in a gallery: 1-2 sentences, at most {max_words} words. Notice what makes this frame worth
looking at - the light, color, texture, scale, composition, the mood or the moment - and
the specific place. Be concrete and observant; one precise detail is better than several
adjectives. Keep the tone understated: no superlatives or travel-brochure words
("stunning", "breathtaking", "majestic", "nestled", "vibrant", "hidden gem", "tapestry"),
no exclamation marks, and don't start with "This photo", "This image" or "A view of"."""

DEFAULT_TITLE = """\
Propose a title for the photo, like the title of a print in an exhibition: 2 to
{max_words} words, specific and quietly evocative, naming the subject, the moment or the
light rather than describing the whole frame. Include the place when it helps, e.g.
"Low Tide, Alki Beach", "Fog Lifting off Puget Sound", "Last Light on the Treasury,
Petra". No hype words, no quotation marks or ending punctuation, no file names."""

# Always appended, whatever the instructions say.
GROUNDING = """\
Name a place only when the location below or the owner's title or tags give it, and a
landmark only when you clearly recognize it; never guess a location, and never name or
guess who a person is."""
TITLE_GROUNDING = """\
If the owner's current title names a place, keep that place in the new title."""

EXTRA_TAGS = """
Also suggest up to {max} specific, lowercase descriptive tags for things that are
clearly visible (e.g. "ramen", "great blue heron", "suspension bridge", "fog"). Put the
most searchable first:
- the name of a landmark, park, or natural feature you clearly recognize (e.g. "space
  needle", "half dome", "wadi rum"), using the location below to confirm it
- the specific type of any aircraft (e.g. "p-51 mustang", "cessna 172", "boeing 747",
  "blue angels"), and its registration if it is legible (e.g. "n5777v")
- the most specific common name of any animal or plant (e.g. "hawksbill turtle",
  "bougainvillea"), only when you are confident of it
Avoid generic words (photo, image, picture, outdoor), town/state/country names (those
are added separately), dates, camera terms, and people's names or identities.
"""


def categories_block(categories: dict) -> str:
    return "\n".join(f"- {name}: {' '.join(c['description'].split())}" for name, c in categories.items())


def build_schema(vcfg: dict) -> dict:
    cats = list(vcfg["categories"])
    props = {
        "scores": {
            "type": "object",
            "properties": {c: {"type": "number", "minimum": 0, "maximum": 1} for c in cats},
            "required": cats,
        },
        "caption": {"type": "string"},
    }
    required = ["scores", "caption"]
    if vcfg["extra_tags"]["enabled"]:
        props["tags"] = {"type": "array", "items": {"type": "string"}, "maxItems": vcfg["extra_tags"]["max"]}
        required.append("tags")
    if vcfg["describe"]["enabled"]:
        props["description"] = {"type": "string"}
        required.append("description")
    if vcfg["title"]["enabled"]:
        props["title"] = {"type": "string"}
        required.append("title")
    return {"type": "object", "properties": props, "required": required}


def build_prompt(vcfg: dict, photo, location: str = "") -> str:
    extra = EXTRA_TAGS.format(max=vcfg["extra_tags"]["max"]) if vcfg["extra_tags"]["enabled"] else ""
    context = ""
    if vcfg.get("use_photo_text", True):
        bits = []
        if photo["title"]:
            bits.append(f"Title: {photo['title']}")
        if photo["description"]:
            bits.append(f"Description: {photo['description'][:400]}")
        if photo["tags"]:
            bits.append(f"Existing tags: {photo['tags'][:300]}")
        if bits:
            context = "\nThe owner wrote this about the photo (may help, may be unrelated):\n" + "\n".join(bits)
    if "media" in photo.keys() and photo["media"] == "video":
        context += "\nThis image is the preview frame of a video; describe the video's subject."
    if location and vcfg.get("use_location", True):
        context += f"\nLocation from the photo's geotag (nearest town; approximate): {location}"
    return PROMPT.format(categories=categories_block(vcfg["categories"]), extra_tags=extra,
                         describe=_brief(vcfg["describe"], DEFAULT_DESCRIBE, GROUNDING),
                         title=_brief(vcfg["title"], DEFAULT_TITLE, GROUNDING + "\n" + TITLE_GROUNDING),
                         context=context)


def _brief(cfg: dict, default: str, rules: str) -> str:
    if not cfg.get("enabled"):
        return ""
    text = (cfg.get("instructions") or default).strip().format(max_words=cfg.get("max_words", 40))
    return f"\n{text}\n{rules}\n"


def text_fingerprint(vcfg: dict) -> str:
    """The description/title briefs. Kept out of fingerprint() so editing them re-runs
    only the model call, and turning them off keeps every cached result."""
    blob = json.dumps({k: vcfg[k] for k in ("describe", "title") if vcfg[k].get("enabled")}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def fingerprint(vcfg: dict, provider_key: str) -> str:
    """Changes when the model, categories, or prompt change → those photos get re-classified."""
    blob = json.dumps({
        "v": PROMPT_VERSION,
        "provider": provider_key,
        "cats": {k: v["description"] for k, v in sorted(vcfg["categories"].items())},
        "tags": vcfg["extra_tags"],
        "thumb": vcfg["thumbnail"],
        "location": vcfg.get("use_location", True),
    }, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def pending_photos(store, vcfg: dict, fp: str, limit: int | None = None, photo_ids: list[str] | None = None):
    done = store.classified_ids(fp)
    if vcfg["describe"]["enabled"] or vcfg["title"]["enabled"]:
        # Results written under different (or no) description/title briefs: classify again.
        tfp = text_fingerprint(vcfg)
        done = {pid for pid in done if (store.classification(pid, fp) or {}).get("text_fp") == tfp}
    out = []
    for p in store.photos():
        if photo_ids and p["id"] not in photo_ids:
            continue
        if p["id"] in done or not p["thumb_url"]:
            continue
        if p["media"] == "video" and not vcfg.get("include_videos", False):
            continue
        out.append(p)
        if limit and len(out) >= limit:
            break
    return out


def _download(url: str) -> tuple[bytes, str]:
    import requests

    r = requests.get(url, timeout=30)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    if ctype not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        ctype = "image/jpeg"
    return r.content, ctype


def classify_photos(store, provider, vcfg: dict, photos, log=print, downloader=_download,
                    locations: dict[str, str] | None = None) -> dict:
    """locations: photo_id -> place label from its geotag (see geo.place_label)."""
    fp = fingerprint(vcfg, provider.key)
    schema = build_schema(vcfg)
    stats = {"ok": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0}

    def work(p):
        img, ctype = downloader(p["thumb_url"])
        result = provider.classify(img, ctype, build_prompt(vcfg, p, (locations or {}).get(p["id"], "")), schema)
        return p, result

    with ThreadPoolExecutor(max_workers=max(1, int(vcfg.get("concurrency", 4)))) as pool:
        futures = [pool.submit(work, p) for p in photos]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                p, result = fut.result()
            except Exception as e:  # keep going; failed photos are retried next run
                stats["failed"] += 1
                log(f"  ! failed: {e}")
                continue
            usage = result.pop("_usage", {})
            stats["input_tokens"] += usage.get("input", 0)
            stats["output_tokens"] += usage.get("output", 0)
            result["model"] = provider.key
            result["text_fp"] = text_fingerprint(vcfg)
            store.save_classification(p["id"], fp, result)
            stats["ok"] += 1
            if i % 25 == 0 or i == len(futures):
                log(f"  {i}/{len(futures)} classified")
    return stats
