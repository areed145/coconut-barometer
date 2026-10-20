"""Small helpers."""
import json
import unittest

from flickr_organizer.util import norm_title, quote_tags
from flickr_organizer.vision.base import parse_json_loose


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


if __name__ == "__main__":
    unittest.main()
