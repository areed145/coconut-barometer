"""Share links: /p/<photo id> and /a/<album id> are pages of their own, so a link pasted into a chat or a
post gets a preview. Previewers don't run scripts, so the title, description and image go in the HTML
(Open Graph and Twitter tags); the app then opens that photo or album. Plus robots.txt and a sitemap
of those pages.

Public photos and albums only, whoever asks: a private one gets the plain page, exactly like one that
doesn't exist, so a link doesn't tell anyone it's there."""
from __future__ import annotations

import datetime as dt
import html
import re
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from .deps import runtime, store

STATIC = Path(__file__).parent / "static"
SITE = "Coconut Barometer"
ABOUT = "Photos, a live weather station and APRS tracking."
PREVIEW = [("b", 1024), ("c", 800), ("z", 640), ("w", 400), ("n", 320), ("m", 0)]   # (size, long side): 1024 px is plenty
SITEMAP_MAX = 50_000                              # URLs in one sitemap (the protocol's limit)

router = APIRouter(include_in_schema=False)


def base_url(request: Request, rt) -> str:
    """This site's address, for absolute links. The host is already checked (allowed_hosts); with
    secure cookies the site is only used over HTTPS, whatever the proxy in front said."""
    base = str(request.base_url).rstrip("/")
    return "https://" + base.split("://", 1)[1] if rt.cfg["web"]["secure_cookies"] else base


def text(s: str | None, limit: int = 200) -> str:
    """Flickr descriptions may hold HTML: plain text on one line, cut at a word."""
    t = " ".join(html.unescape(re.sub(r"</?[A-Za-z][^>]*>", " ", s or "")).split())
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def page(request: Request, rt, title: str = "", description: str = "", image: str = "", path: str = "/",
         kind: str = "website") -> HTMLResponse:
    """index.html with the tags for a link preview of this page."""
    base = base_url(request, rt)
    description = description or ABOUT
    url = base + path
    props = {"og:site_name": SITE, "og:type": kind, "og:title": title or SITE, "og:description": description,
             "og:url": url, "og:image": image or base + "/static/img/logo-320.png"}
    tags = [f'<meta name="description" content="{html.escape(description)}">',
            f'<link rel="canonical" href="{html.escape(url)}">',
            *(f'<meta property="{k}" content="{html.escape(v)}">' for k, v in props.items()),
            f'<meta name="twitter:card" content="{"summary_large_image" if image else "summary"}">']
    head = f"<title>{html.escape(f'{title} · {SITE}' if title else SITE)}</title>\n" + "\n".join(tags)
    doc = (STATIC / "index.html").read_text(encoding="utf-8").replace(f"<title>{SITE}</title>", head, 1)
    return HTMLResponse(doc, headers={"Cache-Control": "no-cache"})


@router.get("/p/{photo_id}")
def photo(photo_id: str, request: Request, rt=Depends(runtime), db=Depends(store)):
    p = db.photo_detail(photo_id, public_only=True) if photo_id.isdigit() else None
    if not p:
        return page(request, rt)
    # Flickr has a size only if the original is at least that big (unknown size: assume it is).
    longest = max(p["width"] or 0, p["height"] or 0) or 10_000
    image = next((p["sizes"][k] for k, px in PREVIEW if k in p["sizes"] and px <= longest), "")
    taken = f"Taken {p['date_taken'][:10]}" if p.get("date_taken") else ""
    return page(request, rt, title=text(p["title"], 100) or "Untitled", description=text(p["description"]) or taken,
                image=image, path=f"/p/{photo_id}", kind="article")


@router.get("/a/{album_id}")
def album(album_id: str, request: Request, rt=Depends(runtime), db=Depends(store)):
    a = db.album(album_id, public_only=True) if album_id.isdigit() else None
    if not a:
        return page(request, rt)
    count = f"{a['count']:,} photo{'' if a['count'] == 1 else 's'}"
    return page(request, rt, title=text(a["title"], 100), description=text(a["description"]) or count,
                image=a["cover_url"] or "", path=f"/a/{album_id}")


@router.get("/robots.txt")
def robots(request: Request, rt=Depends(runtime)):
    return PlainTextResponse(f"User-agent: *\nAllow: /\nSitemap: {base_url(request, rt)}/sitemap.xml\n",
                             headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sitemap.xml")
def sitemap(request: Request, rt=Depends(runtime), db=Depends(store)):
    """The home page, every public album, then public photos, newest first."""
    base = base_url(request, rt)
    day = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat() if ts else None
    urls = [(base + "/", None)]
    urls += [(f"{base}/a/{a['id']}", day(a["date_update"])) for a in db.album_list(public_only=True)]
    urls += [(f"{base}/p/{pid}", day(ts)) for pid, ts in db.public_photo_ids(SITEMAP_MAX - len(urls))]
    body = "\n".join(f"<url><loc>{xml_escape(u)}</loc>{f'<lastmod>{m}</lastmod>' if m else ''}</url>"
                     for u, m in urls[:SITEMAP_MAX])
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{body}\n</urlset>\n'
    return Response(xml, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})
