"""Bounded recovery of an explicitly unopened date; never make live bookings."""
from datetime import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import auto
import test_booking_flow as flow
import test_captcha_prefetch as prefetch_cases

TIMES = ["20:10-21:10", "21:10-22:10"]


def snapshot():
    return {"e": 0, "d": {"synthetic": [
        {"date": "2026-10-30", "time_id": index + 1, "sub_id": 2,
         "abscissa": "Court 1", "yaxis": period, "row": {"status": 5, "margin": 1, "total": 1}}
        for index, period in enumerate(TIMES)]}}


def slots():
    return [dict(flow.SLOT, time=period, period=index + 1) for index, period in enumerate(TIMES)]


class ReleaseRecoveryTests(unittest.TestCase):
    def run_case(self, responses, fetch, **kwargs):
        with patch.object(auto, "Thread", side_effect=lambda target, **kw: SimpleNamespace(start=target)), \
             patch.object(auto, "CaptchaPrefetch", side_effect=prefetch_cases.PrefetchFlowTests.immediate_job):
            return flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                responses=responses, fetch=fetch, times=";".join(TIMES), warm_response=snapshot(),
                max_wait=kwargs.pop("max_wait", 30), **kwargs)

    def test_aligned_first_acceptance_measures_response_with_both_periods(self):
        fetch = Mock(side_effect=AssertionError("prepared pair should avoid a live query"))
        prepare = Mock(return_value=(22.05, {"code": "CLOCK_ALIGNED",
            "clock_offset_ms": -2000, "clock_guard_ms": 50}))
        code, launches, output, _ = self.run_case([{"e": 0, "d": {"id": 101}}], fetch,
            clock_preparation=prepare, release_login_delay=10,
            launch_observer=lambda: auto.time.sleep(.75))
        self.assertEqual(code, 0)
        fetch.assert_not_called()
        self.assertEqual(len(launches), 1)
        self.assertEqual([slot["time"] for slot in launches[0][3]["slots"]], TIMES)
        timing = next(line for line in output.splitlines() if '"stage": "launch"' in line)
        event = json.loads(timing.removeprefix("[timing] "))
        self.assertEqual(event["reason"], "ACCEPTED")
        self.assertEqual(event["release_elapsed_ms"], 750.0)

    def test_explicit_not_open_yet_uses_one_cached_full_selection_and_new_captcha(self):
        fetch = Mock(side_effect=AssertionError("do not repeat the slow calendar query"))
        solver = Mock(side_effect=["first-verification", "second-verification"])
        get = Mock(side_effect=[{"token": "first"}, {"token": "second"}])
        code, launches, _, _ = self.run_case([
            {"e": 1, "m": "预约日期未达到"}, {"e": 0, "d": {"id": 101}}], fetch,
            solver=solver, captcha_get=get)
        self.assertEqual(code, 0)
        fetch.assert_not_called()
        self.assertEqual(len(launches), 2)
        self.assertEqual(launches[0][3], launches[1][3])
        self.assertEqual(len(launches[1][3]["slots"]), 2)
        self.assertEqual(get.call_count, 2)
        self.assertEqual([call.kwargs["initial_cap"]["token"] for call in solver.call_args_list],
                         ["first", "second"])

    def test_second_not_open_yet_returns_to_live_query(self):
        fetch = Mock(return_value=slots())
        code, launches, _, _ = self.run_case([
            {"e": 1, "m": "预约日期未达到"}, {"e": 1, "m": "预约日期未达到"},
            {"e": 0, "d": {"id": 101}}], fetch)
        self.assertEqual(code, 0)
        self.assertEqual(len(launches), 3)
        fetch.assert_called_once()

    def test_unknown_refusal_does_not_enable_cached_retry(self):
        fetch = Mock(return_value=slots())
        code, launches, _, _ = self.run_case([
            {"e": 1, "m": "other refusal"}, {"e": 0, "d": {"id": 101}}], fetch)
        self.assertEqual(code, 0)
        self.assertEqual(len(launches), 2)
        fetch.assert_called_once()

    def test_stale_candidate_after_slow_refusal_requires_live_query(self):
        fetch = Mock(return_value=slots())
        calls = []
        def launch():
            calls.append(True)
            if len(calls) == 1:
                auto.time.sleep(11)
        code, launches, _, _ = self.run_case([
            {"e": 1, "m": "预约日期未达到"}, {"e": 0, "d": {"id": 101}}], fetch,
            launch_observer=launch)
        self.assertEqual(code, 0)
        self.assertEqual(len(launches), 2)
        fetch.assert_called_once()

    def test_uncertain_response_stops_before_any_cached_retry(self):
        fetch = Mock(side_effect=AssertionError("uncertain write must not be retried"))
        with patch.object(auto, "find_reservation", return_value=None):
            code, launches, _, _ = self.run_case([{"e": "EXC", "m": "lost response"}], fetch)
        self.assertEqual(code, 1)
        self.assertEqual(len(launches), 1)
        fetch.assert_not_called()

    def test_retry_does_not_submit_after_run_deadline_during_captcha(self):
        fetch = Mock(side_effect=AssertionError("deadline is over"))
        checks = []
        def solve(*args, **kwargs):
            checks.append(True)
            if len(checks) == 2:
                auto.time.sleep(.5)
            return "fresh"
        starts = []
        def launch():
            starts.append(True)
            if len(starts) == 1:
                auto.time.sleep(.7)
        code, launches, _, _ = self.run_case([
            {"e": 1, "m": "预约日期未达到"}, {"e": 0, "d": {"id": 101}}], fetch,
            max_wait=1, solver=Mock(side_effect=solve), launch_observer=launch)
        self.assertEqual(code, 1)
        self.assertEqual(len(launches), 1)

    def test_candidate_expiring_during_retry_captcha_is_not_submitted(self):
        fetch = Mock(return_value=slots())
        checks = []
        def solve(*args, **kwargs):
            checks.append(True)
            if len(checks) == 2:
                auto.time.sleep(11)
            return "fresh"
        code, launches, _, _ = self.run_case([
            {"e": 1, "m": "预约日期未达到"}, {"e": 0, "d": {"id": 101}}], fetch,
            solver=Mock(side_effect=solve))
        self.assertEqual(code, 0)
        self.assertEqual(len(launches), 2)
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
