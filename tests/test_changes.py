"""Change (the shape of a proposal) and Settled (when one counts as dealt with)."""
import os
import tempfile
import unittest

from flickr_organizer.changes import Change, Settled
from flickr_organizer.store import Store


class ChangeTest(unittest.TestCase):
    def test_round_trips_through_the_queue_format(self):
        c = Change("set_title", "1", "Low Tide, Alki Beach", old="Seattle, WA", source="vision:title")
        self.assertEqual(Change.from_dict(c.to_dict()), c)

    def test_value_is_your_edit_if_you_made_one(self):
        c = Change("add_tags", "1", ["fog", "pier"])
        self.assertEqual(c.value, ["fog", "pier"])
        c.edited = ["fog"]
        self.assertEqual(c.value, ["fog"])

    def test_reads_rows_from_before_change_existed(self):
        tags = Change.from_dict({"action": "add_tags", "photo_id": "1", "target": "fog",
                                 "original_target": "fog | pier", "approve": "n"})
        self.assertEqual((tags.new, tags.edited, tags.approved), (["fog", "pier"], ["fog"], False))
        title = Change.from_dict({"action": "set_title", "photo_id": "1", "target": "Low Tide",
                                  "original_target": "IMG_1234"})
        self.assertEqual((title.new, title.old), ("Low Tide", "IMG_1234"))


class SettledTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))

    def test_applied_rejected_and_edited_settle_but_failed_does_not(self):
        for target, status in [("A", "applied"), ("B", "rejected"), ("C", "failed")]:
            self.store.record("1", "add_to_album", target, status)
        self.store.record("1", "set_title", "Proposed", "edited")
        s = Settled(self.store)
        self.assertTrue(s.album("1", "A") and s.album("1", "B"))
        self.assertFalse(s.album("1", "C"))
        self.assertTrue(s.title("1", "Proposed"))

    def test_descriptions_are_settled_per_proposal(self):
        self.store.record("1", "set_description", "Some text", "rejected")
        self.assertTrue(Settled(self.store).description("1", "Some text"))
        self.assertFalse(Settled(self.store).description("1", "Other text"))   # a new brief's text: proposed
        self.assertFalse(Settled(self.store).description("2", "Some text"))

if __name__ == "__main__":
    unittest.main()
