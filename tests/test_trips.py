"""Trips (#125): photos grouped by when and where they were taken, and named from where most of them are."""
import unittest

from coconut_barometer.trips import cluster, place_name


def row(pid, taken, lat=30.27, lon=-97.74):
    return {"id": pid, "date_taken": taken, "lat": lat, "lon": lon, "ispublic": 1, "geo_public": 1}


class ClusterTest(unittest.TestCase):
    def ids(self, rows, gap=36, jump=50, least=2):
        return [[r["id"] for r in t.photos] for t in cluster(rows, gap, jump, least)]

    def test_a_long_gap_starts_a_new_trip(self):
        rows = [row("a", "2025-01-01 10:00:00"), row("b", "2025-01-02 09:00:00"),     # overnight: the same trip
                row("c", "2025-01-04 12:00:00"), row("d", "2025-01-04 13:00:00")]     # two days on: a new one
        self.assertEqual(self.ids(rows), [["a", "b"], ["c", "d"]])

    def test_far_away_the_next_day_is_a_new_trip_but_a_road_trip_is_one(self):
        road = [row("a", "2025-01-01 09:00:00", 30.27, -97.74), row("b", "2025-01-01 13:00:00", 29.76, -95.37)]  # 235 km in 4 h
        home = [row("c", "2025-01-02 12:00:00", 47.61, -122.33), row("d", "2025-01-02 13:00:00", 47.61, -122.33)]  # 23 h, far
        self.assertEqual(self.ids(road + home), [["a", "b"], ["c", "d"]])

    def test_two_weeks_at_most_and_too_short_runs_dropped(self):
        rows = [row(str(i), f"2025-03-{1 + i:02d} 12:00:00") for i in range(20)]   # a photo a day for 20 days
        self.assertEqual([len(t) for t in self.ids(rows)], [14, 6])
        self.assertEqual(self.ids([row("x", "2025-01-01 10:00:00")]), [])
        self.assertEqual(self.ids(rows[:3] + [row("bad", "not a date")], least=5), [])

    def test_photos_without_a_location_go_by_time_alone(self):
        rows = [row("a", "2025-01-01 10:00:00"), row("b", "2025-01-02 09:00:00", None, None),
                row("c", "2025-01-02 10:00:00", 0, 0)]                                 # Flickr's 0,0: none
        self.assertEqual(self.ids(rows), [["a", "b", "c"]])


class PlaceNameTest(unittest.TestCase):
    def test_town_then_region_then_country(self):
        bigsur = {"cc": "US", "admin1": "California", "name": "Big Sur"}
        carmel = {"cc": "US", "admin1": "California", "name": "Carmel"}
        kyoto, osaka = {"cc": "JP", "admin1": "Kyoto", "name": "Kyoto"}, {"cc": "JP", "admin1": "Osaka", "name": "Osaka"}
        self.assertEqual(place_name([bigsur, bigsur, carmel]), "Big Sur, California")          # a US town: its state
        self.assertEqual(place_name([kyoto, kyoto, osaka]), "Kyoto, Japan")                     # elsewhere: its country
        self.assertEqual(place_name([bigsur, carmel, {**carmel, "name": "Monterey"}]), "California, United States")
        self.assertEqual(place_name([kyoto, osaka, {**osaka, "admin1": "Nara", "name": "Nara"}]), "Japan")
        self.assertEqual(place_name([]), "")


if __name__ == "__main__":
    unittest.main()
