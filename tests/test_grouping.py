"""Group suggestions: learned from the photos already in each pool; applied, and undone."""
import json
import os
import tempfile
import time
import unittest

from fakes import photo, FakeFlickr, CFG
from coconut_barometer.changes import Change
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


    # ---- a group's posting limit, and its rules ------------------------------------------
    def send(self, app, pid, group="gav", name="Aviation"):
        """One photo's add to a group, approved and applied."""
        c = Change("add_to_group", pid, name, album_id=group)
        self.store.queue_put(pid, "pending", [c.to_dict()])
        app.decide(pid, [{"approve": True}])
        return self.store.queue_item(pid)

    def adds(self, pid):
        return [p for m, p in self.fl.calls if m == "flickr.groups.pools.add" and p["photo_id"] == pid]

    def test_a_used_up_limit_holds_the_group_until_it_resets(self):
        self.fl.throttles["gav"] = {"count": 1, "mode": "day", "remaining": 1}
        app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg)
        self.assertIsNone(self.send(app, "x0"))                               # within the limit: applied, out of review
        q = self.send(app, "x1")
        self.assertEqual(len(self.adds("x1")), 1)                            # refused once: not tried again
        self.assertEqual(q["status"], "pending")                             # still in review, saying why
        self.assertIn("Aviation takes 1 a day; can go after", q["result"]["error"])
        self.assertEqual(q["result"]["changes"][0]["status"], "deferred")
        q = self.send(app, "x2")                                             # the group's held: no call at all
        self.assertEqual(self.adds("x2"), [])
        self.assertEqual(q["result"]["changes"][0]["status"], "deferred")
        self.assertIsNone(self.send(app, "x3", "gfood", "Food"))               # other groups carry on
        self.assertIn("Aviation takes 1 a day", app.bulk("group")["held"]["Aviation"])   # Bulk → Groups says so
        hold = json.loads(self.store.get_meta("group_hold:gav"))
        self.assertAlmostEqual(hold["until"] - time.time(), 86400, delta=60)
        self.store.set_meta("group_hold:gav", json.dumps({**hold, "until": time.time() - 1}))   # it's reset
        self.fl.throttles["gav"]["remaining"] = 1
        self.assertIsNone(self.send(app, "x2"))

    def test_the_photos_other_changes_go_through_without_waiting(self):
        self.fl.throttles["gav"] = {"count": 1, "mode": "day", "remaining": 0}
        app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg)
        changes = [Change("set_title", "x1", "Over the field", old=""), Change("add_to_group", "x1", "Aviation", album_id="gav"),
                   Change("add_to_group", "x1", "Food", album_id="gfood")]
        self.store.queue_put("x1", "pending", [c.to_dict() for c in changes])
        app.decide("x1", [{"approve": True, "value": "Over the field"}, {"approve": True}, {"approve": True}])
        q = self.store.queue_item("x1")
        self.assertEqual(next(p for p in self.fl.photos if p["id"] == "x1")["title"], "Over the field")
        self.assertEqual(self.fl.pools["gfood"], ["x1"])
        self.assertEqual([(c["action"], c["new"]) for c in q["changes"]], [("add_to_group", "Aviation")])   # only that waits
        self.assertEqual(self.store.last_applied("x1")["changes"][0]["action"], "set_title")             # and the rest can be undone

    def test_a_limit_for_all_time_is_a_refusal(self):
        self.fl.throttles["gav"] = {"count": 3, "mode": "ever", "remaining": 0}
        q = self.send(ReviewApp(self.store, self.fl, background=False, cfg=self.cfg), "x1")
        self.assertEqual(q["result"]["changes"][0]["status"], "refused")
        self.assertIsNone(self.store.get_meta("group_hold:gav"))

    def test_a_refusal_is_not_retried_or_proposed_again(self):
        app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg)
        self.fl.refusing["gav"] = 8                                          # content the group doesn't take
        q = self.send(app, "new")
        self.assertEqual(len(self.adds("new")), 1)
        self.assertIn("Aviation: the group doesn't take this kind of photo", q["result"]["error"])
        self.assertEqual(q["result"]["changes"][0]["status"], "refused")
        groups = lambda pid: [c.new for c in build_plan(self.cfg, self.store, None, photo_ids={pid}) if c.action == "add_to_group"]
        self.assertEqual(groups("new"), [])
        self.fl.refusing["gfood"] = 4                                        # the photo's in as many groups as it may be
        self.send(app, "f0", "gfood", "Food")
        self.assertEqual(self.store.refused_groups()["f0"], {"*"})

    def test_a_failure_is_still_retried_once(self):
        self.fl.refusing["gav"] = 1                                          # photo not found: not one we know
        q = self.send(ReviewApp(self.store, self.fl, background=False, cfg=self.cfg), "new")
        self.assertEqual(len(self.adds("new")), 2)
        self.assertEqual(q["result"]["changes"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
