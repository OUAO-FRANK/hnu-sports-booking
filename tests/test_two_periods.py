"""Two consecutive periods on the same court; no live bookings."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auto
import auto_grab
import test_booking_flow as booking_flow
from datetime import datetime

TIMES = ["20:10-21:10", "21:10-22:10"]


def cell(court, time, period, sub):
    return {"court": court, "time": time, "period": period, "sub": sub,
            "margin": 1, "total": 1, "price": 20}


FIRST = cell("1号场", TIMES[0], 100, 201)
SECOND = cell("1号场", TIMES[1], 101, 202)
GROUP = {"court": "1号场", "time": "; ".join(TIMES), "slots": [FIRST, SECOND]}


class TwoPeriodTests(unittest.TestCase):
    def test_exact_times_allow_variable_length_but_require_continuity(self):
        self.assertEqual(auto.parse_times("20:10-21:10;21:10-22:10"), TIMES)
        self.assertEqual(auto.parse_times("20:00-21:30"), ["20:00-21:30"])
        for text in ["19:10-20:10;21:10-22:10", "20:00-19:00"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                auto.parse_times(text)

    def groups(self, slots):
        function = getattr(auto, "group_slots", None)
        self.assertIsNotNone(function, "same-court grouping is required")
        return function(slots, TIMES)

    def test_both_periods_on_same_court_form_one_candidate(self):
        groups = self.groups([SECOND, FIRST])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["slots"], [FIRST, SECOND])
        self.assertEqual(groups[0]["price"], 40)

    def test_single_period_never_falls_back_to_one_hour(self):
        self.assertEqual(self.groups([FIRST]), [])

    def test_periods_on_different_courts_are_not_combined(self):
        self.assertEqual(self.groups([FIRST, cell("2号场", TIMES[1], 101, 203)]), [])

    def test_launch_submits_both_periods_in_one_request(self):
        with patch.object(auto_grab, "_req", return_value={"e": 0}) as request:
            auto_grab.launch("fake", 57, "2026-10-30", GROUP, "verified")
        request.assert_called_once()
        self.assertEqual(json.loads(request.call_args.kwargs["form"]["data"]), [
            {"date": "2026-10-30", "period": 100, "sub_resource_id": 201},
            {"date": "2026-10-30", "period": 101, "sub_resource_id": 202},
        ])

    def test_partial_reservation_is_not_a_success(self):
        row = {"resource_id": 57, "period_text": {"x": {
            "date": "2026-10-30", "sub_name": "1号场", "text": [TIMES[0]]}}}
        self.assertFalse(auto.reservation_matches(row, 57, "2026-10-30", GROUP))
        row["period_text"]["x"]["text"].append(TIMES[1] + "\r\n")
        self.assertTrue(auto.reservation_matches(row, 57, "2026-10-30", GROUP))
        row["period_text"]["x"]["text"].append("22:10-23:10")
        self.assertFalse(auto.reservation_matches(row, 57, "2026-10-30", GROUP))

    def test_main_uses_one_submission_for_both_periods(self):
        code, calls, _, _ = booking_flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
            fetch=lambda *args: [FIRST, SECOND], times=";".join(TIMES))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3]["slots"], [FIRST, SECOND])

    def test_main_does_not_submit_a_single_available_hour(self):
        code, calls, _, _ = booking_flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
            fetch=lambda *args: [FIRST], times=";".join(TIMES))
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
