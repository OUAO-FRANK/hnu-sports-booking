"""Read-only catalog and venue-rule regressions."""
from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import booking_catalog as catalog
from shape_adapter import parse_margin


def info(venue=59, limit=1, verify=0):
    return {"id": venue, "name": "健身房" if venue == 59 else "羽毛球馆",
            "calendar_type": 2 if venue == 59 else 1, "is_group": 0,
            "is_verify": verify, "apply_status": True,
            "json_allow_range": {"normal": {"type": "d", "week": list(range(1, 8)),
                "range": [0, 1], "single": {"is_enable": 1, "number": limit},
                "day_limit": {"is_enable": 1, "time": "22:00:00"}}}}


class CatalogTests(unittest.TestCase):
    def test_known_sports_payment_flow_is_supported(self):
        resource = info(57, 2, 1)
        resource["json_advanced_rule"] = {"mode": 1, "rule": {
            "lock_mode": 2, "related_id": 616,
            "related": [{"id": 616, "name": "体育场馆预约支付确认"}]}}
        with patch.object(catalog, "_req", return_value={"e": 0, "d": resource}):
            self.assertEqual(catalog.get_resource_info("fake", 57), resource)

    def test_unimplemented_related_workflow_is_still_rejected(self):
        resource = info(57, 2, 1)
        resource["json_advanced_rule"] = {"mode": 1, "rule": {"lock_mode": 2, "related_id": 999}}
        with patch.object(catalog, "_req", return_value={"e": 0, "d": resource}):
            with self.assertRaises(RuntimeError):
                catalog.get_resource_info("fake", 57)

    def test_live_halls_keep_all_resource_types(self):
        halls = [{"id": 10, "name": "体育场馆个人预约", "resources": [
            {"id": 59, "name": "南校区健身房"}, {"id": 83, "name": "乒乓球馆"}]}]
        with patch.object(catalog, "_req", return_value={"e": "0", "d": halls}):
            self.assertEqual(catalog.get_halls("fake"), halls)

    def test_catalog_contains_full_periods_without_historical_fallback(self):
        response = {"e": 0, "d": {"833": [
            {"time_id": 10091, "sub_id": 0, "abscissa": "2026-10-30",
             "date": "2026-10-30", "yaxis": "16:00-17:50",
             "row": {"status": 3, "margin": 0, "total": 30, "price": 0}}]}}
        with patch.object(catalog, "_req", return_value=response) as request:
            slots = catalog.get_calendar("fake", 59, "2026-10-30")
        self.assertEqual(slots[0]["time"], "16:00-17:50")
        self.assertFalse(slots[0]["available"])
        self.assertEqual(slots[0]["court"], "")
        self.assertEqual(request.call_count, 1)

    def test_gym_calendar_date_is_not_a_court(self):
        payload = {"e": 0, "d": [{"time_id": 1, "sub_id": 0,
            "abscissa": "2026-10-30", "date": "2026-10-30", "yaxis": "20:00-21:30",
            "row": {"status": 1, "margin": 10, "total": 30, "price": 0}}]}
        self.assertEqual(parse_margin(payload)[0]["court"], "")

    def test_variable_length_single_and_contiguous_periods(self):
        self.assertEqual(catalog.parse_times("16：00–17：50"), ["16:00-17:50"])
        self.assertEqual(catalog.parse_times("18:20-19:50;19:50-21:30"),
                         ["18:20-19:50", "19:50-21:30"])
        for text in ["20:00-19:00", "25:00-26:00", "16:00-17:00;18:00-19:00",
                     "16:00-17:00;16:00-17:00"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                catalog.parse_times(text)

    def test_single_limit_is_enforced_without_gym_name_hardcoding(self):
        with self.assertRaisesRegex(ValueError, "1"):
            catalog.validate_selection(info(), ["18:20-19:50", "19:50-21:30"],
                                       "2026-10-30", now=datetime(2026, 10, 29, 21))
        catalog.validate_selection(info(), ["16:00-17:50"], "2026-10-30",
                                   now=datetime(2026, 10, 29, 21))

    def test_bad_date_and_unsupported_form_cannot_launch(self):
        with self.assertRaises(ValueError):
            catalog.validate_selection(info(), ["16:00-17:50"], "2026-11-01",
                                       now=datetime(2026, 10, 29, 21))
        bad = info(); bad["calendar_type"] = 3
        with patch.object(catalog, "_req", return_value={"e": 0, "d": bad}):
            with self.assertRaises(RuntimeError):
                catalog.get_resource_info("fake", 59)


if __name__ == "__main__":
    unittest.main()
