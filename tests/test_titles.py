"""Title proposals: location renames and model-written titles."""
import os
import tempfile
import unittest

from fakes import photo, FakeFlickr, fake_geocoder, FakeVision, CFG
from flickr_organizer.apply import apply_changes
from flickr_organizer.classify import build_prompt, classify_photos, fingerprint, pending_photos
from flickr_organizer.config import from_dict
from flickr_organizer.planner import build_plan
from flickr_organizer.store import Store
from flickr_organizer.sync import sync


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
        return {r.photo_id: r.new for r in rows if r.action == "set_title"}

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
        changes = build_plan(self.cfg, self.store, None, geocoder=fake_geocoder)
        for c in changes:
            if c.action == "set_title" and c.photo_id == "5":
                c.approved = False
            if c.action == "set_title" and c.photo_id == "7":
                c.edited = "Luanda waterfront"               # reviewer tweaks the title
        # Meanwhile photo 2 gets retitled by hand on Flickr.
        self.fl.photos[1]["title"] = "My street"
        apply_changes(self.fl, self.store, changes, log=lambda *a: None)
        titles = {p["id"]: p["title"] for p in self.fl.photos}
        self.assertEqual(titles["7"], "Luanda waterfront")
        self.assertEqual(titles["5"], "")                   # rejected
        self.assertEqual(titles["2"], "My street")          # changed since plan → left alone
        sync(self.fl, self.store, log=lambda *a: None)
        again = self.renames()
        self.assertNotIn("7", again)
        self.assertNotIn("5", again)                        # rejection remembered

    def test_edits_are_recorded_against_the_proposal(self):
        changes = {c.photo_id: c for c in build_plan(self.cfg, self.store, None, geocoder=fake_geocoder)
                   if c.action == "set_title"}
        changes["7"].edited = "Luanda waterfront"
        changes["5"].edited, changes["5"].approved = "Something else", False   # edited, then rejected
        apply_changes(self.fl, self.store, [changes["7"], changes["5"]], log=lambda *a: None)
        d = self.store.decisions()
        self.assertEqual(d[("7", "set_title", "Luanda waterfront")], "applied")   # what was written
        self.assertEqual(d[("7", "set_title", "Luanda, Angola")], "edited")       # what was proposed
        self.assertEqual(d[("5", "set_title", "Austin, TX")], "rejected")         # the proposal, not the edit
        self.assertNotIn(("5", "set_title", "Something else"), d)


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
        return {r.photo_id: (r.new, r.source) for r in rows if r.action == "set_title"}

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


if __name__ == "__main__":
    unittest.main()
