"""The vision prompt and schema."""
import unittest

from fakes import CFG
from coconut_barometer.classify import build_prompt, build_schema
from coconut_barometer.config import from_dict


class ClassifyPromptTest(unittest.TestCase):
    def test_location_in_prompt(self):
        from coconut_barometer.geo import place_label
        import coconut_barometer.geo as geo_mod
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
