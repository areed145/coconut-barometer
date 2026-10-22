"""Title proposals: the model's title for every photo, settled per proposal."""
import os
import tempfile
import unittest

from fakes import FakeFlickr, fake_geocoder, FakeVision, CFG
from coconut_barometer.apply import apply_changes
from coconut_barometer.classify import build_prompt, classify_photos, fingerprint, pending_photos
from coconut_barometer.config import from_dict
from coconut_barometer.planner import build_plan
from coconut_barometer.store import Store
from coconut_barometer.sync import sync


class TitleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict(CFG)
        self.store = Store(os.path.join(self.tmp, "db.duckdb"))
        self.fl = FakeFlickr()
        self.fl.photos[1]["title"] = "IMG_1234"
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

    def plan(self):
        return [r for r in build_plan(self.cfg, self.store, self.vision.key, geocoder=fake_geocoder)
                if r.action == "set_title"]

    def titles(self):
        return {r.photo_id: (r.new, r.source) for r in self.plan()}

    def test_every_photo_gets_the_models_title(self):
        t = self.titles()
        self.assertEqual(t["1"], ("Steam Rising, Tokyo", "vision:title"))   # "Ramen in Tokyo"; quotes/period stripped
        self.assertEqual(t["2"], ("Quiet Corner", "vision:title"))          # IMG_1234
        self.assertEqual(t["5"], ("Quiet Corner", "vision:title"))          # "Rufus at the park": you decide
        self.assertEqual(set(t), {"1", "2", "3", "4", "5"})
        self.assertEqual(build_plan(self.cfg, self.store, None, geocoder=fake_geocoder) and
                         [r for r in build_plan(self.cfg, self.store, None, geocoder=fake_geocoder)
                          if r.action == "set_title"], [])                  # no model result, no title

    def test_apply_reject_and_manual_edit(self):
        changes = {c.photo_id: c for c in self.plan()}
        changes["3"].approved = False
        changes["4"].edited = "Corner café"                                 # tweaked in review
        self.fl.photos[1]["title"] = "My street"                            # retitled on Flickr meanwhile
        apply_changes(self.fl, self.store, [c for pid, c in changes.items() if pid != "5"], log=lambda *a: None)
        titles = {p["id"]: p["title"] for p in self.fl.photos}
        self.assertEqual(titles["4"], "Corner café")
        self.assertEqual(titles["3"], "")                                   # rejected
        self.assertEqual(titles["2"], "My street")                          # changed since plan: left alone
        d = self.store.decisions()
        self.assertEqual(d[("4", "set_title", "Corner café")], "applied")   # what was written
        self.assertEqual(d[("4", "set_title", "Quiet Corner")], "edited")   # what was proposed
        self.assertEqual(d[("3", "set_title", "Quiet Corner")], "rejected")
        sync(self.fl, self.store, log=lambda *a: None)
        again = self.titles()
        self.assertNotIn("3", again)                                        # rejection remembered
        self.assertNotIn("4", again)                                        # edit counts as dealt with
        self.assertNotIn("1", again)                                        # applied
        self.assertNotIn("2", again)                                        # you retitled it yourself: declined
        self.assertIn("5", again)                                           # not decided yet: still proposed

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

    def test_fingerprints_survive_the_retired_switches(self):
        """Results from before the switches were removed stay valid (no re-run of the library)."""
        vcfg = from_dict({"vision": {"provider": "claude", "model": "claude-haiku-5-5", "min_confidence": 0.6,
                                     "extra_tags": {"max": 8}}})["vision"]
        vcfg["categories"] = {"food": {"description": "food"}}
        from coconut_barometer.classify import text_fingerprint
        self.assertEqual(text_fingerprint(vcfg), text_fingerprint(from_dict({"vision": {
            "describe": {"enabled": True, "reword": True}, "title": {"enabled": True, "replace": "generic_or_place"}}})["vision"]))
        import hashlib, json
        legacy = json.dumps({"describe": {"enabled": True, "instructions": None, "max_words": 40, "reword": True},
                             "title": {"enabled": True, "instructions": None, "max_words": 8,
                                       "replace": "generic_or_place"}}, sort_keys=True)
        self.assertEqual(text_fingerprint(vcfg), hashlib.sha1(legacy.encode()).hexdigest()[:12])


if __name__ == "__main__":
    unittest.main()
