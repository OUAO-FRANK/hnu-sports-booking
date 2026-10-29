"""Offline regressions for the active entry point; never use real credentials."""
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime, timedelta
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auto
import auto_grab
import test_booking_catalog as catalog_cases


SLOT = {"court": "Court 1", "time": "19:10-20:10", "period": 1,
        "sub": 2, "margin": 1, "total": 1}


def record(rid=101, status=6, court="Court 1", day="2026-10-30", venue=57):
    return {"id": rid, "status": status, "resource_id": venue,
            "period_text": {"label": {"sub_name": court, "date": day,
                                       "text": ["19:10-20:10\r\n"]}}}


class Clock:
    def __init__(self, wall):
        self.wall = wall
        self.elapsed = 0.0

    def sleep(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.elapsed += seconds

    def monotonic(self):
        return self.elapsed

    perf_counter = monotonic

    def time(self):
        return self.wall.timestamp()

    def now(self):
        return self.wall


class FlowTests(unittest.TestCase):
    def run_flow(self, wall, *, preflight_delay=0, responses=None, fetch=None,
                 payment=False, qr_delay=0, payment_response=None, popup=None,
                 pay_mode="browser", http_payment=None, browser=None, times=None,
                 resource=None, result_fields=None, engine=None, solver=None, court=None):
        clock = Clock(wall)
        observed = []
        responses = iter(responses or [{"e": 0, "d": {"id": 101}}])
        login_calls = 0
        detail_calls = 0

        def login(cookie):
            nonlocal login_calls
            if login_calls == 0:
                clock.sleep(preflight_delay)
            login_calls += 1
            return cookie

        def launch(*args):
            observed.append((clock.now(), args[2], args[3]["court"], args[3]))
            return next(responses)

        def current_record():
            row = record(court=observed[-1][2])
            selected = observed[-1][3].get("slots")
            if selected:
                row["period_text"]["label"]["text"] = [slot["time"] for slot in selected]
            if result_fields:
                row.update(result_fields)
            return row

        def request(path, params=None, **kwargs):
            nonlocal detail_calls
            if (params or {}).get("data_type") == "reservation_detail":
                detail_calls += 1
                if detail_calls > 1 and payment_response is not None:
                    return payment_response
                row = current_record()
                if payment:
                    row["wait_time"] = 80
                return {"e": 0, "d": {"data": [row]}}
            rows = [current_record()] if observed else []
            return {"e": 0, "d": {"data": rows, "total": len(rows)}}

        def warmup(*args):
            return True

        def qr(*args):
            clock.sleep(qr_delay)
            return "https://example.invalid/fake"

        with ExitStack() as stack:
            for name, value in {
                "time": clock, "datetime": SimpleNamespace(now=clock.now),
                "date": SimpleNamespace(today=lambda: clock.now().date(), fromisoformat=date.fromisoformat),
                "load_cookie": lambda: "fake", "ensure_login": login,
                "ensure_browser": browser or (lambda: True), "warmup": warmup,
                "CaptchaEngine": engine or Mock(), "solve_captcha": solver or Mock(return_value="fake"),
                "get_resource_info": Mock(return_value=resource or catalog_cases.info(57, 2, 1)),
                "fetch_slots": fetch or (lambda *a: [dict(SLOT)]),
                "launch": launch, "_req": request,
                "get_pay_content": qr, "show_qr": popup or Mock(return_value=False),
                "OUT": MagicMock(),
                "create_http_payment": http_payment or Mock(side_effect=AssertionError("unexpected HTTP payment")),
            }.items():
                stack.enter_context(patch.object(auto, name, value))
            stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")))
            stack.enter_context(patch.dict(sys.modules, {"segno": SimpleNamespace(make=Mock())}))
            flags = ["--pay-mode", pay_mode] if payment else ["--no-pay"]
            selection = ["--times", times] if times else ["--slots", "19-22"]
            selection += ["--court", court] if court else []
            stack.enter_context(patch.object(sys, "argv", ["auto.py", *flags, *selection, "--max-wait", "3"]))
            with redirect_stdout(io.StringIO()) as output:
                code = auto.main()
        return code, observed, output.getvalue(), clock

    def test_no_submission_before_release(self):
        code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 21, 59, 40))
        self.assertGreaterEqual(calls[0][0], datetime(2026, 10, 29, 22))

    def test_preflight_crossing_release_keeps_tomorrow(self):
        _, calls, _, clock = self.run_flow(datetime(2026, 10, 29, 21, 59, 50), preflight_delay=15)
        self.assertEqual(calls[0][1], "2026-10-30")
        self.assertLess(clock.elapsed, 60)

    def test_late_start_does_not_wait_until_next_evening(self):
        _, calls, _, clock = self.run_flow(datetime(2026, 10, 29, 22, 0, 5))
        self.assertEqual(calls[0][1], "2026-10-30")
        self.assertLess(clock.elapsed, 60)

    def test_temporary_rejection_does_not_ban_slot(self):
        code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 21, 59, 40), responses=[
            {"e": 1, "m": "预约日期未达到"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)

    def test_login_failure_stops_instead_of_polling(self):
        fetch = Mock(side_effect=RuntimeError("登录已失效"))
        code, calls, output, _ = self.run_flow(datetime(2026, 10, 29, 21, 59, 40), fetch=fetch)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("登录", output)
        self.assertEqual(fetch.call_count, 1)

    def test_rejected_first_court_does_not_starve_second_court(self):
        slots = [dict(SLOT), dict(SLOT, court="Court 2", sub=3)]
        code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22),
            fetch=lambda *a: slots, responses=[{"e": 1, "m": "该场已满"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 0)
        self.assertEqual([call[2] for call in calls], ["Court 1", "Court 2"])

    def test_gateway_error_reconciles_before_another_submission(self):
        code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22), responses=[
            {"e": 502, "m": "HTTPError"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)


class QueryTests(unittest.TestCase):
    def test_expired_login_is_not_empty_availability(self):
        with patch.object(auto_grab, "_req", return_value={"e": "10042", "m": "请登录"}):
            with self.assertRaises(RuntimeError):
                auto_grab.fetch_slots("fake", 57, "2026-10-30")

    def test_unknown_shape_is_not_empty_availability(self):
        with patch.object(auto_grab, "_req", return_value={"e": 0, "d": {"changed": "schema"}}):
            with self.assertRaises(RuntimeError):
                auto_grab.fetch_slots("fake", 57, "2026-10-30")


class IdentityTests(unittest.TestCase):
    def test_old_first_row_is_not_used_for_payment(self):
        rows = [record(10), record(101, "6")]
        with patch.object(auto, "reservation_rows", return_value=rows), \
             patch.object(auto, "reservation_detail", return_value=rows[1]) as detail:
            actual = auto.find_reservation("fake", {"e": 0, "d": []}, {"10"}, 57, "2026-10-30", SLOT)
        self.assertEqual(actual["id"], 101)
        detail.assert_called_once_with("fake", 101)

    def test_multiple_new_rows_refuse_automatic_payment(self):
        rows = [record(101), record(102)]
        with patch.object(auto, "reservation_rows", return_value=rows):
            with self.assertRaises(RuntimeError):
                auto.find_reservation("fake", {}, set(), 57, "2026-10-30", SLOT)

    def test_launch_id_is_used_without_list_order_assumptions(self):
        with patch.object(auto, "reservation_rows", side_effect=AssertionError("must use returned ID")), \
             patch.object(auto, "reservation_detail", return_value=record()):
            actual = auto.find_reservation("fake", {"e": 0, "d": {"reserve_id": 101}}, {"10"}, 57, "2026-10-30", SLOT)
        self.assertEqual(actual["id"], 101)

    def test_post_timeout_does_not_trigger_second_submission(self):
        helper = FlowTests()
        code, calls, _, _ = helper.run_flow(datetime(2026, 10, 29, 22), responses=[{"e": "EXC", "m": "timeout"}])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_returned_id_with_wrong_court_cannot_be_paid(self):
        with patch.object(auto, "reservation_detail", return_value=record(court="Court 2")):
            with self.assertRaises(RuntimeError):
                auto.find_reservation("fake", {"e": 0, "d": {"id": 101}}, set(), 57, "2026-10-30", SLOT)

    def test_unrelated_new_manual_booking_is_not_our_reservation(self):
        with patch.object(auto, "reservation_rows", return_value=[record(101, venue=84)]), \
             patch.object(auto, "reservation_detail", side_effect=AssertionError("must not use unrelated booking")), \
             patch.object(auto.time, "sleep"):
            self.assertIsNone(auto.find_reservation("fake", {}, set(), 57, "2026-10-30", SLOT))

class PaymentTests(unittest.TestCase):
    def test_http_payment_skips_browser_and_uses_verified_result(self):
        browser = Mock(return_value=False)
        payment = Mock(return_value={"content": "https://example.invalid/fake",
                                     "deadline": 70, "estimated": True, "amount": "20.00"})
        popup = Mock(return_value=True)
        code, _, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 22), payment=True,
            pay_mode="http", http_payment=payment, browser=browser, popup=popup)
        self.assertEqual(code, 0)
        browser.assert_not_called()
        payment.assert_called_once_with("fake", 101)
        self.assertEqual(popup.call_args.kwargs["deadline"], 70)

    def test_http_failure_stops_without_browser_order_fallback(self):
        browser = Mock(return_value=True)
        payment = Mock(side_effect=RuntimeError("creation result uncertain"))
        popup = Mock(return_value=True)
        code, _, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 22), payment=True,
            pay_mode="http", http_payment=payment, browser=browser, popup=popup)
        self.assertEqual(code, 1)
        payment.assert_called_once()
        browser.assert_not_called()
        popup.assert_not_called()

    def test_old_remaining_time_is_not_reused_after_qr_query_failure(self):
        popup = Mock(return_value=False)
        FlowTests().run_flow(datetime(2026, 10, 29, 22), payment=True, qr_delay=15,
            payment_response={"e": "EXC", "m": "timeout"}, popup=popup)
        self.assertEqual(popup.call_args.kwargs["deadline"], 80)

    def test_only_server_confirmed_status_is_paid(self):
        check = getattr(auto, "reservation_paid", None)
        self.assertIsNotNone(check, "payment needs a checked server response")
        for status, expected in [(6, False), ("6", False), (7, True), ("7", True)]:
            with self.subTest(status=status), patch.object(auto, "reservation_detail", return_value={"id": 101, "status": status}):
                self.assertEqual(check("fake", 101), expected)

    def test_terminal_reservation_stops_payment(self):
        check = getattr(auto, "reservation_paid", None)
        self.assertIsNotNone(check)
        with patch.object(auto, "reservation_detail", return_value={"id": 101, "status": 5, "remake": "申请超时"}):
            with self.assertRaises(auto.PaymentStopped):
                check("fake", 101)

    def test_estimated_deadline_is_not_reset_when_qr_appears(self):
        window = getattr(auto, "payment_window", None)
        self.assertIsNotNone(window)
        with patch.object(auto.time, "monotonic", return_value=20):
            self.assertEqual(window({}, reserved_at=0), (90, True))
            self.assertEqual(window({"wait_time": "45"}, reserved_at=0), (65, False))


if __name__ == "__main__":
    unittest.main()
