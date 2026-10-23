"""Photos without a geotag: guessing where (geoguess), proposing it, applying it, the daily sweep."""
import os
import tempfile
import unittest

from fakes import photo, FakeFlickr, fake_geocoder, FakeVision, CFG
from coconut_barometer import gazetteer
from coconut_barometer.apply import apply_changes
from coconut_barometer.changes import Change
from coconut_barometer.config import from_dict
from coconut_barometer.gazetteer import Gazetteer
from coconut_barometer.geoguess import Guesser
from coconut_barometer.planner import build_plan
from coconut_barometer.review import ReviewApp
from coconut_barometer.review.app import _location
from coconut_barometer.store import Store
from coconut_barometer.sync import sync

TOWNS = [
    {"lat": 35.3658, "lon": -120.8499, "name": "Morro Bay", "admin1": "California", "cc": "US"},
    {"lat": 36.5552, "lon": -121.9233, "name": "Carmel", "admin1": "California", "cc": "US"},
    {"lat": 39.9784, "lon": -86.1180, "name": "Carmel", "admin1": "Indiana", "cc": "US"},
    {"lat": 39.7684, "lon": -86.1580, "name": "Indianapolis", "admin1": "Indiana", "cc": "US"},
    {"lat": 34.0522, "lon": -118.2437, "name": "Los Angeles", "admin1": "California", "cc": "US"},
    {"lat": -6.1349, "lon": 12.3689, "name": "Soyo", "admin1": "Zaire", "cc": "AO"},
    {"lat": -8.8368, "lon": 13.2343, "name": "Luanda", "admin1": "Luanda", "cc": "AO"},
    {"lat": 30.2119, "lon": -92.0560, "name": "Sunset", "admin1": "Louisiana", "cc": "US"},
    {"lat": 41.1364, "lon": -112.0310, "name": "Sunset", "admin1": "Utah", "cc": "US"},
    {"lat": 35.6895, "lon": 139.6917, "name": "Tokyo", "admin1": "Tokyo", "cc": "JP"},
]
GAZ = Gazetteer(TOWNS)


def at(when, **kw):
    return {**photo(kw.pop("pid"), **kw), "datetaken": when}


class GazetteerTest(unittest.TestCase):
    def test_towns_states_and_countries(self):
        self.assertEqual([p.admin1 for p in GAZ.towns("carmel")], ["California", "Indiana"])
        self.assertEqual(GAZ.region("ca")[0].admin1, "California")          # US postal codes too
        self.assertEqual(GAZ.country("angola").cc, "AO")
        self.assertIsNone(GAZ.country("ao"))                                 # never a bare code
        carmel_in = GAZ.towns("carmel")[1]
        self.assertEqual(GAZ.agreement(carmel_in, {"indiana"}), 2)
        self.assertEqual(GAZ.agreement(carmel_in, {"ca"}), -1)               # another state named
        self.assertEqual(GAZ.agreement(carmel_in, {"unitedstates"}), 1)
        self.assertEqual(GAZ.search("Carmel, CA")[0].label, "Carmel, California, United States")

    def test_place_search_falls_back_to_the_towns_offline(self):
        def down(*a, **kw):
            raise ConnectionError("no network")
        r = gazetteer.search("Morro Bay", gazetteer=GAZ, get=down)
        self.assertEqual((r["source"], r["places"][0]["label"]), ("offline", "Morro Bay, California, United States"))

        class Ok:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"display_name": "Red Rocks Amphitheatre, Morrison, Colorado", "lat": "39.665", "lon": "-105.205",
                         "place_rank": 30, "boundingbox": ["39.66", "39.67", "-105.21", "-105.20"]}]
        seen = {}
        r = gazetteer.search("red rocks", get=lambda url, **kw: seen.update(kw) or Ok())
        self.assertEqual(r["source"], "nominatim")
        self.assertEqual((r["places"][0]["accuracy"], r["places"][0]["bbox"][0]), (16, 39.66))
        self.assertIn("coconut-barometer", seen["headers"]["User-Agent"])     # Nominatim's policy


class GuessTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        self.fl = FakeFlickr()
        alone = "2001-01-01 00:00:00"           # no geotagged photo near this time: only their words count
        self.fl.photos = [at("2015-05-15 12:00:00", pid="10", lat=35.37, lon=-120.85),    # Morro Bay, iPhone
                          at("2015-05-15 14:00:00", pid="11", lat=35.40, lon=-120.80),    # 2 h later, iPhone
                          at("2015-05-15 13:00:00", pid="12"),                             # between: no geotag
                          at(alone, pid="13", title="Rockfishing, Morro Bay, CA"),
                          at(alone, pid="14", title="Carmel Beach, CA", tags="carmel unitedstates"),
                          at(alone, pid="15", title="Sunset"),
                          at(alone, pid="16", title="Shore", tags="soyo angola")]
        sync(self.fl, self.store, log=lambda *a: None)
        for pid in ("10", "11", "12"):
            self.store.save_camera(pid, "Apple iPhone 6", "Apple", "iPhone 6")
        self.g = Guesser(self.store, GAZ, fake_geocoder)

    def guess(self, pid, vision=None):
        return self.g.guess(self.store.photo(pid), vision, self.store.cameras().get(pid, ""))

    def test_between_the_same_cameras_photos_before_and_after(self):
        best = self.guess("12")[0]
        self.assertAlmostEqual(best.lat, 35.385, places=3)                  # halfway, in time and so in place
        self.assertEqual(best.sources, ["nearby"])
        self.assertIn("same camera (Apple iPhone 6)", best.reasons[0])
        self.assertEqual(best.confidence, "medium")

    def test_town_in_the_title_with_its_state(self):
        best = self.guess("13")[0]
        self.assertEqual((best.place, best.accuracy, best.confidence), ("Morro Bay, California, United States", 11, "high"))

    def test_a_state_beside_it_rules_out_the_town_of_that_name_elsewhere(self):
        best = self.guess("14")[0]
        self.assertEqual(best.place, "Carmel, California, United States")

    def test_tags_on_everything_say_nothing(self):
        for i in range(30):                    # unitedstates on most photos: it backs nothing up
            self.store.upsert_photos([{**dict(self.store.photo("10")), "id": f"x{i}", "tags": "unitedstates"}])
        self.g = Guesser(self.store, GAZ, fake_geocoder)
        self.assertIn("unitedstates", self.g.common)
        reasons = " ".join(r for g in self.guess("14") for r in g.reasons)
        self.assertNotIn("unitedstates", reasons)

    def test_a_name_many_towns_share_is_a_weak_clue_and_one_word_tags_need_their_country(self):
        self.assertEqual(self.guess("15")[0].confidence, "low")              # "Sunset"
        best = self.guess("16")[0]                                           # tags soyo + angola
        self.assertEqual(best.place, "Soyo, Zaire, Angola")

    def test_the_models_words_count_too(self):
        best = self.guess("15", {"title": "Low Tide, Morro Bay", "tags": ["morro rock"]})[0]
        self.assertEqual((best.place, best.sources), ("Morro Bay, California, United States", ["vision"]))

    def test_no_clue_no_guess(self):
        self.store.update_photo("15", title="IMG_1234")
        self.assertEqual(self.guess("15"), [])


class LocationReviewTest(unittest.TestCase):
    def setUp(self):
        self.cfg = from_dict(CFG)
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        self.fl = FakeFlickr()
        self.fl.photos[3].update(title="Ramen, Tokyo", datetaken="2020-06-01 10:00:00")   # photo 4: no geotag
        sync(self.fl, self.store, log=lambda *a: None)
        self.app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg, provider=lambda: FakeVision("m1"),
                             process_kw={"geocoder": fake_geocoder, "downloader": lambda url: (b"jpg", "image/jpeg")})

    def plan(self, pid="4"):
        return [c for c in build_plan(self.cfg, self.store, None, geocoder=fake_geocoder, photo_ids={pid}, gazetteer=GAZ)
                if c.action == "set_location"]

    def test_proposed_only_without_a_geotag_and_only_when_on(self):
        (c,) = self.plan()
        self.assertEqual(c.new["place"], "Tokyo, Japan")
        self.assertTrue(c.options and c.options[0]["place"] == "Tokyo, Japan")
        self.assertEqual(self.plan("1"), [])                                  # has one
        self.cfg["review"]["missing_location"] = False
        self.assertEqual(self.plan(), [])

    def test_applied_on_flickr_and_locally(self):
        (c,) = self.plan()
        c.edited = {"lat": 35.0, "lon": 135.75, "accuracy": 14}              # you moved the pin
        self.assertEqual(apply_changes(self.fl, self.store, [c], log=lambda *a: None)["applied"], 1)
        self.assertIn(("flickr.photos.geo.setLocation", {"photo_id": "4", "lat": "35.000000", "lon": "135.750000",
                                                         "accuracy": 14}), self.fl.calls)
        p = self.store.photo("4")
        self.assertEqual((p["lat"], p["lon"], p["accuracy"], p["geo_public"]), (35.0, 135.75, 14, 0))   # privacy re-read
        self.assertEqual(self.store.decisions()[("4", "set_location", "35.000000,135.750000,14")], "applied")
        sync(self.fl, self.store, log=lambda *a: None)
        self.assertEqual(self.plan(), [])                                     # it has one now

    def test_a_location_set_on_flickr_since_is_never_overwritten(self):
        (c,) = self.plan()
        self.fl.photos[3].update(latitude=1.5, longitude=2.5, accuracy=16)
        self.assertEqual(apply_changes(self.fl, self.store, [c], log=lambda *a: None)["failed"], 1)
        self.assertFalse(any(m == "flickr.photos.geo.setLocation" for m, _ in self.fl.calls))

    def test_your_pick_from_the_page(self):
        self.assertEqual(_location({"lat": 10, "lon": 200, "accuracy": 30}), {"lat": 10.0, "lon": -160.0, "accuracy": 16, "place": ""})
        self.assertEqual(_location({"lat": 95, "lon": 0}), {})
        self.assertEqual(_location("nowhere"), {})
        self.store.queue_put("4", "pending", [c.to_dict() for c in self.plan()])
        self.assertEqual(self.app.decide("4", [{"approve": True, "value": {"lat": 35.1, "lon": 136.0, "accuracy": 12}}])["status"],
                         "applied")
        self.assertEqual((self.store.photo("4")["lat"], self.store.photo("4")["accuracy"]), (35.1, 12))

    def test_no_place_picked_is_passed_on(self):
        c = Change("set_location", "4", {})
        apply_changes(self.fl, self.store, [c], log=lambda *a: None)
        self.assertEqual(self.store.decisions()[("4", "set_location", "")], "rejected")
        self.assertIsNone(self.store.photo("4")["lat"])

    def test_the_daily_sweep_sends_the_ones_not_in_review(self):
        self.fl.photos.append(photo("8"))                                    # another without one
        out = self.app._sync(self.store, "photo_list")
        self.assertEqual(out["missing_location"], 2)
        item = self.store.queue_item("4")
        self.assertEqual(item["status"], "pending")                          # ready: no vision or Flickr calls
        self.assertIn("set_location", [c["action"] for c in item["changes"]])
        self.assertEqual(self.app._sync(self.store, "photo_list")["missing_location"], 0)   # already there
        self.assertNotIn("missing_location", self.app._sync(self.store, "incremental"))     # the daily sweep only
        # Ignored: its location passed on, so the sweep leaves it out (sending it from the gallery still works).
        self.app.ignore("4")
        self.store.queue_remove("8")
        self.assertEqual(self.app._sync(self.store, "photo_list")["missing_location"], 1)
        self.assertIsNone(self.store.queue_item("4"))
        self.store.queue_remove("8")
        self.cfg["review"]["missing_location"] = False
        self.assertNotIn("missing_location", self.app._sync(self.store, "photo_list"))


if __name__ == "__main__":
    unittest.main()
