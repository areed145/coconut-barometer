"""Shared test doubles: a fake Flickr API, fake geocoder, fake vision model, base config."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from coconut_barometer import settings as _settings  # noqa: E402

# Never near your real key files: the encryption key lives in a temp folder unless a test moves it.
_KEYS = tempfile.mkdtemp(prefix="cb-test-keys-")
_settings.DEFAULT_KEY_FILE = os.path.join(_KEYS, "secret.key")
_settings.OLD_KEY_FILE = os.path.join(_KEYS, "before-the-rename", "secret.key")

from coconut_barometer.flickr_client import FlickrCallError  # noqa: E402
from coconut_barometer.vision.base import VisionProvider  # noqa: E402


def photo(pid, lat=0, lon=0, acc=16, title="", tags=""):
    return {"id": pid, "secret": "s", "server": "1", "title": title, "ispublic": 1,
            "description": {"_content": ""}, "tags": tags, "latitude": lat, "longitude": lon, "geo_is_public": 1,
            "accuracy": acc, "media": "photo", "url_m": f"https://live.staticflickr.com/1/{pid}_s.jpg",
            "datetaken": "2024-01-01", "dateupload": "1700000000", "lastupdate": "1700000000"}


class FakeFlickr:
    def __init__(self):
        self.photos = [
            photo("1", 35.68, 139.69, title="Ramen in Tokyo"),   # Japan, food
            photo("2", 30.27, -97.74),                            # Austin, TX
            photo("3", 29.76, -95.37, acc=6),                     # Houston: state ok, city no
            photo("4"),                                           # no geo
            photo("5", 30.27, -97.74, tags="food"),               # already tagged food
        ]
        # Japan album already has photo 1? No. Texas has photo 2 already.
        self.sets = {"100": {"title": "Japan", "photos": []},
                     "101": {"title": "Texas", "photos": ["2"]},
                     "102": {"title": "Food", "photos": []},
                     "103": {"title": "Austin", "photos": []},
                     "104": {"title": "United States", "photos": []},
                     "105": {"title": "Landscapes", "photos": []}}
        self.calls = []
        self.clock = 1700000000                  # Flickr's "now", moved on by every write
        self.activity = []                       # activity.userPhotos items
        self.daily = {}                          # stats day -> {"total": n, "photos": [(id, views, comments, faves)]}
        self.fail_next: dict[str, int] = {}      # method -> how many of its next calls fail (a Flickr outage)
        self.pools: dict[str, list[str]] = {}    # group id -> photos added by pools.add
        self.moderated: set[str] = set()         # groups whose additions wait for a moderator
        self.throttles: dict[str, dict] = {}     # group id -> {count, mode, remaining}: its posting limit
        self.refusing: dict[str, int] = {}       # group id -> the error code pools.add answers with

    @property
    def call_count(self):
        return len(self.calls)

    def _touch(self, photo_id=None, set_id=None):
        self.clock += 10
        for ph in self.photos:
            if ph["id"] == photo_id:
                ph["lastupdate"] = str(self.clock)
        if set_id:
            self.sets[set_id]["date_update"] = self.clock

    def nsid(self):
        return "12345@N00"

    def call(self, method, expect=(), **p):
        self.calls.append((method, p))
        if self.fail_next.get(method):
            self.fail_next[method] -= 1
            raise FlickrCallError(method, 105, "Service currently unavailable")
        page, per = int(p.get("page", 1)), 2   # tiny pages to exercise pagination
        def paged(items):
            chunk = items[(page - 1) * per: page * per]
            return chunk, max(1, -(-len(items) // per))
        if method == "flickr.people.getPhotos":
            chunk, pages = paged(self.photos)
            return {"photos": {"photo": chunk, "page": page, "pages": pages}}
        if method == "flickr.photosets.getList":
            items = [{"id": k, "title": {"_content": v["title"]}, "photos": len(v["photos"]), "videos": 0,
                      "date_update": str(v.get("date_update", 1700000000)),
                      "primary_photo_extras": {"url_q": f"https://live.staticflickr.com/1/{k}_q.jpg"}}
                     for k, v in self.sets.items()]
            chunk, pages = paged(items)
            return {"photosets": {"photoset": chunk, "page": page, "pages": pages}}
        if method == "flickr.photosets.getPhotos":
            chunk, pages = paged([{"id": x} for x in self.sets[p["photoset_id"]]["photos"]])
            return {"photoset": {"photo": chunk, "page": str(page), "pages": str(pages)}}
        if method == "flickr.photosets.addPhoto":
            s = self.sets[p["photoset_id"]]["photos"]
            if p["photo_id"] in s:
                raise FlickrCallError(method, 3, "Photo already in set")
            s.append(p["photo_id"])
            self._touch(set_id=p["photoset_id"])
            return {}
        if method == "flickr.photosets.create":
            new = str(200 + len(self.sets))
            self.sets[new] = {"title": p["title"], "photos": [p["primary_photo_id"]]}
            return {"photoset": {"id": new}}
        if method == "flickr.photos.setMeta":
            assert "description" in p
            for ph in self.photos:
                if ph["id"] == p["photo_id"]:
                    ph["title"] = p["title"]
                    ph["description"] = {"_content": p["description"]}
            self._touch(photo_id=p["photo_id"])
            return {}
        if method == "flickr.photos.licenses.getInfo":
            return {"licenses": {"license": [{"id": 0, "name": "All Rights Reserved"}]}}
        if method == "flickr.stats.getTotalViews":
            if "date" not in p:
                return {"stats": {"total": {"views": 1234}}}
            if p["date"] not in self.daily:
                raise FlickrCallError(method, 2, "No stats for that date")
            return {"stats": {"total": {"views": self.daily[p["date"]]["total"]}, "photos": {"views": "1"}}}
        if method == "flickr.stats.getPopularPhotos":
            rows = [{"id": i, "stats": {"views": v, "comments": c, "favorites": f}}
                    for i, v, c, f in self.daily[p["date"]]["photos"]]
            chunk, pages = paged(rows)
            return {"photos": {"photo": chunk, "page": page, "pages": pages}}
        if method == "flickr.photos.recentlyUpdated":
            chunk, pages = paged([ph for ph in self.photos if int(ph["lastupdate"]) >= int(p["min_date"])])
            return {"photos": {"photo": chunk, "page": page, "pages": pages}}
        if method == "flickr.people.getInfo":
            return {"person": {"photos": {"count": {"_content": len(self.photos)}}}}
        if method == "flickr.activity.userPhotos":
            chunk, pages = paged(self.activity)
            return {"items": {"item": chunk, "page": page, "pages": pages}}
        if method == "flickr.groups.pools.getGroups":
            chunk, pages = paged([{"nsid": "g1", "name": "Ramen Lovers", "members": "50", "photos": "900"},
                                  {"nsid": "g2", "name": "Texas", "members": "10", "photos": "30"}])
            return {"groups": {"group": chunk, "page": page, "pages": pages}}
        if method == "flickr.groups.pools.getPhotos":
            assert p["user_id"] == "12345@N00"
            ids = {"g1": ["1", "5"], "g2": ["2"]}[p["group_id"]]
            chunk, pages = paged([{"id": x, "dateadded": "1700000001"} for x in ids])
            return {"photos": {"photo": chunk, "page": page, "pages": pages}}
        if method == "flickr.photos.comments.getList":
            return {"comments": {"comment": [{"id": f"c{p['photo_id']}", "author": "9@N0", "authorname": "fan",
                                              "datecreate": "1700000002", "_content": "Nice!"}]}}
        if method == "flickr.photos.getFavorites":
            people = [{"nsid": f"u{i}", "username": f"user{i}", "favedate": "1700000003"} for i in range(60)]
            per, pg = int(p["per_page"]), int(p["page"])
            return {"photo": {"person": people[(pg - 1) * per: pg * per], "page": pg, "pages": 2}}
        if method == "flickr.photos.getInfo":
            ph = next((x for x in self.photos if x["id"] == p["photo_id"]), None)
            if ph is None:
                raise FlickrCallError(method, 1, "Photo not found")             # as Flickr says it
            info = {"id": ph["id"], "server": ph["server"], "secret": ph["secret"], "media": ph["media"],
                    "title": {"_content": ph["title"]}, "description": ph["description"],
                    "tags": {"tag": [{"_content": t} for t in ph["tags"].split()]},
                    "dates": {"taken": ph["datetaken"], "takenunknown": "0", "lastupdate": "1"}}
            if ph["latitude"]:
                info["location"] = {"latitude": ph["latitude"], "longitude": ph["longitude"], "accuracy": ph["accuracy"]}
                info["geoperms"] = {"ispublic": ph["geo_is_public"]}
            return {"photo": info}
        if method == "flickr.photosets.removePhoto":
            self.sets[p["photoset_id"]]["photos"].remove(p["photo_id"])
            return {}
        if method == "flickr.photosets.delete":
            del self.sets[p["photoset_id"]]
            return {}
        if method == "flickr.photos.delete":
            self.photos = [ph for ph in self.photos if ph["id"] != p["photo_id"]]
            return {}
        if method == "flickr.photos.geo.removeLocation":
            for ph in self.photos:
                if ph["id"] == p["photo_id"]:
                    ph.update(latitude=0, longitude=0)
            return {}
        if method == "flickr.groups.getInfo":
            t = self.throttles.get(p["group_id"])
            return {"group": {"id": p["group_id"], **({"throttle": {k: str(v) for k, v in t.items()}} if t else {})}}
        if method == "flickr.groups.pools.add":
            if p["group_id"] in self.refusing:
                raise FlickrCallError(method, self.refusing[p["group_id"]], "Refused")
            t = self.throttles.get(p["group_id"])
            if t is not None:
                if t["remaining"] <= 0:
                    raise FlickrCallError(method, 5, "Photo limit reached")
                t["remaining"] -= 1
            pool = self.pools.setdefault(p["group_id"], [])
            if p["photo_id"] in pool:
                raise FlickrCallError(method, 3, "Photo already in pool")
            if p["group_id"] in self.moderated:
                pool.append(p["photo_id"])
                raise FlickrCallError(method, 6, "Your Photo has been added to the Pending Queue for this Pool")
            pool.append(p["photo_id"])
            return {}
        if method == "flickr.groups.pools.remove":
            self.pools.get(p["group_id"], []).remove(p["photo_id"])
            return {}
        if method == "flickr.photos.geo.setLocation":
            for ph in self.photos:
                if ph["id"] == p["photo_id"]:
                    ph.update(latitude=float(p["lat"]), longitude=float(p["lon"]), accuracy=p["accuracy"], geo_is_public=0)
            self._touch(photo_id=p["photo_id"])
            return {}
        if method == "flickr.photos.getAllContexts":
            return {"set": [{"id": k} for k, v in self.sets.items() if p["photo_id"] in v["photos"]]}
        if method == "flickr.photos.getExif":
            cams = {"1": "Nikon D90", "2": "Apple iPhone X"}
            if p["photo_id"] == "4":
                raise FlickrCallError(method, 2, "Permission denied")
            return {"photo": {"camera": cams.get(p["photo_id"], ""), "exif": [
                {"tag": "Make", "raw": {"_content": "x"}},
                {"tag": "ExposureTime", "raw": {"_content": "0.004"}, "clean": {"_content": "1/250 sec"}},
                {"tag": "LensModel", "raw": {"_content": "28-300mm"}}]}}
        if method == "flickr.photos.setTags":
            import shlex
            from coconut_barometer.util import norm_tag
            for ph in self.photos:
                if ph["id"] == p["photo_id"]:
                    ph["tags"] = " ".join(norm_tag(t) for t in shlex.split(p["tags"]))
            self._touch(photo_id=p["photo_id"])
            return {}
        if method == "flickr.photos.addTags":
            for ph in self.photos:
                if ph["id"] == p["photo_id"]:
                    ph["tags"] = (ph["tags"] + " " + p["tags"]).strip()
            self._touch(photo_id=p["photo_id"])
            return {}
        raise AssertionError(method)


def fake_geocoder(points):
    out = []
    for lat, lon in points:
        if lat < 0:
            out.append({"cc": "AO", "admin1": "Luanda", "name": "Luanda"})
        elif lat > 34:
            out.append({"cc": "JP", "admin1": "Tokyo", "name": "Tokyo"})
        elif lon < -97:
            out.append({"cc": "US", "admin1": "Texas", "name": "Austin"})
        else:
            out.append({"cc": "US", "admin1": "Texas", "name": "Houston"})
    return out


class FakeVision(VisionProvider):
    name = "fake"

    def classify(self, image, media_type, prompt, schema):
        assert set(schema["properties"]["scores"]["required"]) == {"food", "landscape"}
        food = 0.95 if "Ramen" in prompt else 0.1
        out = {"scores": {"food": food, "landscape": 0.2}, "caption": "a bowl of ramen" if food > .5 else "x",
               "tags": ["ramen", "noodle soup"] if food > .5 else [], "_usage": {"input": 800, "output": 60}}
        if "description" in schema["properties"]:
            out["description"] = "A steaming bowl of ramen." if food > .5 else "Something else."
            if "haiku" in prompt:
                out["description"] = "Steam curls over the ramen."
        if "title" in schema["properties"]:
            out["title"] = '"Steam Rising, Tokyo."' if food > .5 else "Quiet Corner"
        return out


CFG = {
    # JP alias so the test doesn't depend on pycountry being installed.
    "geo": {"countries": {"aliases": {"JP": "Japan"}}, "cities": [{"album": "Austin", "lat": 30.2672, "lon": -97.7431, "radius_km": 30}]},
    "vision": {"provider": "fake", "model": "m1",
               "categories": {"food": {"album": "Food", "description": "food", "tags": ["food"]},
                              "landscape": {"album": "Landscapes", "description": "scenery"}}},
}
