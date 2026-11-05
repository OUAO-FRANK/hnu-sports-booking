"""Release scheduling and local inference without live API calls."""
from datetime import datetime, timedelta
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from threading import Event
import time
from unittest.mock import Mock, patch

import auto
from captcha_engine import CaptchaEngine
from test_booking_flow import Clock
import test_booking_flow as flow


class ReleaseWarmupTests(unittest.TestCase):
    def test_calibrated_wait_and_preparation_ignore_later_wall_clock_jump(self):
        release = datetime(2026, 11, 2, 22)
        clock = Clock(release - timedelta(seconds=20))
        callbacks = []
        def prepare():
            callbacks.append(clock.monotonic())
            clock.wall += timedelta(seconds=120)
        with patch.object(auto, "time", clock), \
             patch.object(auto, "datetime", SimpleNamespace(now=clock.now)), \
             patch.object(auto, "Thread", side_effect=lambda target, **kwargs: SimpleNamespace(start=target)), \
             patch.object(auto, "_req", return_value={"e": 0, "d": {}}), redirect_stdout(io.StringIO()):
            auto.wait_for_release("synthetic", 57, release, "2026-11-03",
                before_release=prepare, seconds_until=lambda: 20 - clock.monotonic())
        self.assertEqual(callbacks, [15.5])
        self.assertAlmostEqual(clock.monotonic(), 20, places=5)

    def run_wait(self, start, response=None, delay=0, *, capture=False, before_release=None):
        waiter = getattr(auto, "wait_for_release", None)
        self.assertIsNotNone(waiter, "bounded near-release connection warmup is required")
        release = datetime(2026, 11, 2, 22)
        clock = Clock(start)
        calls = []

        def request(path, **kwargs):
            calls.append((clock.now(), path, kwargs))
            if delay > kwargs['timeout']:
                clock.sleep(kwargs['timeout'])
                return {'e': 'EXC', 'm': 'ReadTimeoutError'}
            clock.sleep(delay)
            return response() if callable(response) else (response or {"e": 0})

        with patch.object(auto, "time", clock), \
             patch.object(auto, "datetime", SimpleNamespace(now=clock.now)), \
             patch.object(auto, "Thread", side_effect=lambda target, **kwargs: SimpleNamespace(start=target)), \
             patch.object(auto, "_req", side_effect=request), redirect_stdout(io.StringIO()):
            snapshot = waiter("synthetic", 57, release, "2026-11-03", before_release=before_release)
        if capture:
            return clock, calls, release, snapshot
        return clock, calls, release

    def test_fast_probe_is_refreshed_once_near_release(self):
        clock, calls, release = self.run_wait(datetime(2026, 11, 2, 21, 59, 40), delay=.1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], release - timedelta(seconds=10))
        self.assertEqual(calls[1][0], release - timedelta(seconds=4.5))
        self.assertLess(calls[0][0], release)
        self.assertEqual(calls[0][1], "/site/reservation/resource-info-margin")
        self.assertEqual(calls[0][2]["params"]["start_time"], "2026-11-03")
        self.assertLessEqual(calls[0][2]["timeout"], 9.0)
        self.assertLessEqual(calls[1][2]["timeout"], 4.0)
        self.assertGreaterEqual(clock.now(), release)

    def test_eight_second_probe_finishes_before_release_and_remains_fresh(self):
        clock, calls, release, snapshot = self.run_wait(datetime(2026, 11, 2, 21, 59, 40),
            delay=8, response=self.snapshot(day='2026-11-03'), capture=True)
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(calls), 1, 'a pending slow query must not cause a concurrent refresh')
        self.assertEqual(calls[0][0], release - timedelta(seconds=10))
        self.assertGreaterEqual(calls[0][2]['timeout'], 8)
        self.assertAlmostEqual(clock.monotonic() - snapshot[1], 2, places=5)
        self.assertGreaterEqual(clock.now(), release)

    def test_failed_refresh_does_not_reuse_the_old_fast_probe(self):
        replies = iter([self.snapshot(day='2026-11-03'), {'e': 'EXC'}])
        _, calls, _, snapshot = self.run_wait(datetime(2026, 11, 2, 21, 59, 40),
            delay=.1, response=lambda: next(replies), capture=True)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(snapshot)

    def test_fast_snapshot_age_is_based_on_completion_time(self):
        clock, _, _, snapshot = self.run_wait(datetime(2026, 11, 2, 21, 59, 40),
            delay=.1, response=self.snapshot(day='2026-11-03'), capture=True)
        self.assertIsNotNone(snapshot)
        self.assertAlmostEqual(clock.monotonic() - snapshot[1], 4.4, places=5)

    def test_early_login_expiry_stops_before_captcha_preparation(self):
        prepare = Mock()
        with self.assertRaises(auto.RequestError):
            self.run_wait(datetime(2026, 11, 2, 21, 59, 40), response={'e': '10043'}, before_release=prepare)
        prepare.assert_not_called()

    def test_skips_extra_request_when_release_is_less_than_two_seconds_away(self):
        clock, calls, release = self.run_wait(datetime(2026, 11, 2, 21, 59, 59, 500000))
        self.assertEqual(calls, [])
        self.assertGreaterEqual(clock.now(), release)

    def test_late_start_does_not_warm_or_wait(self):
        clock, calls, _ = self.run_wait(datetime(2026, 11, 2, 22, 0, 5))
        self.assertEqual(calls, [])
        self.assertEqual(clock.elapsed, 0)

    def test_warmup_uses_the_available_safe_window(self):
        _, calls, _ = self.run_wait(datetime(2026, 11, 2, 21, 59, 57))
        self.assertAlmostEqual(calls[0][2]["timeout"], 2.5)

    def test_transient_warmup_error_does_not_cancel_booking(self):
        clock, calls, release = self.run_wait(datetime(2026, 11, 2, 21, 59, 58), response={"e": "EXC"})
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(clock.now(), release)

    def test_expired_login_during_warmup_stops_before_submission(self):
        with self.assertRaises(auto.RequestError):
            self.run_wait(datetime(2026, 11, 2, 21, 59, 58), response={"e": "10042"})

    def test_model_warmup_runs_detection_and_ocr_without_a_real_captcha(self):
        engine = object.__new__(CaptchaEngine)
        engine.yolo = Mock()
        engine.ocr = Mock()
        warmup = getattr(engine, "warmup", None)
        self.assertIsNotNone(warmup, "loading ONNX sessions is not inference warmup")
        warmup()
        engine.yolo.infer.assert_called_once()
        engine.ocr.read.assert_called_once()

    def test_slow_warmup_cannot_hold_the_release_thread_past_deadline(self):
        finished = Event()
        def slow_response(*args, **kwargs):
            finished.wait(2.7)
            return {"e": 0}
        started = time.monotonic()
        release = datetime.now() + timedelta(seconds=2.05)
        try:
            with patch.object(auto, "_req", side_effect=slow_response) as query, redirect_stdout(io.StringIO()):
                snapshot = auto.wait_for_release("synthetic", 57, release, "2026-11-03")
            self.assertIsNone(snapshot)
            query.assert_called_once()
            self.assertLess(time.monotonic() - started, 2.5,
                            "read timeout is not a wall-clock limit for slow response bodies")
        finally:
            finished.set()

    def run_last_half_second_response(self, response):
        release = datetime.now() + timedelta(seconds=2.1)
        returned = []
        started = time.monotonic()
        def request(*args, **kwargs):
            time.sleep(max(0, (release - datetime.now()).total_seconds() - .2))
            returned.append(datetime.now())
            return response
        with patch.object(auto, "_req", side_effect=request) as query, redirect_stdout(io.StringIO()):
            snapshot = auto.wait_for_release("synthetic", 57, release, "2026-11-03")
        query.assert_called_once()
        self.assertEqual(len(returned), 1)
        self.assertLess(returned[0], release)
        self.assertLess(time.monotonic() - started, 2.5, "late response recovery must not add a wait")
        return snapshot

    def test_last_half_second_completed_query_is_not_discarded(self):
        periods = ["20:10-21:10", "21:10-22:10"]
        snapshot = self.run_last_half_second_response(self.snapshot(day="2026-11-03", times=periods))
        self.assertIsNotNone(snapshot, "an already completed calendar must avoid a duplicate live query")
        self.assertEqual([slot["time"] for slot in snapshot[0]], periods)
        self.assertTrue(all(slot["date"] == "2026-11-03" for slot in snapshot[0]))
        self.assertGreaterEqual(time.monotonic() - snapshot[1], 0)
        self.assertLess(time.monotonic() - snapshot[1], .5)

    def test_last_half_second_login_expiry_stops_before_submission(self):
        for code in ("10042", "10043"):
            with self.subTest(code=code):
                with self.assertRaises(auto.RequestError) as raised:
                    self.run_last_half_second_response({"e": code})
                self.assertEqual(raised.exception.code, code)

    def test_last_half_second_refusal_is_not_an_available_snapshot(self):
        self.assertIsNone(self.run_last_half_second_response({"e": 1, "m": "预约日期未达到"}))

    def test_captcha_preparation_starts_while_the_query_is_pending(self):
        entered, finish, returned = Event(), Event(), Event()
        callback_times = []
        def request(*args, **kwargs):
            entered.set()
            finish.wait(4)
            returned.set()
            return {'e': 0, 'd': {}}
        def prepare():
            self.assertTrue(entered.is_set())
            self.assertFalse(returned.is_set())
            callback_times.append(datetime.now())
            finish.set()
        release = datetime.now() + timedelta(seconds=3.1)
        try:
            with patch.object(auto, '_req', side_effect=request), redirect_stdout(io.StringIO()):
                auto.wait_for_release('synthetic', 57, release, '2026-11-03', before_release=prepare)
        finally:
            finish.set()
        self.assertEqual(len(callback_times), 1)
        self.assertLess(callback_times[0], release)

    def snapshot(self, *, status=5, day="2026-10-30", times=None):
        return {"e": 0, "d": {"synthetic": [
            {"date": day, "time_id": index + 1, "sub_id": 2, "abscissa": "Court 1", "yaxis": period,
             "row": {"status": status, "margin": 1 if status == 5 else 0, "total": 1}}
            for index, period in enumerate(times or ["19:10-20:10"])]}}

    def test_confirmed_available_snapshot_avoids_one_post_release_query(self):
        fetch = Mock(side_effect=RuntimeError("should use the fresh pre-release query"))
        code, calls, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
            fetch=fetch, warm_response=self.snapshot())
        self.assertEqual(code, 0)
        fetch.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0][0], datetime(2026, 10, 29, 22))

    def test_wrong_date_or_unavailable_snapshot_still_queries_after_release(self):
        for response in [self.snapshot(day="2026-10-29"), self.snapshot(status=4)]:
            fetch = Mock(return_value=[{"court": "Court 1", "time": "19:10-20:10", "period": 1,
                                        "sub": 2, "margin": 1, "total": 1}])
            code, _, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                fetch=fetch, warm_response=response)
            self.assertEqual(code, 0)
            fetch.assert_called_once()

    def test_incomplete_two_period_snapshot_never_skips_the_live_query(self):
        fetch = Mock(side_effect=RuntimeError("stop test after the live query"))
        code, calls, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
            times="20:10-21:10;21:10-22:10", fetch=fetch,
            warm_response=self.snapshot(times=["20:10-21:10"]))
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        fetch.assert_called_once()

    def test_snapshot_older_than_five_seconds_falls_back_to_live_query(self):
        slot = {"court": "Court 1", "time": "19:10-20:10", "period": 1,
                "sub": 2, "margin": 1, "total": 1, "date": "2026-10-30"}
        fetch = Mock(return_value=[slot])
        with patch.object(auto, "wait_for_release", return_value=([slot], -10)):
            code, _, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22), fetch=fetch)
        self.assertEqual(code, 0)
        fetch.assert_called_once()

    def test_rejected_snapshot_is_not_reused_on_the_next_attempt(self):
        slot = {"court": "Court 2", "time": "19:10-20:10", "period": 1,
                "sub": 3, "margin": 1, "total": 1}
        fetch = Mock(return_value=[slot])
        code, calls, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
            fetch=fetch, warm_response=self.snapshot(),
            responses=[{"e": 1, "m": "该场已满"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 0)
        self.assertEqual([call[2] for call in calls], ["Court 1", "Court 2"])
        fetch.assert_called_once()

    def test_two_periods_on_different_courts_still_require_live_query(self):
        response = self.snapshot(times=["20:10-21:10", "21:10-22:10"])
        response["d"]["synthetic"][1]["abscissa"] = "Court 2"
        fetch = Mock(side_effect=RuntimeError("stop test after the live query"))
        code, calls, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
            times="20:10-21:10;21:10-22:10", fetch=fetch, warm_response=response)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        fetch.assert_called_once()

    def test_simulated_seven_second_query_is_removed_from_release_path(self):
        def slow_query(*args):
            auto.time.sleep(7)
            return [{"court": "Court 1", "time": "19:10-20:10", "period": 1,
                     "sub": 2, "margin": 1, "total": 1}]
        wall = datetime(2026, 10, 29, 21, 59, 40)
        baseline, old_calls, _, _ = flow.FlowTests().run_flow(wall, fetch=slow_query, max_wait=30)
        optimized, new_calls, _, _ = flow.FlowTests().run_flow(wall, fetch=slow_query,
            warm_response=self.snapshot(), max_wait=30)
        self.assertEqual((baseline, optimized), (0, 0))
        self.assertEqual((old_calls[0][0] - new_calls[0][0]).total_seconds(), 7)


if __name__ == "__main__":
    unittest.main()
