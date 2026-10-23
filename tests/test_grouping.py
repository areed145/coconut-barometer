"""Group suggestions: learned from the photos already in each pool; applied, and undone."""
import os
import tempfile
import unittest

from fakes import photo, FakeFlickr, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.grouping import GroupMatcher
from coconut_barometer.planner import build_plan
from coconut_barometer.review import ReviewApp
from coconut_barometer.store import Store
from coconut_barometer.sync import sync


class GroupingTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        self.fl = FakeFlickr()
        rows = [(f"a{i}", "me aviation b17 airshow") for i in range(4)] + \
               [(f"f{i}", "me ramen noodles") for i in range(4)] + \
               [(f"x{i}", "me street") for i in range(12)] + [("new", "me b17 airshow"), ("priv", "me b17 airshow")]
        self.fl.photos += [{**photo(pid, tags=tags), "ispublic": 0 if pid == "priv" else 1} for pid, tags in rows]
        sync(self.fl, self.store, log=lambda *a: None)
        group = lambda gid, name: {"id": gid, "name": name, "path_alias": None, "members": 10, "pool_count": 10,
                                   "privacy": "3", "info": "{}", "fetched_at": 0}
        self.store.replace_groups([group("gav", "Aviation"), group("gfood", "Food"), group("gtiny", "Tiny")],
                                  [("gav", f"a{i}", 1) for i in range(4)] + [("gfood", f"f{i}", 1) for i in range(4)]
                                  + [("gtiny", "a0", 1)])
        self.cfg = from_dict({**CFG, "groups": {"min_match": 1}})

    def test_matches_the_group_its_like(self):
        m = GroupMatcher(self.store).match("new", min_score=1)              # (a tiny library: small lifts)
        self.assertEqual([x.name for x in m], ["Aviation"])                   # not Food; Tiny is too small
        self.assertEqual(set(m[0].shared), {"b17", "airshow"})               # "me" is on everything: says nothing
        self.assertEqual(GroupMatcher(self.store).match("a0", min_score=1), [])   # already in it

    def test_proposed_for_public_photos_and_applied_and_undone(self):
        rows = [c for c in build_plan(self.cfg, self.store, None, photo_ids={"new", "priv"}) if c.action == "add_to_group"]
        self.assertEqual([(c.photo_id, c.new, c.album_id) for c in rows], [("new", "Aviation", "gav")])
        self.cfg["groups"]["suggest"] = False
        self.assertEqual([c for c in build_plan(self.cfg, self.store, None, photo_ids={"new"}) if c.action == "add_to_group"], [])
        app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg)
        self.fl.moderated.add("gav")
        self.store.queue_put("new", "pending", [rows[0].to_dict()])
        app.decide("new", [{"approve": True}])
        self.assertEqual(self.fl.pools["gav"], ["new"])
        self.assertEqual(self.store.decisions()[("new", "add_to_group", "Aviation")], "applied")   # waiting on a moderator: fine
        self.assertIn("new", self.store.group_pools()[1]["gav"])
        app.undo("new")
        self.assertEqual(self.fl.pools["gav"], [])
        self.assertNotIn("new", self.store.group_pools()[1]["gav"])


if __name__ == "__main__":
    unittest.main()
