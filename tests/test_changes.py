"""Change: the shape of a proposal."""
import unittest

from coconut_barometer.changes import Change


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


if __name__ == "__main__":
    unittest.main()
