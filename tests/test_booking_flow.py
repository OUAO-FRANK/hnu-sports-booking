"""Offline regressions for the active entry point; never use real credentials."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
import io
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, MagicMock, Mock, patch

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
    def test_malformed_captcha_status_never_produces_verified_token_or_refetches(self):
        cap = {"token": "synthetic", "secretKey": "synthetic"}
        for value in (False, 0.0, [], {"unexpected": "synthetic"}, None):
            with self.subTest(value=value), \
                 patch.object(auto, "get_captcha", return_value=None) as get, \
                 patch.object(auto, "verify_captcha", return_value={"e": value}) as verify, \
                 patch.object(auto, "aes_ecb_encrypt_b64", return_value="synthetic"):
                with self.assertRaises(auto.RequestError):
                    auto.solve_captcha(None, "fake", initial_cap=cap,
                                       initial_points=[[1, 2]] * 4)
                get.assert_not_called()
                verify.assert_called_once()

    def test_malformed_payment_result_never_treated_as_success(self):
        for value in (False, 0.0, [], None):
            with self.subTest(value=value), \
                 patch.object(auto, "reservation_detail", side_effect=[record(status=6), record(status=7)]) as detail, \
                 patch.object(auto, "_req", return_value={"e": value}):
                with self.assertRaises(auto.RequestError):
                    auto.reservation_paid("fake", 101, order_id=999)
                self.assertEqual(detail.call_count, 1)

    def test_unknown_application_codes_reconcile_before_another_launch(self):
        for value in (2, 9, 999, "unexpected"):
            with self.subTest(value=value), \
                 patch.object(auto, "find_reservation", return_value=None) as reconcile:
                code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22),
                    fetch=lambda *args: [dict(SLOT), dict(SLOT, court="Court 2", sub=3)],
                    responses=[{"e": value, "m": "已满"}, {"e": 0, "d": {"id": 101}}])
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)
                reconcile.assert_called_once()

    def test_unknown_submission_responses_reconcile_before_any_second_launch(self):
        for response in ({"e": "SHAPE"}, {}, {"e": 302, "m": "HTTPRedirect"},
                         {"e": 429, "m": "Too Many Requests"}, {"e": 403}):
            with self.subTest(response=response), \
                 patch.object(auto, "find_reservation", return_value=None) as reconcile:
                code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22),
                    fetch=lambda *args: [dict(SLOT), dict(SLOT, court="Court 2", sub=3)],
                    responses=[response, {"e": 0, "d": {"id": 101}}])
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1, "ambiguous responses must not trigger a second write")
                reconcile.assert_called_once()

    def test_malformed_status_values_never_count_as_acceptance_or_trigger_retries(self):
        for value in (False, 0.0, [], {"unexpected": "synthetic"}):
            with self.subTest(value=value), patch.object(auto, "find_reservation", return_value=None):
                code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22),
                    fetch=lambda *args: [dict(SLOT), dict(SLOT, court="Court 2", sub=3)],
                    responses=[{"e": value}, {"e": 0, "d": {"id": 101}}])
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)

    def test_login_expiry_10043_stops_without_trying_another_court(self):
        code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22),
            fetch=lambda *args: [dict(SLOT), dict(SLOT, court="Court 2", sub=3)],
            responses=[{"e": 10043}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)

    def test_ambiguous_response_with_matching_two_period_receipt_is_not_repeated(self):
        periods = ["19:10-20:10", "20:10-21:10"]
        receipt = record()
        receipt["period_text"]["label"]["text"] = periods
        with patch.object(auto, "find_reservation", return_value=receipt) as reconcile:
            code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22), times=";".join(periods),
                fetch=lambda *args: [dict(SLOT, time=period, period=index)
                                     for index, period in enumerate(periods, 1)],
                responses=[{"e": "SHAPE"}])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][3]["slots"]), 2)
        reconcile.assert_called_once()

    def test_launch_reason_is_classified_without_storing_response_text(self):
        classify = getattr(auto, "launch_reason", None)
        self.assertIsNotNone(classify)
        for response, expected in [
            ({"e": 0, "m": "success"}, "ACCEPTED"),
            ({"e": 1, "m": "预约日期未达到"}, "NOT_RELEASED"),
            ({"e": 1, "m": "该场已满"}, "UNAVAILABLE"),
            ({"e": "EXC", "m": "private request content"}, "UNCERTAIN"),
            ({"e": 1, "m": "PRIVATE_NAME PRIVATE_TOKEN"}, "REJECTED"),
        ]:
            self.assertEqual(classify(response), expected)

    def test_launch_timing_records_only_safe_reason(self):
        with patch.object(auto, "record") as record_event:
            code, _, _, _ = self.run_flow(datetime(2026, 10, 29, 22))
        self.assertEqual(code, 0)
        launch = next(call for call in record_event.call_args_list
                      if call.args == ("timing",) and call.kwargs.get("stage") == "launch")
        self.assertEqual(launch.kwargs.get("reason"), "ACCEPTED")

    def test_paid_order_still_requires_confirmed_reservation(self):
        for final_status, expected in ((6, False), (7, True)):
            with self.subTest(final_status=final_status), \
                 patch.object(auto, "reservation_detail", side_effect=[record(status=6), record(status=final_status)]), \
                 patch.object(auto, "_req", return_value={"e": 0}) as request:
                self.assertEqual(auto.reservation_paid("fake", 101, order_id=999), expected)
                self.assertEqual(request.call_args.args, ("/site/payment-order/get-pay-result",))
                self.assertEqual(request.call_args.kwargs["params"], {"id": 999})

    def test_unpaid_order_and_query_errors_do_not_confirm_reservation(self):
        with patch.object(auto, "reservation_detail", return_value=record(status=6)), \
             patch.object(auto, "_req", return_value={"e": 3}):
            self.assertFalse(auto.reservation_paid("fake", 101, order_id=999))
        with patch.object(auto, "reservation_detail", return_value=record(status=6)), \
             patch.object(auto, "_req", return_value={"e": 1, "m": "query failed"}):
            with self.assertRaises(auto.RequestError):
                auto.reservation_paid("fake", 101, order_id=999)

    def test_terminal_reservation_never_probes_payment_order(self):
        with patch.object(auto, "reservation_detail", return_value=record(status=5)), \
             patch.object(auto, "_req") as request, patch.object(auto, "record") as event:
            with self.assertRaises(auto.PaymentStopped):
                auto.reservation_paid("fake", 101, order_id=999)
            request.assert_not_called()
            event.assert_called_once_with("receipt", status="5", outcome="MATCHED")

    def test_confirmed_reservation_records_final_receipt_state(self):
        with patch.object(auto, "reservation_detail", return_value=record(status=7)), \
             patch.object(auto, "record") as event:
            self.assertTrue(auto.reservation_paid("fake", 101))
        event.assert_called_once_with("receipt", status="7", outcome="MATCHED")

    def test_http_popup_checks_previously_validated_order_id(self):
        payment = Mock(return_value={"content": "http://wxpay.hnu.edu.cn/third/home?id=synthetic",
            "deadline": 70, "estimated": False, "amount": "20.00", "order_id": 999})
        def popup(*args, **kwargs):
            return kwargs["on_poll"]()
        with patch.object(auto, "reservation_paid", return_value=True) as check:
            code, _, _, _ = self.run_flow(datetime(2026, 10, 29, 22), payment=True,
                pay_mode="http", http_payment=payment, popup=popup)
        self.assertEqual(code, 0)
        check.assert_called_once_with("fake", 101, order_id=999)

    def test_payment_preparation_failure_identifies_pre_order_stage(self):
        popup = Mock()
        payment = Mock(side_effect=auto.PaymentPreparationError("资料读取失败；本次尚未发送创建订单请求"))
        code, calls, output, _ = self.run_flow(datetime(2026, 10, 29, 22),
            payment=True, pay_mode="http", http_payment=payment, popup=popup)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("支付资料核对未完成", output)
        self.assertIn("本次尚未发送创建订单请求", output)
        self.assertNotIn("请核对已有订单", output)
        self.assertIn("preparation_deadline", payment.call_args.kwargs)
        popup.assert_not_called()

    def run_flow(self, wall, *, preflight_delay=0, responses=None, fetch=None,
                 payment=False, qr_delay=0, payment_response=None, popup=None,
                 pay_mode="browser", http_payment=None, browser=None, times=None,
                 resource=None, result_fields=None, engine=None, solver=None, court=None,
                 warm_response=None, max_wait=3, captcha_get=None, inference=None,
                 output_stream=None, launch_observer=None, allow_court_change=False,
                 qr_warmup=None, no_popup=False, extra_flags=(), payment_config_factory=None,
                 clock_preparation=None, release_login_delay=0):
        clock = Clock(wall)
        observed = []
        responses = iter(responses or [{"e": 0, "d": {"id": 101}}])
        login_calls = 0
        detail_calls = 0

        def login(cookie):
            nonlocal login_calls
            if login_calls == 0:
                clock.sleep(preflight_delay)
            else:
                clock.sleep(release_login_delay)
            login_calls += 1
            return cookie

        def launch(*args):
            if launch_observer:
                launch_observer()
            observed.append((clock.now(), args[2], args[3]["court"], args[3]))
            return next(responses)

        def current_record():
            row = record(court=observed[-1][2])
            selected = observed[-1][3].get("slots")
            if selected:
                labels = {}
                for slot in selected:
                    label = labels.setdefault(slot["court"], {"date": row["period_text"]["label"]["date"],
                        "sub_name": slot["court"], "text": []})
                    label["text"].append(slot["time"])
                row["period_text"] = {f"label{index}": label for index, label in enumerate(labels.values())}
            if result_fields:
                row.update(result_fields)
            return row

        def request(path, params=None, **kwargs):
            nonlocal detail_calls
            if path == "/site/reservation/resource-info-margin":
                return warm_response() if callable(warm_response) else (warm_response or {"e": 0, "d": {}})
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

        prepare_clock = clock_preparation or Mock(return_value=(None, {"code": "CLOCK_UNAVAILABLE"}))

        def start_clock(release):
            result = prepare_clock(release)
            return lambda **kwargs: result

        with ExitStack() as stack:
            for name, value in {
                "time": clock, "datetime": SimpleNamespace(now=clock.now),
                "date": SimpleNamespace(today=lambda: clock.now().date(), fromisoformat=date.fromisoformat),
                "load_cookie": lambda: "fake", "ensure_login": login,
                "ensure_browser": browser or (lambda: True), "warmup": warmup,
                "CaptchaEngine": engine or Mock(), "solve_captcha": solver or Mock(return_value="fake"),
                "get_captcha": captcha_get or Mock(return_value=None),
                "infer_captcha": inference or Mock(return_value=[[1, 2], [3, 4], [5, 6], [7, 8]]),
                "get_resource_info": Mock(return_value=resource or catalog_cases.info(57, 2, 1)),
                "fetch_slots": fetch or (lambda *a: [dict(SLOT)]),
                "launch": launch, "_req": request,
                "get_pay_content": qr, "show_qr": popup or Mock(return_value=False),
                "warmup_qr": qr_warmup or Mock(),
                "OUT": MagicMock(),
                "create_http_payment": http_payment or Mock(side_effect=AssertionError("unexpected HTTP payment")),
                "PaymentConfigPrefetch": payment_config_factory or Mock(),
            }.items():
                stack.enter_context(patch.object(auto, name, value))
            # The same fixture also drives the saved pre-change benchmark modules.
            stack.enter_context(patch.object(auto, "start_release_clock", start_clock, create=True))
            stack.enter_context(patch.object(auto, "prepare_release_clock", prepare_clock, create=True))
            stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")))
            stack.enter_context(patch.dict(sys.modules, {"segno": SimpleNamespace(make=Mock())}))
            flags = ["--pay-mode", pay_mode] if payment else ["--no-pay"]
            if no_popup:
                flags.append("--no-popup")
            flags.extend(extra_flags)
            selection = ["--times", times] if times else ["--slots", "19-22"]
            selection += ["--court", court] if court else []
            selection += ["--allow-court-change"] if allow_court_change else []
            stack.enter_context(patch.object(sys, "argv", ["auto.py", *flags, *selection, "--max-wait", str(max_wait)]))
            with redirect_stdout(output_stream if output_stream is not None else io.StringIO()) as output:
                code = auto.main()
        return code, observed, output.getvalue(), clock

    def test_no_submission_before_release(self):
        code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 21, 59, 40))
        self.assertGreaterEqual(calls[0][0], datetime(2026, 10, 29, 22))

    def test_fast_and_slow_wall_clocks_use_calibrated_release_deadline(self):
        for offset in (-2, 2):
            with self.subTest(offset=offset):
                preparation = Mock(return_value=(20 - offset + .05,
                    {"code": "CLOCK_ALIGNED", "clock_offset_ms": offset * 1000, "clock_guard_ms": 50}))
                code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 21, 59, 40),
                    clock_preparation=preparation)
                self.assertEqual(code, 0)
                self.assertEqual(calls[0][1], "2026-10-30")
                server_dispatch = calls[0][0] + timedelta(seconds=offset)
                self.assertAlmostEqual((server_dispatch - datetime(2026, 10, 29, 22)).total_seconds(), .05, places=4)
                preparation.assert_called_once()

    def test_near_or_after_release_does_not_wait_for_clock_probe(self):
        for wall in (datetime(2026, 10, 29, 21, 59, 50), datetime(2026, 10, 29, 22)):
            preparation = Mock(side_effect=AssertionError("clock probe would delay release"))
            code, calls, _, _ = self.run_flow(wall, clock_preparation=preparation)
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            preparation.assert_not_called()

    def test_slow_login_recheck_does_not_skip_scheduled_clock_alignment(self):
        preparation = Mock(return_value=(22.05, {"code": "CLOCK_ALIGNED",
            "clock_offset_ms": -2000, "clock_guard_ms": 50}))
        code, calls, output, _ = self.run_flow(datetime(2026, 10, 29, 21, 59, 40),
            release_login_delay=10, clock_preparation=preparation)
        self.assertEqual(code, 0)
        preparation.assert_called_once()
        self.assertEqual(calls[0][0], datetime(2026, 10, 29, 22, 0, 2, 50000))
        timing = next(line for line in output.splitlines() if '"stage": "launch"' in line)
        self.assertEqual(json.loads(timing.removeprefix("[timing] "))["release_elapsed_ms"], 0)

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

    def test_full_pair_retry_precedes_slow_terminal_feedback(self):
        launches = []
        at_second_launch = []
        terminal = None
        class SlowTerminal(io.StringIO):
            def write(self, value):
                if len(launches) == 1 and value.strip():
                    auto.time.sleep(.01)  # Terminal backpressure, not a network measurement.
                return super().write(value)
        terminal = SlowTerminal()
        def observe():
            if launches:
                at_second_launch.append(terminal.getvalue())
                self.assertTrue(any(call.args == ("timing",)
                    and call.kwargs.get("stage") == "launch"
                    and call.kwargs.get("reason") == "UNAVAILABLE"
                    for call in events.call_args_list), "file diagnostics must not wait for console feedback")
            launches.append(auto.time.monotonic())
        periods = ["19:10-20:10", "20:10-21:10"]
        slots = [dict(SLOT, court=court, time=period, period=index)
                 for court in ("Court 1", "Court 2")
                 for index, period in enumerate(periods, 1)]
        with patch("random.SystemRandom.shuffle", side_effect=lambda groups: None), \
             patch.object(auto, "record") as events:
            code, calls, output, _ = self.run_flow(datetime(2026, 10, 29, 22),
                fetch=lambda *args: slots, times=";".join(periods), output_stream=terminal,
                launch_observer=observe,
                responses=[{"e": 1, "m": "该场已满"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 0)
        self.assertEqual(len(launches), 2)
        self.assertEqual(launches[1], launches[0], "feedback must not block another complete candidate")
        self.assertNotIn("该场已满", at_second_launch[0])
        self.assertIn("该场已满", output, "deferred rejection must still be visible")
        self.assertLess(output.index("该场已满"), output.index("e=0"))
        self.assertTrue(all([s["time"] for s in call[3]["slots"]] == periods for call in calls))
        timings = [call.kwargs for call in events.call_args_list
                   if call.args == ("timing",) and call.kwargs.get("stage") == "launch"]
        self.assertEqual([row["reason"] for row in timings], ["UNAVAILABLE", "ACCEPTED"])
        self.assertEqual([row["duration_ms"] for row in timings], [0, 0])

    def test_deferred_rejection_survives_failure_before_next_submission(self):
        slots = [dict(SLOT), dict(SLOT, court="Court 2", sub=3)]
        for failure in (auto.RequestError({"e": 10043, "m": "synthetic expired login"}),
                        KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                terminal = io.StringIO()
                solver = Mock(side_effect=["fake", failure])
                if isinstance(failure, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.run_flow(datetime(2026, 10, 29, 22), fetch=lambda *args: slots,
                            solver=solver, output_stream=terminal, responses=[{"e": 1, "m": "该场已满"}])
                else:
                    code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22),
                        fetch=lambda *args: slots, solver=solver, output_stream=terminal,
                        responses=[{"e": 1, "m": "该场已满"}])
                    self.assertEqual(code, 1)
                    self.assertEqual(len(calls), 1)
                    self.assertLess(terminal.getvalue().index("该场已满"),
                                    terminal.getvalue().index("验证码请求失败"))
                self.assertIn("该场已满", terminal.getvalue())

    def test_deferred_rejection_is_flushed_when_no_next_submission_occurs(self):
        slots = [dict(SLOT), dict(SLOT, court="Court 2", sub=3)]
        for expire in (False, True):
            with self.subTest(expire=expire):
                solver_calls = []
                def solve(*args, **kwargs):
                    solver_calls.append(True)
                    if len(solver_calls) == 1:
                        return "fake"
                    if expire:
                        auto.time.sleep(4)
                    return None
                code, calls, output, _ = self.run_flow(datetime(2026, 10, 29, 22),
                    fetch=lambda *args: slots, solver=solve, responses=[{"e": 1, "m": "该场已满"}])
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(output.count("该场已满"), 1)

    def test_pending_feedback_precedes_uncertain_response_reconciliation(self):
        terminal = io.StringIO()
        slots = [dict(SLOT), dict(SLOT, court="Court 2", sub=3), dict(SLOT, court="Court 3", sub=4)]
        def reconcile(*args):
            self.assertIn("该场已满", terminal.getvalue())
            self.assertIn("SHAPE", terminal.getvalue())
            return None
        with patch.object(auto, "find_reservation", side_effect=reconcile) as check:
            code, calls, output, _ = self.run_flow(datetime(2026, 10, 29, 22),
                fetch=lambda *args: slots, output_stream=terminal,
                responses=[{"e": 1, "m": "该场已满"}, {"e": "SHAPE"}])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2, "an ambiguous second submission must still stop all further writes")
        check.assert_called_once()
        self.assertLess(output.index("该场已满"), output.index("SHAPE"))

    def test_broken_terminal_does_not_mask_next_submission_exception(self):
        launches = []
        class BrokenTerminal(io.StringIO):
            def write(self, value):
                if "该场已满" in value:
                    raise BrokenPipeError("synthetic closed terminal")
                return super().write(value)
        def observe():
            launches.append(True)
            if len(launches) == 2:
                raise RuntimeError("synthetic submission exception")
        with self.assertRaisesRegex(RuntimeError, "synthetic submission exception"):
            self.run_flow(datetime(2026, 10, 29, 22), output_stream=BrokenTerminal(),
                fetch=lambda *args: [dict(SLOT), dict(SLOT, court="Court 2", sub=3)],
                launch_observer=observe, responses=[{"e": 1, "m": "该场已满"}])
        self.assertEqual(len(launches), 2)

    def test_any_court_spreads_complete_candidates_without_reducing_periods(self):
        slots = [dict(SLOT, court=court, time=period, period=index)
                 for court in ("Court 1", "Court 2")
                 for index, period in enumerate(("19:10-20:10", "20:10-21:10"), 1)]
        slots.append(dict(SLOT, court="Court 3"))  # Incomplete pair must stay excluded.
        with patch("random.SystemRandom.shuffle", side_effect=lambda groups: groups.reverse()):
            code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22),
                fetch=lambda *a: slots, times="19:10-20:10;20:10-21:10")
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][2], "Court 2")
        self.assertEqual([s["period"] for s in calls[0][3]["slots"]], [1, 2])
        self.assertEqual({s["court"] for s in calls[0][3]["slots"]}, {"Court 2"})

    def test_any_court_rejection_keeps_other_complete_candidates(self):
        slots = [dict(SLOT, court=court) for court in ("Court 1", "Court 2")]
        with patch("random.SystemRandom.shuffle", side_effect=lambda groups: groups.reverse()):
            code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22),
                fetch=lambda *a: slots, times="19:10-20:10", responses=[
                    {"e": 1, "m": "该场已满"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 0)
        self.assertEqual([call[2] for call in calls], ["Court 2", "Court 1"])

    def test_specific_court_does_not_randomize_selection(self):
        slots = [dict(SLOT, court=court) for court in ("Court 1", "Court 2")]
        with patch("random.SystemRandom.shuffle") as shuffle:
            code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22),
                fetch=lambda *a: slots, times="19:10-20:10", court="Court 2")
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][2], "Court 2")
        shuffle.assert_not_called()

    def test_first_prepared_submission_precedes_terminal_diagnostics(self):
        events = []
        class Terminal(io.StringIO):
            def write(self, value):
                if any(marker in value for marker in ('开始执行', '命中', '>>>', '重复查询',
                                                       '"stage": "query"', '"stage": "connection_warmup"')):
                    events.append('console')
                return super().write(value)
        response = {"e": 0, "d": {"synthetic": [{"date": "2026-10-30", "time_id": 1,
            "sub_id": 2, "abscissa": "Court 1", "yaxis": "19:10-20:10",
            "row": {"status": 5, "margin": 1, "total": 1}}]}}
        code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 21, 59, 40),
            warm_response=response, output_stream=Terminal(), launch_observer=lambda: events.append('launch'))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(events[0], 'launch', 'terminal backpressure must not delay the first submission')
        self.assertIn('console', events, 'diagnostics should still appear after submission')

    def test_slow_terminal_does_not_delay_first_live_submission(self):
        launched = []
        class SlowTerminal(io.StringIO):
            def write(self, value):
                if not launched and any(marker in value for marker in ('开始执行', '命中', '>>>', '"stage": "query"')):
                    auto.time.sleep(.04)  # Simulated terminal backpressure, not a network measurement.
                return super().write(value)
        code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22),
            output_stream=SlowTerminal(), launch_observer=lambda: launched.append(True))
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][0], datetime(2026, 10, 29, 22))

    def test_empty_queries_still_show_progress(self):
        code, calls, output, _ = self.run_flow(datetime(2026, 10, 29, 22), fetch=lambda *args: [])
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn('无符合条件的组合', output)
        self.assertIn('开始执行', output)

    def test_response_timing_excludes_later_terminal_output(self):
        class SlowTerminal(io.StringIO):
            def write(self, value):
                if any(marker in value for marker in ('命中', '>>>')):
                    auto.time.sleep(.04)
                return super().write(value)
        code, _, output, _ = self.run_flow(datetime(2026, 10, 29, 22),
            output_stream=SlowTerminal(), launch_observer=lambda: auto.time.sleep(.05))
        self.assertEqual(code, 0)
        self.assertIn('(50ms)', output, 'submission timing must stop before terminal rendering')

    def test_failed_captcha_rounds_keep_throttled_progress(self):
        code, calls, output, _ = self.run_flow(datetime(2026, 10, 29, 22),
            solver=Mock(return_value=None), max_wait=6)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        count = output.count('尚未完成预约提交')
        self.assertGreaterEqual(count, 1)
        self.assertLessEqual(count, 2, 'failed rounds should report at most once per five seconds')

    def test_gateway_error_reconciles_before_another_submission(self):
        code, calls, _, _ = self.run_flow(datetime(2026, 10, 29, 22), responses=[
            {"e": 502, "m": "HTTPError"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)


class CrossCourtTests(unittest.TestCase):
    times = ["19:10-20:10", "20:10-21:10"]

    def slots(self):
        return [dict(SLOT, court="Court 1", sub=11),
                dict(SLOT, court="Court 2", time=self.times[1], period=2, sub=22)]

    def groups(self, slots):
        self.assertIn('allow_court_change', inspect.signature(auto.group_slots).parameters)
        return auto.group_slots(slots, self.times, allow_court_change=True)

    def test_strict_default_still_rejects_different_courts(self):
        self.assertEqual(auto.group_slots(self.slots(), self.times), [])

    def test_fixed_court_cannot_be_combined_with_court_changes(self):
        with patch.object(auto, 'load_cookie') as load, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                auto.main(['--non-interactive', '--court', 'Court 1', '--allow-court-change'])
        self.assertEqual(raised.exception.code, 2)
        load.assert_not_called()

    def test_complete_same_court_candidates_take_priority(self):
        slots = self.slots() + [dict(self.slots()[1], court="Court 1", sub=11)]
        groups = self.groups(slots)
        self.assertEqual(len(groups), 1)
        self.assertEqual({s['court'] for s in groups[0]['slots']}, {'Court 1'})

    def test_cross_court_fallback_contains_both_periods(self):
        groups = self.groups(self.slots())
        self.assertEqual(len(groups), 1)
        self.assertEqual([s['court'] for s in groups[0]['slots']], ['Court 1', 'Court 2'])
        self.assertEqual([s['period'] for s in groups[0]['slots']], [1, 2])
        self.assertIn('Court 1', groups[0]['court'])
        self.assertIn('Court 2', groups[0]['court'])

    def test_missing_period_never_degrades_to_one_hour(self):
        self.assertEqual(self.groups(self.slots()[:1]), [])

    def test_ambiguous_duplicate_court_period_is_not_used(self):
        slots = self.slots() + [dict(self.slots()[0], period=99)]
        self.assertEqual(self.groups(slots), [])

    def test_cross_court_launch_uses_individual_resource_ids(self):
        group = self.groups(self.slots())[0]
        with patch.object(auto_grab, '_req', return_value={'e': 0}) as request:
            auto_grab.launch('fake', 57, '2026-10-30', group, 'synthetic')
        import json
        rows = json.loads(request.call_args.kwargs['form']['data'])
        self.assertEqual([r['sub_resource_id'] for r in rows], [11, 22])
        self.assertEqual([r['period'] for r in rows], [1, 2])
        self.assertEqual(request.call_count, 1)

    def test_cross_court_receipt_must_match_every_period(self):
        group = self.groups(self.slots())[0]
        row = {'resource_id': 57, 'period_text': {
            'first': {'date': '2026-10-30', 'sub_name': 'Court 1', 'text': [self.times[0]]},
            'second': {'date': '2026-10-30', 'sub_name': 'Court 2', 'text': [self.times[1]]}}}
        self.assertTrue(auto.reservation_matches(row, 57, '2026-10-30', group))
        row['period_text'].pop('second')
        self.assertFalse(auto.reservation_matches(row, 57, '2026-10-30', group))

    def test_live_flow_submits_complete_cross_court_bundle_once(self):
        code, calls, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 22),
            times=';'.join(self.times), fetch=lambda *args: self.slots(), allow_court_change=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual([s['sub'] for s in calls[0][3]['slots']], [11, 22])

    def test_available_cross_court_snapshot_avoids_a_post_release_query(self):
        response = {'e': 0, 'd': {'synthetic': [
            {'date': '2026-10-30', 'time_id': s['period'], 'sub_id': s['sub'],
             'abscissa': s['court'], 'yaxis': s['time'], 'row': {'status': 5, 'margin': 1, 'total': 1}}
            for s in self.slots()]}}
        fetch = Mock(side_effect=AssertionError('fresh complete snapshot should be reused'))
        code, calls, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
            times=';'.join(self.times), warm_response=response, fetch=fetch, allow_court_change=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        fetch.assert_not_called()

    def test_cross_court_conflict_is_not_split_or_resubmitted(self):
        code, calls, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 22),
            times=';'.join(self.times), fetch=lambda *args: self.slots(), allow_court_change=True,
            responses=[{'e': 2, 'm': '预约时间冲突'}, {'e': 0, 'd': {'id': 101}}])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][3]['slots']), 2)

    def test_partial_receipt_prevents_payment(self):
        partial = record(court='Court 1')
        payment = Mock(side_effect=AssertionError('partial reservation must not enter payment'))
        with patch.object(auto, 'reservation_detail', return_value=partial):
            code, calls, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 22),
                times=';'.join(self.times), fetch=lambda *args: self.slots(), allow_court_change=True,
                payment=True, pay_mode='http', http_payment=payment)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        payment.assert_not_called()


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
    def test_payment_config_is_prepared_before_release_and_passed_with_receipt(self):
        resource = catalog_cases.info(57, 2, 1)
        resource["json_advanced_rule"] = {"mode": 1, "rule": {"related_id": 616}}
        preparation = object()
        factory = Mock(return_value=preparation)
        payment = Mock(return_value={"content": "https://example.invalid/fake", "deadline": 140,
                                     "estimated": True, "amount": "20.00"})
        def observe():
            factory.assert_called_once_with("fake")
        code, calls, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 21, 59),
            resource=resource, payment=True, pay_mode="http", http_payment=payment,
            payment_config_factory=factory, launch_observer=observe, popup=Mock(return_value=True))
        self.assertEqual(code, 0)
        self.assertGreaterEqual(calls[0][0], datetime(2026, 10, 29, 22))
        self.assertIs(payment.call_args.kwargs["preparation"], preparation)
        self.assertEqual(payment.call_args.kwargs["reservation"]["id"], 101)

    def test_payment_config_preparation_skips_late_no_pay_browser_and_free_resources(self):
        paid = catalog_cases.info(57, 2, 1)
        paid["json_advanced_rule"] = {"mode": 1, "rule": {"related_id": 616}}
        for wall, kwargs in ((datetime(2026, 10, 29, 22), {"resource": paid, "payment": True, "pay_mode": "http"}),
                             (datetime(2026, 10, 29, 21, 59, 41), {"resource": paid, "payment": True, "pay_mode": "http"}),
                             (datetime(2026, 10, 29, 21, 59), {"resource": paid}),
                             (datetime(2026, 10, 29, 21, 59), {"resource": paid, "payment": True}),
                             (datetime(2026, 10, 29, 21, 59), {"payment": True, "pay_mode": "http",
                                "result_fields": {"status": 7, "pay_money": "0.00"}})):
            with self.subTest(wall=wall, kwargs=kwargs):
                factory = Mock()
                FlowTests().run_flow(wall, payment_config_factory=factory,
                    http_payment=Mock(side_effect=RuntimeError("synthetic payment stopped")), **kwargs)
                factory.assert_not_called()

    def test_config_worker_start_failure_does_not_stop_booking(self):
        resource = catalog_cases.info(57, 2, 1)
        resource["json_advanced_rule"] = {"mode": 1, "rule": {"related_id": 616}}
        code, calls, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 21, 59), resource=resource,
            payment=True, pay_mode="http", result_fields={"status": 7, "pay_money": "0.00"},
            payment_config_factory=Mock(side_effect=RuntimeError("no thread")))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_popup_preload_finishes_before_scheduled_submission(self):
        events = []
        code, _, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 21, 59),
            payment=True, qr_warmup=lambda: events.append("preload"),
            launch_observer=lambda: events.append("launch"), popup=Mock(return_value=True))
        self.assertEqual(code, 0)
        self.assertEqual(events, ["preload", "launch"])

    def test_late_no_pay_and_no_popup_runs_skip_preload(self):
        for wall, kwargs in ((datetime(2026, 10, 29, 22), {"payment": True}),
                             (datetime(2026, 10, 29, 21, 59, 30), {"payment": True}),
                             (datetime(2026, 10, 29, 21, 59), {"payment": True, "extra_flags": ["--now"]}),
                             (datetime(2026, 10, 29, 21, 59), {"payment": True, "extra_flags": ["--dry-run"]}),
                             (datetime(2026, 10, 29, 21, 59), {}),
                             (datetime(2026, 10, 29, 21, 59), {"payment": True, "no_popup": True})):
            with self.subTest(wall=wall, kwargs=kwargs):
                preload = Mock()
                FlowTests().run_flow(wall, qr_warmup=preload, **kwargs)
                preload.assert_not_called()

    def test_failed_local_preload_does_not_stop_booking(self):
        preload = Mock(side_effect=ImportError("missing UI dependency"))
        code, calls, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 21, 59),
            payment=True, qr_warmup=preload, popup=Mock(return_value=True))
        preload.assert_called_once()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_http_payment_skips_browser_and_uses_verified_result(self):
        browser = Mock(return_value=False)
        payment = Mock(return_value={"content": "https://example.invalid/fake",
                                     "deadline": 70, "estimated": True, "amount": "20.00"})
        popup = Mock(return_value=True)
        code, _, _, _ = FlowTests().run_flow(datetime(2026, 10, 29, 22), payment=True,
            pay_mode="http", http_payment=payment, browser=browser, popup=popup)
        self.assertEqual(code, 0)
        browser.assert_not_called()
        payment.assert_called_once_with("fake", 101, reservation=ANY, preparation=None, preparation_deadline=ANY)
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
