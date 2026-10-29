"""Console choices must be complete and confirmed before running a booking."""
from datetime import datetime
from contextlib import redirect_stdout
import io
from unittest.mock import Mock, patch
import unittest

import booking_menu as menu
from test_booking_catalog import info


def cell(time, court="", price=0, available=False):
    return {"time": time, "court": court, "price": price, "available": available,
            "period": 1, "sub": 0, "margin": 0, "total": 30, "date": "2026-10-30"}


class MenuTests(unittest.TestCase):
    def choose(self, answers, *, resource=None, cells=None, now=None):
        resource = resource or info()
        responses = iter(answers)
        output = []
        halls = [{"id": 14, "name": "一站式育人空间预约", "resources": [
            {"id": 115, "name": "非体育空间"}]}, {"id": 10, "name": "体育场馆个人预约", "resources": [
            {"id": resource["id"], "name": resource["name"]}]}]
        with patch.object(menu, "get_halls", return_value=halls), \
             patch.object(menu, "get_resource_info", return_value=resource), \
             patch.object(menu, "get_calendar", return_value=cells or [
                 cell("16:00-17:50"), cell("18:20-19:50"), cell("20:00-21:30")]):
            args = menu.choose_booking("fake", input_fn=lambda prompt: next(responses),
                output=output.append, now=now or datetime(2026, 10, 29, 21))
        return args, "\n".join(output)

    def test_free_gym_uses_actual_single_period_and_release_time(self):
        args, output = self.choose(["1", "", "3", "2", "yes"])
        self.assertIn("20:00-21:30", args)
        self.assertIn("--at", args)
        self.assertIn("22:00:00", args)
        self.assertIn("免费", output)
        self.assertNotIn("--court", args)
        self.assertNotIn("一站式", output)
        self.assertNotIn("非体育空间", output)
        self.assertNotIn("选择项目 / 大厅", output)

    def test_dates_accept_enter_number_or_full_date(self):
        for date_input, expected in [("", "2026-10-30"), ("1", "2026-10-29"),
                ("2", "2026-10-30"), ("2026-10-30", "2026-10-30")]:
            with self.subTest(date_input=date_input):
                args, output = self.choose(["1", date_input, "1", "1", "yes"],
                    now=datetime(2026, 10, 29, 19), cells=[cell("20:00-21:30")])
                self.assertIsNotNone(args)
                self.assertEqual(args[args.index("--date") + 1], expected)
                self.assertIn("1. 2026-10-29", output)
                self.assertIn("2. 2026-10-30", output)
                self.assertIn("直接回车", output)

    def test_court_enter_defaults_to_any_and_time_examples_are_shown(self):
        cells = [cell(t, c, 20) for t in ["20:10-21:10", "21:10-22:10"]
                 for c in ["1号场", "2号场"]]
        args, output = self.choose(["1", "", "1,2", "", "1", "yes"],
                                  resource=info(57, 2, 1), cells=cells)
        self.assertIsNotNone(args)
        self.assertNotIn("--court", args)
        self.assertIn("单段示例", output)
        self.assertIn("多段示例", output)
        self.assertIn("1,2", output)
        self.assertIn("回车直接选择", output)

    def test_two_periods_and_specific_court(self):
        cells = [cell(t, c, 20) for t in ["20:10-21:10", "21:10-22:10"]
                 for c in ["1号场", "2号场"]]
        args, _ = self.choose(["1", "", "1,2", "2", "1", "yes"],
                             resource=info(57, 2, 1), cells=cells)
        self.assertIn("20:10-21:10;21:10-22:10", args)
        self.assertEqual(args[args.index("--court") + 1], "2号场")
        self.assertIn("--now", args)

    def test_gym_rejects_two_periods_and_reprompts(self):
        args, output = self.choose(["1", "", "1,2", "2", "1", "yes"])
        self.assertIn("18:20-19:50", args)
        self.assertIn("最多", output)

    def test_no_confirmation_returns_no_booking(self):
        args, _ = self.choose(["1", "", "1", "1", "no"])
        self.assertIsNone(args)

    def test_finished_today_is_hidden_at_2311(self):
        args, output = self.choose(["1", "", "3", "1", "no"],
                                  now=datetime(2026, 10, 29, 23, 11))
        self.assertIsNone(args)
        self.assertIn("今天已无未开场时段", output)
        self.assertIn("1. 2026-10-30", output)
        self.assertNotIn("2026-10-29", output)

    def test_weekly_rule_hint_is_not_shown(self):
        resource = info()
        resource["json_allow_range"]["normal"]["multiple"] = {
            "is_enable": 1, "rule": ["1", "w"]}
        _, output = self.choose(["1", "", "1", "1", "no"], resource=resource)
        self.assertNotIn("累计规则", output)
        self.assertNotIn("每周", output)
        self.assertIn("单次最多", output)

    def test_cancel_at_any_prompt(self):
        args, _ = self.choose(["q"])
        self.assertIsNone(args)

    def test_does_not_offer_waiting_until_after_selected_period_starts(self):
        args, output = self.choose(["1", "2026-10-29", "1", "2", "1", "yes"],
            now=datetime(2026, 10, 29, 19), cells=[cell("20:00-21:30")])
        self.assertIsNotNone(args)
        self.assertNotIn("--at", args)
        self.assertIn("编号无效", output)

    def test_main_does_not_call_booking_after_cancellation(self):
        with patch.object(menu, "load_cookie", return_value="fake"), \
             patch.object(menu, "choose_booking", return_value=None), \
             patch("auto.main", side_effect=AssertionError("must not book")):
            self.assertEqual(menu.main(), 0)

    def test_error_after_confirmation_does_not_claim_nothing_was_submitted(self):
        with patch.object(menu, "load_cookie", return_value="fake"), \
             patch.object(menu, "choose_booking", return_value=["--now"]), \
             patch("auto.main", side_effect=RuntimeError("unknown after submission")), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(menu.main(), 1)
        self.assertNotIn("未开始预约", output.getvalue())
        self.assertIn("核对", output.getvalue())


if __name__ == "__main__":
    unittest.main()
