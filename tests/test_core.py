import math
import unittest

from amap_poi_crawler import Cell, adcode_matches, cell_key, gcj02_to_wgs84, parse_types, split_cell


class CoreTests(unittest.TestCase):
    def test_parse_types(self):
        self.assertEqual(parse_types("教育:141200,体育:080100"), [("教育", "141200"), ("体育", "080100")])

    def test_split_cell(self):
        children = split_cell(Cell(0, 0, 2, 2))
        self.assertEqual(len(children), 4)
        self.assertTrue(all(item.depth == 1 for item in children))
        self.assertEqual(sum((c.max_lon-c.min_lon)*(c.max_lat-c.min_lat) for c in children), 4)

    def test_cell_key_is_stable(self):
        self.assertEqual(cell_key(Cell(1, 2, 3, 4)), "1.000000,2.000000,3.000000,4.000000,d0")

    def test_city_adcode_filter(self):
        self.assertTrue(adcode_matches({"adcode": "341002"}, "341000"))
        self.assertFalse(adcode_matches({"adcode": "340100"}, "341000"))

    def test_gcj02_to_wgs84_is_plausible(self):
        lon, lat = gcj02_to_wgs84(118.553225, 33.777275)
        self.assertTrue(math.isclose(lon, 118.54799753, abs_tol=2e-6))
        self.assertTrue(math.isclose(lat, 33.77881280, abs_tol=2e-6))


if __name__ == "__main__":
    unittest.main()

