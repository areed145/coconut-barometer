"""End-to-end tests with a fake Flickr API, fake geocoder and fake vision model.

Run:  python -m unittest discover tests
"""
import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flickr_organizer.apply import apply_plan
from flickr_organizer.camera import fetch_cameras, pending_cameras
from flickr_organizer.classify import build_prompt, build_schema, classify_photos, fingerprint, pending_photos
from flickr_organizer.config import from_dict
from flickr_organizer.flickr_client import FlickrCallError
from flickr_organizer.pipeline import process
from flickr_organizer.planner import build_plan, read_plan_csv, write_plan_csv
from flickr_organizer.review import ReviewApp
from flickr_organizer import social
from flickr_organizer.report import write_report
from flickr_organizer.store import Store
from flickr_organizer.sync import sync
from flickr_organizer.util import norm_title, quote_tags
from flickr_organizer.vision.base import VisionProvider, parse_json_loose


def photo(pid, lat=0, lon=0, acc=16, title="", tags=""):
    return {"id": pid, "secret": "s", "server": "1", "title": title, "ispublic": 1,
            "description": {"_content": ""}, "tags": tags, "latitude": lat, "longitude": lon,
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

    def nsid(self):
        return "12345@N00"

    def call(self, method, **p):
        self.calls.append((method, p))
        page, per = int(p.get("page", 1)), 2   # tiny pages to exercise pagination
        def paged(items):
            chunk = items[(page - 1) * per: page * per]
            return chunk, max(1, -(-len(items) // per))
        if method == "flickr.people.getPhotos":
            chunk, pages = paged(self.photos)
            return {"photos": {"photo": chunk, "page": page, "pages": pages}}
        if method == "flickr.photosets.getList":
            items = [{"id": k, "title": {"_content": v["title"]}, "photos": len(v["photos"]), "videos": 0}
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
            return {}
        if method == "flickr.photos.licenses.getInfo":
            return {"licenses": {"license": [{"id": 0, "name": "All Rights Reserved"}]}}
        if method == "flickr.stats.getTotalViews":
            return {"stats": {"total": {"views": 1234}}}
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
            ph = next(x for x in self.photos if x["id"] == p["photo_id"])
            return {"photo": {"title": {"_content": ph["title"]}, "description": ph["description"],
                              "tags": {"tag": [{"_content": t} for t in ph["tags"].split()]},
                              "dates": {"taken": ph["datetaken"], "takenunknown": "0", "lastupdate": "1"}}}
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
        if method == "flickr.photos.addTags":
            for ph in self.photos:
                if ph["id"] == p["photo_id"]:
                    ph["tags"] = (ph["tags"] + " " + p["tags"]).strip()
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


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict(CFG)
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        sync(self.fl, self.store, log=lambda *a: None)
        self.vision = FakeVision("m1")
        vcfg = self.cfg["vision"]
        todo = pending_photos(self.store, vcfg, fingerprint(vcfg, self.vision.key))
        classify_photos(self.store, self.vision, vcfg, todo, log=lambda *a: None,
                        downloader=lambda url: (b"jpg", "image/jpeg"))

    def plan(self):
        return build_plan(self.cfg, self.store, self.vision.key, geocoder=fake_geocoder)

    def test_sync_paginates(self):
        self.assertEqual(len(self.store.photos()), 5)
        self.assertEqual(len(self.store.albums()), 6)
        self.assertEqual(self.store.memberships()["2"], {"101"})

    def test_plan_contents(self):
        rows = self.plan()
        albums = {(r.photo_id, r.target) for r in rows if r.action == "add_to_album"}
        self.assertIn(("1", "Japan"), albums)
        self.assertIn(("1", "Food"), albums)
        self.assertIn(("2", "Austin"), albums)
        self.assertIn(("2", "United States"), albums)
        self.assertNotIn(("2", "Texas"), albums)          # already a member
        self.assertIn(("3", "Texas"), albums)              # accuracy 6 is enough for state
        self.assertNotIn(("3", "Austin"), albums)          # Houston, and too coarse for city
        self.assertFalse(any(r.photo_id == "4" for r in rows if r.source.startswith("geo")))
        self.assertFalse(any(t == "Landscapes" for _, t in albums))  # below min_confidence
        tags = {r.photo_id: r.target for r in rows if r.action == "add_tags"}
        self.assertEqual(tags["1"], "food | ramen | noodle soup")
        self.assertNotIn("5", tags)                        # 'food' tag exists, low score

    def test_apply_and_incremental(self):
        rows = self.plan()
        path = os.path.join(self.tmp, "plan.csv")
        write_plan_csv(rows, path)
        write_report(rows, os.path.join(self.tmp, "plan.html"), "plan.csv")
        # Reviewer rejects photo 3 → Texas, and drops the "noodle soup" tag.
        edited = read_plan_csv(path)
        for r in edited:
            if r["photo_id"] == "3" and r["target"] == "Texas":
                r["approve"] = "n"
            if r["action"] == "add_tags" and r["photo_id"] == "1":
                r["target"] = "food | ramen"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=edited[0].keys()); w.writeheader(); w.writerows(edited)

        stats = apply_plan(self.fl, self.store, path, log=lambda *a: None)
        self.assertEqual(stats["failed"], 0)
        self.assertIn("1", self.fl.sets["102"]["photos"])      # Food
        self.assertIn("2", self.fl.sets["103"]["photos"])      # Austin
        self.assertNotIn("3", self.fl.sets["101"]["photos"])   # rejected
        self.assertIn("ramen", self.fl.photos[0]["tags"])
        self.assertNotIn("noodle", self.fl.photos[0]["tags"])

        # Next run: re-sync, re-plan → nothing applied or rejected comes back.
        sync(self.fl, self.store, log=lambda *a: None)
        again = self.plan()
        self.assertEqual(again, [], f"unexpected rows: {[(r.photo_id, r.target) for r in again]}")

    def test_new_photo_is_incremental(self):
        path = os.path.join(self.tmp, "p.csv")
        write_plan_csv(self.plan(), path)
        apply_plan(self.fl, self.store, path, log=lambda *a: None)
        self.fl.photos.append(photo("6", 35.0, 135.7, title="Ramen in Kyoto"))
        sync(self.fl, self.store, log=lambda *a: None)
        vcfg = self.cfg["vision"]
        todo = pending_photos(self.store, vcfg, fingerprint(vcfg, self.vision.key))
        self.assertEqual([p["id"] for p in todo], ["6"])   # only the new one gets classified
        classify_photos(self.store, self.vision, vcfg, todo, log=lambda *a: None,
                        downloader=lambda url: (b"jpg", "image/jpeg"))
        self.assertEqual({r.photo_id for r in self.plan()}, {"6"})

    def test_create_missing_album(self):
        self.cfg["albums"]["create_missing"] = True
        self.cfg["geo"]["cities"].append({"album": "Houston", "lat": 29.76, "lon": -95.37, "radius_km": 30})
        self.fl.photos[2]["accuracy"] = 16
        sync(self.fl, self.store, log=lambda *a: None)
        rows = [r for r in self.plan() if r.target == "Houston"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].album_id, "")
        path = os.path.join(self.tmp, "c.csv")
        write_plan_csv(rows, path)
        stats = apply_plan(self.fl, self.store, path, log=lambda *a: None)
        self.assertEqual(stats["albums_created"], 1)
        self.assertTrue(any(s["title"] == "Houston" and "3" in s["photos"] for s in self.fl.sets.values()))

    def test_dry_run_changes_nothing(self):
        path = os.path.join(self.tmp, "d.csv")
        write_plan_csv(self.plan(), path)
        before = len(self.fl.calls)
        apply_plan(None, self.store, path, dry_run=True, log=lambda *a: None)
        self.assertEqual(len(self.fl.calls), before)
        self.assertEqual(self.store.counts()["applied"], 0)

    def test_changes_applied_during_a_sync_survive_it(self):
        store, fl = self.store, self.fl
        real_call = fl.call

        def call(method, **p):   # the review tool applies a description while the sync runs
            out = real_call(method, **p)
            if method == "flickr.people.getPhotos" and int(p.get("page", 1)) == 1:
                store.record("1", "set_description", "Written mid-sync", "applied")
                store.record("1", "add_tag", "midsync", "applied")
            return out
        fl.call = call
        sync(fl, store, log=lambda *a: None)
        self.assertEqual(store.photo("1")["description"], "Written mid-sync")
        self.assertIn("midsync", store.photo("1")["tags"].split())

    def test_fingerprint_changes_with_categories(self):
        vcfg = self.cfg["vision"]
        a = fingerprint(vcfg, "claude:x")
        vcfg["categories"]["food"]["description"] = "food, more specific"
        self.assertNotEqual(a, fingerprint(vcfg, "claude:x"))


class RenameTest(unittest.TestCase):
    def setUp(self):
        import flickr_organizer.rename as rename_mod
        self._orig = rename_mod.country_names
        rename_mod.country_names = lambda cc: {"AO": ["Angola"], "US": ["United States"]}.get(cc, [cc])
        self.rename_mod = rename_mod
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "enabled": False},
                              "rename": {"enabled": True}})
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        self.fl.photos[1]["title"] = "IMG_1234"                       # Austin
        self.fl.photos.append(photo("7", -8.84, 13.23, title="DSC01234.JPG"))   # Luanda
        self.fl.photos.append(photo("8", -8.84, 13.23, title="Sunset over the bay"))
        sync(self.fl, self.store, log=lambda *a: None)

    def tearDown(self):
        self.rename_mod.country_names = self._orig

    def renames(self):
        rows = build_plan(self.cfg, self.store, None, geocoder=fake_geocoder)
        return {r.photo_id: r.target for r in rows if r.action == "set_title"}

    def test_proposes_location_titles(self):
        got = self.renames()
        self.assertEqual(got["2"], "Austin, TX")        # IMG_1234, configured city
        self.assertEqual(got["5"], "Austin, TX")        # blank title
        self.assertEqual(got["7"], "Luanda, Angola")    # DSC01234.JPG, international
        self.assertNotIn("1", got)                      # "Ramen in Tokyo" is a real title
        self.assertNotIn("8", got)
        self.assertNotIn("3", got)                      # region-level accuracy only
        self.assertNotIn("4", got)                      # no geotag

    def test_region_fallback(self):
        self.cfg["rename"]["fallback_to_region"] = True
        self.assertEqual(self.renames()["3"], "Texas")

    def test_disabled_by_default(self):
        self.cfg["rename"]["enabled"] = False
        self.assertEqual(self.renames(), {})

    def test_apply_reject_and_manual_edit(self):
        rows = build_plan(self.cfg, self.store, None, geocoder=fake_geocoder)
        path = os.path.join(self.tmp, "r.csv")
        write_plan_csv(rows, path)
        edited = read_plan_csv(path)
        for r in edited:
            if r["action"] == "set_title" and r["photo_id"] == "5":
                r["approve"] = "n"
            if r["action"] == "set_title" and r["photo_id"] == "7":
                r["target"] = "Luanda waterfront"           # reviewer tweaks the title
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=edited[0].keys()); w.writeheader(); w.writerows(edited)
        # Meanwhile photo 2 gets retitled by hand on Flickr.
        self.fl.photos[1]["title"] = "My street"
        apply_plan(self.fl, self.store, path, log=lambda *a: None)
        titles = {p["id"]: p["title"] for p in self.fl.photos}
        self.assertEqual(titles["7"], "Luanda waterfront")
        self.assertEqual(titles["5"], "")                   # rejected
        self.assertEqual(titles["2"], "My street")          # changed since plan → left alone
        sync(self.fl, self.store, log=lambda *a: None)
        again = self.renames()
        self.assertNotIn("7", again)
        self.assertNotIn("5", again)                        # rejection remembered

    def test_review_page_has_renames(self):
        rows = build_plan(self.cfg, self.store, None, geocoder=fake_geocoder)
        out = os.path.join(self.tmp, "r.html")
        write_report(rows, out, "r.csv")
        self.assertIn("Luanda, Angola", open(out).read())


class DescribeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict({**CFG, "geo": {**CFG["geo"], "enabled": False},
                              "vision": {**CFG["vision"], "extra_tags": {"enabled": False},
                                         "describe": {"enabled": True}}})
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        self.fl.photos[1]["description"] = {"_content": "Written by me"}
        self.fl.photos[0]["title"] = "Ramen in Tokyo"
        sync(self.fl, self.store, log=lambda *a: None)
        self.vision = FakeVision("m1")
        self.classify()

    def classify(self):
        vcfg = self.cfg["vision"]
        todo = pending_photos(self.store, vcfg, fingerprint(vcfg, self.vision.key))
        classify_photos(self.store, self.vision, vcfg, todo, log=lambda *a: None,
                        downloader=lambda url: (b"jpg", "image/jpeg"))
        return todo

    def descriptions(self):
        rows = build_plan(self.cfg, self.store, self.vision.key)
        return {r.photo_id: r.target for r in rows if r.action == "set_description"}

    def test_only_blank_descriptions(self):
        got = self.descriptions()
        self.assertEqual(got["1"], "A steaming bowl of ramen.")
        self.assertNotIn("2", got)                          # has your own description
        self.assertEqual(set(got), {"1", "3", "4", "5"})

    def test_disabled_by_default_and_cache_kept(self):
        self.cfg["vision"]["describe"]["enabled"] = False
        self.assertEqual(self.descriptions(), {})
        self.assertEqual(self.classify(), [])               # toggling off doesn't re-classify

    def test_enabling_later_reclassifies_once(self):
        self.store.db.execute("DELETE FROM classifications")
        self.cfg["vision"]["describe"]["enabled"] = False
        self.classify()
        self.cfg["vision"]["describe"]["enabled"] = True
        self.assertEqual(len(self.classify()), 5)           # old results had no description
        self.assertEqual(self.classify(), [])

    def test_apply_edit_reject_and_keep_title(self):
        path = os.path.join(self.tmp, "d.csv")
        write_plan_csv(build_plan(self.cfg, self.store, self.vision.key), path)
        write_report(build_plan(self.cfg, self.store, self.vision.key), os.path.join(self.tmp, "d.html"), "d.csv")
        edited = read_plan_csv(path)
        for r in edited:
            if r["action"] != "set_description":
                continue
            if r["photo_id"] == "1":
                r["target"] = "Tonkotsu ramen."
            if r["photo_id"] == "3":
                r["approve"] = "n"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=edited[0].keys()); w.writeheader(); w.writerows(edited)
        self.fl.photos[3]["description"] = {"_content": "Wrote my own meanwhile"}   # on Flickr, after the plan
        apply_plan(self.fl, self.store, path, log=lambda *a: None)
        by_id = {p["id"]: p for p in self.fl.photos}
        self.assertEqual(by_id["1"]["description"]["_content"], "Tonkotsu ramen.")
        self.assertEqual(by_id["1"]["title"], "Ramen in Tokyo")      # title sent back unchanged
        self.assertEqual(by_id["3"]["description"]["_content"], "")  # rejected
        self.assertEqual(by_id["4"]["description"]["_content"], "Wrote my own meanwhile")  # not overwritten
        self.assertEqual(by_id["5"]["description"]["_content"], "Something else.")
        sync(self.fl, self.store, log=lambda *a: None)
        self.assertEqual(self.descriptions(), {})           # applied, rejected, or now has one


    def test_rebuild_rewords_descriptions_this_tool_wrote(self):
        path = os.path.join(self.tmp, "d.csv")
        write_plan_csv([r for r in build_plan(self.cfg, self.store, self.vision.key) if r.photo_id == "1"], path)
        apply_plan(self.fl, self.store, path, log=lambda *a: None)
        self.store.db.execute("UPDATE decisions SET at = at - 10")             # applied a while ago
        self.store.db.commit()
        sync(self.fl, self.store, log=lambda *a: None)
        self.assertEqual(self.descriptions(), {"3": "Something else.", "4": "Something else.", "5": "Something else."})
        self.cfg["vision"]["describe"]["instructions"] = "Write it like a haiku, {max_words} words."
        self.classify()                                                       # the brief changed: re-run
        rows = {r.photo_id: r for r in build_plan(self.cfg, self.store, self.vision.key)
                if r.action == "set_description"}
        self.assertEqual(rows["1"].target, "Steam curls over the ramen.")
        self.assertEqual(rows["1"].original_target, "A steaming bowl of ramen.")   # what it replaces
        self.assertNotIn("2", rows)                                           # "Written by me" is yours
        write_plan_csv([rows["1"]], path)
        apply_plan(self.fl, self.store, path, log=lambda *a: None)
        self.assertEqual(self.fl.photos[0]["description"]["_content"], "Steam curls over the ramen.")
        # You edit it on Flickr afterwards: it's yours now, even after another brief change.
        self.fl.photos[0]["description"] = {"_content": "Tonkotsu at midnight."}
        sync(self.fl, self.store, log=lambda *a: None)
        self.cfg["vision"]["describe"]["instructions"] = "Write it like a haiku again, {max_words} words."
        self.classify()
        self.assertNotIn("1", self.descriptions())

    def test_retitle_and_describe_share_one_call(self):
        import flickr_organizer.rename as rename_mod
        orig, rename_mod.country_names = rename_mod.country_names, lambda cc: ["United States"]
        self.addCleanup(setattr, rename_mod, "country_names", orig)
        self.cfg["rename"]["enabled"] = True
        rows = build_plan(self.cfg, self.store, self.vision.key, geocoder=fake_geocoder)
        self.assertEqual({r.action for r in rows if r.photo_id == "5"}, {"set_title", "set_description"})
        path = os.path.join(self.tmp, "m.csv")
        write_plan_csv(rows, path)
        before = len(self.fl.calls)
        apply_plan(self.fl, self.store, path, log=lambda *a: None)
        calls = [p for m, p in self.fl.calls[before:] if m == "flickr.photos.setMeta" and p["photo_id"] == "5"]
        self.assertEqual(calls, [{"photo_id": "5", "title": "Austin, TX", "description": "Something else."}])
        self.assertEqual(self.store.decisions()[("5", "set_title", "Austin, TX")], "applied")

class StandardTagsTest(unittest.TestCase):
    def setUp(self):
        import flickr_organizer.standard_tags as std_mod
        orig, std_mod.country_names = std_mod.country_names, lambda cc: {"US": ["United States"], "JP": ["Japan"]}[cc]
        self.addCleanup(setattr, std_mod, "country_names", orig)
        self.tmp = tempfile.mkdtemp()
        on = {"enabled": True}
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "enabled": False}, "standard_tags": {
            "date": on, "coords": on, "album_tags": {"enabled": True, "albums": {"Texas": ["lonestar"]}},
            "place": {"enabled": True, "extra": {"Texas": ["southwest"]}},
            "camera": {"enabled": True, "rules": [{"match": "iphone", "tags": ["mobile"]}],
                       "albums": [{"match": "^nikon", "album": "Landscapes"}]},
            "owner": {"enabled": True, "tags": ["me"]}, "filler": {"enabled": True, "tags": ["photo"]}}})
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        self.fl.photos[0]["datetaken"] = "2019-07-17 19:03:25"
        self.fl.photos[2]["tags"] = "2011 summer"                  # already dated, older style
        self.fl.photos[3]["datetakenunknown"] = "1"
        sync(self.fl, self.store, log=lambda *a: None)
        self.stats = fetch_cameras(self.fl, self.store, pending_cameras(self.store), log=lambda *a: None)

    def plan(self):
        return build_plan(self.cfg, self.store, None, geocoder=fake_geocoder)

    def tags(self):
        return {r.photo_id: r.target.split(" | ") for r in self.plan() if r.action == "add_tags"}

    def test_camera_fetch_is_cached(self):
        self.assertEqual(self.stats, {"ok": 2, "no_camera": 3, "failed": 0})
        self.assertEqual(pending_cameras(self.store), [])     # permission-denied is cached too
        self.assertEqual(self.store.cameras()["2"], "Apple iPhone X")
        row = self.store.db.execute("SELECT lens, exif FROM cameras WHERE photo_id='1'").fetchone()
        self.assertEqual(row["lens"], "28-300mm")
        self.assertEqual(json.loads(row["exif"])["ExposureTime"], "1/250 sec")   # readable form preferred

    def test_all_groups_in_priority_order(self):
        t = self.tags()
        self.assertEqual(t["1"], ["y2019", "m07", "d17", "japan", "asia", "tokyo", "nikon", "d90",
                                  "lat360", "lon1400", "me", "photo"])
        # Austin: configured city name, Texas album tag (already a member), state extras.
        self.assertEqual(t["2"], ["y2024", "m01", "d01", "unitedstates", "americas", "northamerica", "texas",
                                  "tx", "austin", "southwest", "apple", "iphone", "x", "mobile", "lonestar",
                                  "lat300", "lon980", "me", "photo"])
        self.assertNotIn("y2011", t["3"])                    # skip_if_dated
        self.assertNotIn("houston", t["3"])                  # accuracy 6: no city tag
        self.assertEqual(t["4"], ["me", "photo"])            # no geo, date unknown, no camera

    def test_camera_albums_and_cap(self):
        albums = {(r.photo_id, r.target) for r in self.plan() if r.action == "add_to_album"}
        self.assertIn(("1", "Landscapes"), albums)
        self.cfg["standard_tags"]["max_tags_per_photo"] = 4
        t = self.tags()
        self.assertEqual(t["1"], ["y2019", "m07", "d17", "japan"])
        self.assertEqual(t["3"], ["unitedstates", "americas"])   # its 2 existing tags leave room for 2

    def test_disabled_by_default(self):
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "enabled": False}})
        self.assertEqual(self.tags(), {})


class QueueReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "describe": {"enabled": True}}})
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        sync(self.fl, self.store, log=lambda *a: None)
        self.vision = FakeVision("m1")
        self.stats = self.process()
        self.app = ReviewApp(self.store, self.fl)

    def decide(self, pid, choices):
        """Approve/reject returns at once; the worker applies in the background."""
        self.assertEqual(self.app.decide(pid, choices), {"status": "applying"})
        self.app.wait()
        item = self.store.queue_item(pid)
        return {"status": item["status"], **item["result"]}

    def process(self, **kw):
        return process(self.cfg, self.store, self.fl, self.vision, geocoder=fake_geocoder, log=lambda *a: None,
                       downloader=lambda url: (b"jpg", "image/jpeg"), **kw)

    def changes(self, pid):
        return {(c["action"], c["target"]) for c in self.store.queue_item(pid)["changes"]}

    def test_queue_is_built_per_photo_and_resumable(self):
        self.assertEqual(self.stats["queued"] + self.stats["nothing_to_change"], 5)
        self.assertEqual(self.store.camera_ids(), {"1", "2", "3", "4", "5"})      # EXIF fetched for each
        self.assertIn(("add_to_album", "Food"), self.changes("1"))
        self.assertIn(("set_description", "A steaming bowl of ramen."), self.changes("1"))
        self.assertEqual(self.process()["queued"], 0)                            # nothing re-done
        item = self.app.item("1")
        self.assertEqual(item["photo"]["title"], "Ramen in Tokyo")
        self.assertTrue(item["photo"]["image"].endswith("1_s_b.jpg"))

    def test_approve_one_by_one_applies_and_records(self):
        changes = self.store.queue_item("1")["changes"]
        choices = []
        for c in changes:
            if c["action"] == "add_tags":
                choices.append({"approve": True, "target": "food | ramen"})          # dropped "noodle soup"
            elif c["action"] == "set_description":
                choices.append({"approve": True, "target": "Tonkotsu ramen."})
            elif c["target"] == "Japan":
                choices.append({"approve": False})
            else:
                choices.append({"approve": True})
        r = self.decide("1", choices)
        self.assertEqual(r["status"], "applied")
        self.assertIn("1", self.fl.sets["102"]["photos"])                          # Food
        self.assertNotIn("1", self.fl.sets["100"]["photos"])                       # Japan rejected
        self.assertIn("ramen", self.fl.photos[0]["tags"])
        self.assertNotIn("noodle", self.fl.photos[0]["tags"])
        self.assertEqual(self.fl.photos[0]["description"]["_content"], "Tonkotsu ramen.")
        tags = next(o for o in r["changes"] if o["action"] == "add_tags")["tags"]
        self.assertEqual(tags, {"food": "applied", "ramen": "applied", "noodle soup": "rejected"})
        self.assertEqual(self.app.summary()["applied"], 1)
        # Re-planning later doesn't bring back anything applied or rejected.
        self.process(rebuild=True, photo_ids=["1"])
        self.assertEqual(self.store.queue_item("1")["status"], "none")

    def test_reject_all_and_no_overwrite_of_flickr_edits(self):
        self.decide("3", [{"approve": False} for _ in self.store.queue_item("3")["changes"]])
        self.assertEqual(self.store.queue_item("3")["status"], "rejected")
        # Photo 5's description gets written on Flickr after the queue was built.
        self.fl.photos[4]["description"] = {"_content": "Mine"}
        r = self.decide("5", [{"approve": True} for _ in self.store.queue_item("5")["changes"]])
        self.assertEqual(self.fl.photos[4]["description"]["_content"], "Mine")      # re-read before setMeta
        desc = next(o for o in r["changes"] if o["action"] == "set_description")
        self.assertEqual(desc["status"], "rejected")
        self.assertEqual([j["photo_id"] for j in self.app.jobs_status()["finished"]], ["3", "5"])

    def test_process_unprocessed_video_from_viewer(self):
        video = photo("6", 35.0, 135.7, title="Ramen in Kyoto")
        video["media"] = "video"
        self.fl.photos.append(video)
        sync(self.fl, self.store, log=lambda *a: None)
        app = ReviewApp(self.store, self.fl, cfg=self.cfg, provider=lambda: self.vision,
                        process_kw={"geocoder": fake_geocoder, "downloader": lambda url: (b"jpg", "image/jpeg")})
        self.assertEqual(app.queue("unprocessed"), ["6"])
        self.assertEqual(app.item("6")["status"], "unprocessed")
        self.assertEqual(app.find("https://www.flickr.com/photos/someone/6/in/album-1"), "6")
        self.assertIsNone(app.find("https://www.flickr.com/photos/someone/999"))
        self.assertEqual(app.process("6"), {"status": "processing"})
        app.wait()
        item = app.item("6")
        self.assertEqual(item["status"], "pending")                     # classified despite include_videos: false
        self.assertIn(("add_to_album", "Food"), {(c["action"], c["target"]) for c in item["changes"]})
        self.assertEqual(app.recent[-1]["kind"], "process")
        self.assertEqual(app.summary()["unprocessed"], 0)
        # Re-processing an already-queued photo works too, and changes nothing that's settled.
        app.process("6"); app.wait()
        self.assertEqual(app.item("6")["status"], "pending")

    def test_rebuild_videos_only_and_never_while_applying(self):
        self.fl.photos[0]["media"] = "video"                           # photo 1 becomes a video
        sync(self.fl, self.store, log=lambda *a: None)
        self.store.queue_finish("2", "applying", {"choices": []})      # mid-apply in the viewer
        self.cfg["vision"]["include_videos"] = True
        before = self.store.queue_item("3")["built_at"]
        stats = self.process(rebuild=True, media="video")
        self.assertEqual(stats["queued"] + stats["nothing_to_change"], 1)   # only the video
        self.assertEqual(self.store.queue_item("3")["built_at"], before)
        self.assertEqual(self.process(rebuild=True)["queued"] + self.process(rebuild=True)["nothing_to_change"], 4)
        self.assertEqual(self.store.queue_item("2")["status"], "applying")  # left alone

    def test_unfinished_jobs_resume_on_restart(self):
        # Choices recorded, tool closed before the worker got to it.
        quiet = ReviewApp(self.store, self.fl, background=False)
        self.store.queue_finish("1", "applying", {"choices": [{"approve": True}] * len(self.store.queue_item("1")["changes"])})
        self.assertEqual(quiet.summary()["applying"], 1)
        ReviewApp(self.store, self.fl).wait()                          # a new session picks it up
        self.assertEqual(self.store.queue_item("1")["status"], "applied")
        self.assertIn("1", self.fl.sets["102"]["photos"])


class SocialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        for ph, (views, faves, comments) in zip(self.fl.photos, [(100, 60, 1), (5, 0, 0), (7, 0, 2)]):
            ph.update(views=str(views), count_faves=str(faves), count_comments=str(comments))
        sync(self.fl, self.store, log=lambda *a: None)
        self.quiet = lambda *a: None

    def test_counts_and_daily_snapshot_from_sync(self):
        p = self.store.photo("1")
        self.assertEqual((p["views"], p["faves"], p["comments"]), (100, 60, 1))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM photo_stats").fetchone()[0], 3)
        self.assertIn("1234", self.store.db.execute("SELECT stats FROM account_stats").fetchone()[0])

    def test_viewer_shows_engagement_and_sorts_by_views(self):
        self.fl.photos[0]["license"] = "0"
        sync(self.fl, self.store, log=lambda *a: None)
        social.fetch_groups(self.fl, self.store, log=self.quiet)
        social.fetch_comments(self.fl, self.store, ["1"], log=self.quiet)
        for pid in ("1", "2", "3"):
            self.store.queue_put(pid, "pending", [])
        app = ReviewApp(self.store, self.fl, background=False)
        st = app.item("1")["stats"]
        self.assertEqual((st["views"], st["faves"], st["comments"], st["license"]), (100, 60, 1, "All Rights Reserved"))
        self.assertEqual([g["name"] for g in st["groups"]], ["Ramen Lovers"])
        self.assertEqual(st["recent_comments"][0]["text"], "Nice!")
        self.assertEqual(st["first_snapshot"]["views"], 100)
        self.assertEqual(app.queue("pending", "views"), ["1", "3", "2"])

    def test_groups_comments_faves_fetched_only_when_counts_change(self):
        self.assertEqual(social.fetch_groups(self.fl, self.store, log=self.quiet)["photos_in_pools"], 3)
        self.assertEqual(self.store.photo_groups("1"), ["Ramen Lovers"])
        todo = social.plan(self.store)
        self.assertEqual(sorted(todo["comments"]), ["1", "3"])
        self.assertEqual(todo["faves"], ["1"])
        social.fetch_comments(self.fl, self.store, todo["comments"], log=self.quiet)
        social.fetch_faves(self.fl, self.store, todo["faves"], log=self.quiet)
        self.assertEqual(self.store.db.execute("SELECT text FROM comments WHERE photo_id='1'").fetchone()[0], "Nice!")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM faves").fetchone()[0], 60)   # both pages
        self.assertEqual(social.plan(self.store), {"comments": [], "faves": []})              # nothing changed
        self.fl.photos[0]["count_comments"] = "2"                                              # a new comment
        sync(self.fl, self.store, log=lambda *a: None)
        self.assertEqual(social.plan(self.store)["comments"], ["1"])


class TitleTest(unittest.TestCase):
    def setUp(self):
        import flickr_organizer.rename as rename_mod
        orig, rename_mod.country_names = rename_mod.country_names, lambda cc: {"US": ["United States"]}.get(cc, [cc])
        self.addCleanup(setattr, rename_mod, "country_names", orig)
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "title": {"enabled": True},
                                                "describe": {"enabled": True}}})
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        self.fl.photos[2].update(title="Houston, TX", accuracy=16)      # place-only
        self.fl.photos[3]["title"] = "Bellagio 175"                     # numbered place, no geotag
        self.fl.photos[4]["title"] = "Rufus at the park"                # yours
        sync(self.fl, self.store, log=lambda *a: None)
        self.vision = FakeVision("m1")
        self.classify()

    def classify(self):
        vcfg = self.cfg["vision"]
        todo = pending_photos(self.store, vcfg, fingerprint(vcfg, self.vision.key))
        classify_photos(self.store, self.vision, vcfg, todo, log=lambda *a: None,
                        downloader=lambda url: (b"jpg", "image/jpeg"))
        return todo

    def titles(self):
        rows = build_plan(self.cfg, self.store, self.vision.key, geocoder=fake_geocoder)
        return {r.photo_id: (r.target, r.source) for r in rows if r.action == "set_title"}

    def test_proposes_titles_only_for_generic_or_place_titles(self):
        t = self.titles()
        self.assertNotIn("1", t)                                   # "Ramen in Tokyo" is yours
        self.assertEqual(t["2"], ("Quiet Corner", "vision:title"))  # blank
        self.assertEqual(t["3"], ("Quiet Corner", "vision:title"))  # "Houston, TX"
        self.assertIn("4", t)                                      # "Bellagio 175"
        self.assertNotIn("5", t)                                   # "Rufus at the park"
        renamer = __import__("flickr_organizer.rename", fromlist=["Renamer"]).Renamer(self.cfg["rename"], self.cfg["geo"])
        tx = {"cc": "US", "admin1": "Texas", "name": "Pasadena"}
        place_only = lambda title, place=tx: renamer.is_place_only({"title": title}, place)
        self.assertTrue(place_only("Houston, TX"))                  # neighbouring town, right state
        self.assertTrue(place_only("Houston, Texas, United States"))
        self.assertFalse(place_only("Fishing, Galveston, TX"))      # a town after the comma: yours
        self.assertFalse(place_only("Sunset over Galveston Bay"))
        self.assertFalse(place_only("Houston, Japan"))              # wrong country for the geotag
        self.assertTrue(place_only("Foz do Iguac\u0327u 625", None))  # decomposed accent, numbered
        self.cfg["vision"]["title"]["replace"] = "all"
        self.assertEqual(self.titles()["1"], ("Steam Rising, Tokyo", "vision:title"))   # quotes/period stripped
        self.cfg["vision"]["title"]["replace"] = "generic"
        self.assertEqual(set(self.titles()), {"2"})

    def test_model_title_wins_over_location_rename(self):
        self.cfg["rename"]["enabled"] = True
        self.assertEqual(self.titles()["2"][1], "vision:title")
        self.cfg["vision"]["title"]["enabled"] = False
        self.assertEqual(self.titles()["2"], ("Austin, TX", "geo:rename"))

    def test_editing_a_brief_reruns_only_the_writing(self):
        vcfg = self.cfg["vision"]
        prompt = build_prompt(vcfg, self.store.photo("1"))
        self.assertIn("caption beside a print", prompt)
        self.assertIn("never guess a location", prompt)            # grounding always kept
        self.assertEqual(self.classify(), [])
        vcfg["describe"]["instructions"] = "Write {max_words} words like a haiku."
        self.assertIn("Write 40 words like a haiku.", build_prompt(vcfg, self.store.photo("1")))
        self.assertIn("never guess a location", build_prompt(vcfg, self.store.photo("1")))
        self.assertEqual(len(self.classify()), 5)                  # new brief: re-run once
        self.assertEqual(self.classify(), [])
        vcfg["describe"]["enabled"] = vcfg["title"]["enabled"] = False
        self.assertEqual(self.classify(), [])                      # turning them off keeps the cache


class UtilTest(unittest.TestCase):
    def test_norm_title(self):
        self.assertEqual(norm_title("Côte d'Ivoire"), norm_title("cote d ivoire"))
        self.assertEqual(norm_title("  Texas "), "texas")

    def test_same_text_ignores_flickr_escaping(self):
        from flickr_organizer.util import same_text
        self.assertTrue(same_text('A sign reading "Shields" &amp; more', 'A sign reading &quot;Shields&quot; & more '))
        self.assertFalse(same_text("Steam curls", "Tonkotsu at midnight"))

    def test_quote_tags(self):
        self.assertEqual(quote_tags(["food", "noodle soup"]), 'food "noodle soup"')

    def test_parse_json_loose(self):
        self.assertEqual(parse_json_loose('Sure!\n```json\n{"a": 1}\n```'), {"a": 1})

    def test_location_in_prompt(self):
        from flickr_organizer.geo import place_label
        import flickr_organizer.geo as geo_mod
        orig, geo_mod.country_names = geo_mod.country_names, lambda cc: ["United States"]
        self.addCleanup(setattr, geo_mod, "country_names", orig)
        acc = {"country": 1, "state": 6, "city": 11}
        place = {"cc": "US", "admin1": "Washington", "name": "Seattle"}
        self.assertEqual(place_label({"accuracy": 16}, place, acc), "Seattle, Washington, United States")
        self.assertEqual(place_label({"accuracy": 6}, place, acc), "Washington, United States")
        cfg = from_dict(CFG)
        p = {"title": "", "description": "", "tags": ""}
        self.assertIn("geotag (nearest town; approximate): Seattle", build_prompt(cfg["vision"], p, "Seattle"))
        cfg["vision"]["use_location"] = False
        self.assertNotIn("Seattle", build_prompt(cfg["vision"], p, "Seattle"))

    def test_prompt_and_schema(self):
        cfg = from_dict(CFG)
        p = {"title": "Ramen", "description": "", "tags": "japan"}
        prompt = build_prompt(cfg["vision"], p)
        self.assertIn("- food: food", prompt)
        self.assertIn("Existing tags: japan", prompt)
        self.assertIn("tags", build_schema(cfg["vision"])["required"])


if __name__ == "__main__":
    unittest.main()
