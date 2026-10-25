"""Re-uploads (duplicates.py): a new upload that looks like a photo already in the library is held in review,
to keep (then reviewed as usual) or delete from Flickr."""
import io
import os
import random
import tempfile
import unittest

from PIL import Image

from fakes import photo, FakeFlickr, fake_geocoder, FakeVision, CFG
from coconut_barometer import local
from coconut_barometer.config import from_dict
from coconut_barometer.duplicates import phash
from coconut_barometer.review import ReviewApp
from coconut_barometer.store import Store


def picture(seed, size=(320, 240), quality=90):
    """A blocky random picture: each seed a different one."""
    rnd = random.Random(seed)
    small = Image.new("L", (8, 6))
    small.putdata([rnd.randrange(256) for _ in range(48)])
    out = io.BytesIO()
    small.resize(size, Image.Resampling.BILINEAR).convert("RGB").save(out, "JPEG", quality=quality)
    return out.getvalue()


class PhashTest(unittest.TestCase):
    def test_a_resized_recompressed_copy_is_close_and_another_picture_isnt(self):
        a = phash(picture(1))
        self.assertLessEqual((a ^ phash(picture(1, size=(75, 56), quality=60))).bit_count(), 6)
        self.assertGreater((a ^ phash(picture(2))).bit_count(), 6)


class DuplicateUploadTest(unittest.TestCase):
    def setUp(self):
        self.cfg = from_dict({**CFG, "review": {"missing_location": False}})
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        self.fl = FakeFlickr()
        self.images = {}            # thumbnail url -> bytes; each library photo its own picture
        self.fetched = []
        for i, ph in enumerate(self.fl.photos):
            self.images[ph["url_m"]] = picture(100 + i)
        self.app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg, provider=lambda: FakeVision("m1"),
                             process_kw={"geocoder": fake_geocoder, "downloader": self.download})
        self.app._sync(self.store, "full")

    def download(self, url):
        self.fetched.append(url)
        if url not in self.images:
            raise OSError("404")
        return self.images[url], "image/jpeg"

    def upload(self, pid, image, at=1700000500):
        ph = {**photo(pid, title="IMG_9"), "dateupload": str(at)}
        self.images[ph["url_m"]] = image
        self.fl.photos.append(ph)
        return self.app._sync(self.store, "full")

    def test_a_copy_of_a_library_photo_is_held_with_its_match(self):
        self.upload("9", picture(100, size=(75, 56), quality=60))         # photo 1, again
        item = self.app.item("9")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["changes"], [])
        self.assertEqual(item["duplicate"]["photo_id"], "1")
        self.assertEqual(item["duplicate"]["url"], "https://www.flickr.com/photos/12345@N00/1")

    def test_a_new_picture_is_reviewed_as_usual_and_fingerprints_are_kept(self):
        self.upload("9", picture(999))
        item = self.app.item("9")
        self.assertIsNone(item["duplicate"])
        self.assertTrue(item["changes"])
        self.fetched.clear()
        self.upload("10", picture(998), at=1700000600)
        self.assertEqual([u for u in self.fetched if not u.endswith("/10_s.jpg")], [])   # the library's: once

    def test_only_an_earlier_upload_counts_as_the_original(self):
        self.upload("9", picture(999))
        self.upload("10", picture(999), at=1700000600)                   # a second copy of 9
        self.assertIsNone(self.app.item("9")["duplicate"])
        self.assertEqual(self.app.item("10")["duplicate"]["photo_id"], "9")

    def test_an_unreadable_thumbnail_doesnt_stop_review(self):
        self.upload("9", b"not a jpeg")
        item = self.app.item("9")
        self.assertIsNone(item["duplicate"])
        self.assertTrue(item["changes"])
        self.assertNotIn("9", self.app.processing)

    def test_keep_reviews_it_as_usual(self):
        self.upload("9", picture(100))
        self.assertEqual(self.app.duplicate_decision("9", "keep")["status"], "pending")
        item = self.app.item("9")
        self.assertIsNone(item["duplicate"])
        self.assertTrue(item["changes"])

    def test_delete_removes_it_from_flickr_and_the_library(self):
        self.upload("9", picture(100))
        self.assertEqual(self.app.duplicate_decision("9", "delete"), {"status": "deleted"})
        self.assertIn(("flickr.photos.delete", {"photo_id": "9"}), self.fl.calls)
        self.assertIsNone(self.store.photo("9"))
        self.assertIsNone(self.store.queue_item("9"))
        with self.assertRaises(KeyError):                                 # nothing waiting any more
            self.app.duplicate_decision("9", "keep")

    def test_a_replan_leaves_a_held_duplicate_alone(self):
        self.upload("9", picture(100))
        self.app._replan(self.store)
        self.assertEqual(self.app.item("9")["duplicate"]["photo_id"], "1")

    def test_local_mode_doesnt_check(self):
        local.set_on(True)
        self.addCleanup(local.set_on, False)
        self.upload("9", picture(100))
        self.assertIsNone(self.app.item("9")["duplicate"])
        self.assertEqual(self.fetched, ["https://live.staticflickr.com/1/9_s.jpg"])     # to classify it; no library


if __name__ == "__main__":
    unittest.main()
