"""Config validation (load_config refuses errors) and the `check` command."""
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from fakes import FakeFlickr
from coconut_barometer.cli import cmd_check
from coconut_barometer.config import DEFAULTS, _merge, load_config, retire, validate
from coconut_barometer.store import Store
from coconut_barometer.sync import sync

ROOT = os.path.join(os.path.dirname(__file__), "..")


def problems(raw):
    return validate(raw, _merge(DEFAULTS, raw))


class ValidateTest(unittest.TestCase):
    def test_example_config_is_valid(self):
        tmp = tempfile.mkdtemp()           # a copy, so reading it settles on a database here, not the one in the project
        shutil.copy(os.path.join(ROOT, "config.example.yaml"), tmp)
        cfg = load_config(os.path.join(tmp, "config.example.yaml"))
        self.assertTrue(cfg["vision"]["categories"])

    def test_typos_get_a_suggestion(self):
        errors, _ = problems({"vision": {"min_confidenc": 1, "categories": {"x": {
            "description": "d", "tag": ["a"]}}}})
        self.assertIn("vision.min_confidenc: unknown setting (did you mean vision.min_confidence?)", errors)
        self.assertIn("vision.categories.x.tag: unknown setting (did you mean tags?)", errors)

    def test_types_choices_and_ranges(self):
        errors, _ = problems({"vision": {"use_location": "yes", "provider": "gemini", "min_confidence": 75,
                                         "extra_tags": {"max": 2.5}}, "geo": None})
        joined = "\n".join(errors)
        for expected in ("vision.use_location: should be true or false", "isn't one of claude",
                         "vision.min_confidence: 75 is outside 0..1", "vision.extra_tags.max: should be a number",
                         "geo: is empty"):
            self.assertIn(expected, joined)

    def test_placeholders_items_and_regexes(self):
        errors, _ = problems({
            "vision": {"title": {"instructions": "Up to {max_words} words, {mood}"}},
            "standard_tags": {"date": {"formats": ["y{year}", "{decade}"]},
                              "camera": {"rules": [{"match": "iphone("}], "albums": [{"match": "x"}]}},
            "geo": {"cities": [{"album": "Austin", "lat": "30.2", "lon": -97.7, "radius": 30}]},
        })
        joined = "\n".join(errors)
        for expected in ("unknown placeholder {mood}", "formats[1]: unknown placeholder {decade}",
                         "rules[0].match: not a valid regular expression", "albums[0]: missing album",
                         "geo.cities[0].radius: unknown setting", "geo.cities[0]: lat, lon and radius_km should be numbers"):
            self.assertIn(expected, joined)
        self.assertNotIn("y{year}", joined)

    def test_retired_settings_are_dropped(self):
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        with open(path, "w") as f:
            f.write("vision:\n  enabled: true\n  include_videos: false\n  describe: {enabled: false, reword: true}\n"
                    "  title: {enabled: true, replace: all}\nrename: {enabled: true}\ngeo: {enabled: false}\n"
                    "web: {process_new: true}\n")
        from coconut_barometer.config import read_raw
        raw = read_raw(path)
        self.assertEqual(problems(raw)[0], [])
        self.assertEqual(raw["schedule"], {"new_photos": "process"})            # process_new: true carried over
        self.assertNotIn("rename", raw)
        self.assertEqual(raw["vision"], {"describe": {}, "title": {}})

    def test_warnings_dont_stop_loading(self):
        errors, warnings = problems({"vision": {"categories": {"x": {"description": "d"}}}})
        self.assertEqual(errors, [])
        self.assertTrue(any("neither album nor tags" in w for w in warnings))

    def test_removed_settings_are_dropped_not_refused(self):
        raw = retire({"flickr": {"calls_per_second": 1}, "geo": {"add_tags": True, "countries": {"auto_match": True}},
                      "standard_tags": {"max_tags_per_photo": 40, "album_tags": {"enabled": True},
                                        "place": {"enabled": True, "state_abbr": False, "region_overrides": {}}}})
        self.assertEqual(problems(raw)[0], [])
        self.assertEqual(raw["standard_tags"], {"place": {"enabled": True}})

    def test_load_config_refuses_errors(self):
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        with open(path, "w") as f:
            f.write("vision:\n  min_confidence: 2\n")
        with self.assertRaises(SystemExit) as e:
            load_config(path)
        self.assertIn("vision.min_confidence: 2 is outside 0..1", str(e.exception))


class CheckTest(unittest.TestCase):
    def test_check_reports_albums_that_dont_exist(self):
        tmp = tempfile.mkdtemp()
        sync(FakeFlickr(), Store(os.path.join(tmp, "db.duckdb")), log=lambda *a: None)
        path = os.path.join(tmp, "config.yaml")
        with open(path, "w") as f:
            f.write("database: db.duckdb\nvision:\n  categories:\n    food:\n      album: Fod\n"
                    "      description: food\n    scenery:\n      album: Landscapes\n      description: views\n")
        out = io.StringIO()
        with redirect_stdout(out):
            code = cmd_check(path)
        self.assertEqual(code, 0)
        self.assertIn("vision.categories.food.album: no album called 'Fod', so it's skipped (closest: 'Food')",
                      out.getvalue())
        self.assertNotIn("Landscapes", out.getvalue())


if __name__ == "__main__":
    unittest.main()
