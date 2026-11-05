"""Refresh an expired prechecked challenge while a live query is pending."""
from datetime import datetime
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import auto
from auto_grab import RequestError
from captcha_prefetch import CaptchaPrefetch
import test_booking_flow as flow


PERIODS = ["20:10-21:10", "21:10-22:10"]


class QueryCaptchaRefreshTests(unittest.TestCase):
    def run_case(self, *, slow=False, failure=False, max_wait=30):
        refreshed, returned = Event(), Event()
        jobs, checks = [], []

        def get(*args):
            number = get_mock.call_count
            if number == 2:
                self.assertFalse(returned.is_set(), "refresh should overlap the pending query")
                refreshed.set()
            return {"token": f"challenge-{number}"}

        get_mock = Mock(side_effect=get)

        def solve(*args, **kwargs):
            checks.append((kwargs.get("initial_cap"), returned.is_set()))
            if len(checks) == 1:
                auto.time.sleep(.2)
            return "fresh-verification"

        def start(fetch):
            if not jobs:
                result = fetch()
                job = SimpleNamespace(peek=lambda: result, take=lambda **kwargs: result)
            else:
                job = CaptchaPrefetch(fetch, clock=auto.time.monotonic)
            jobs.append(job)
            return job

        def query(*args):
            if failure:
                raise RequestError({"e": "10042"})
            if slow:
                auto.time.sleep(7.8)
                if max_wait > 7.8:
                    self.assertTrue(refreshed.wait(1), "new image should be ready before query returns")
            returned.set()
            return [dict(flow.SLOT, time=period, period=index, date="2026-10-30")
                    for index, period in enumerate(PERIODS, 1)]

        try:
            with patch.object(auto, "Thread", side_effect=lambda target, **kwargs: SimpleNamespace(start=target)), \
                 patch.object(auto, "CaptchaPrefetch", side_effect=start):
                result = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                    times=";".join(PERIODS), fetch=query, max_wait=max_wait,
                    captcha_get=get_mock, solver=Mock(side_effect=solve))
                for job in jobs[1:]:
                    job._completed.result(timeout=1)
        finally:
            returned.set()
        return result, get_mock, checks

    def test_slow_query_refreshes_image_then_checks_normally_after_query(self):
        (code, calls, _, _), get, checks = self.run_case(slow=True)
        self.assertEqual(code, 0)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(checks), 2)
        self.assertEqual(checks[1], ({"token": "challenge-2"}, True))
        self.assertEqual(len(calls), 1)
        self.assertEqual([slot["time"] for slot in calls[0][3]["slots"]], PERIODS)

    def test_fast_query_keeps_valid_verification_without_an_extra_get_or_check(self):
        (code, calls, _, _), get, checks = self.run_case()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(checks), 1)

    def test_login_failure_does_not_refresh_or_submit(self):
        (code, calls, _, _), get, checks = self.run_case(failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(get.call_count, 1)

    def test_expired_run_budget_does_not_refresh_or_submit(self):
        (code, calls, _, _), get, checks = self.run_case(slow=True, max_wait=1)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(get.call_count, 1)

    def test_failed_completed_preparation_is_cleared_before_query_retry(self):
        get = Mock(side_effect=[RequestError({"e": "502"}), {"token": "fresh"}])
        immediate = lambda target, **kwargs: SimpleNamespace(start=target)
        with patch.object(auto, "Thread", side_effect=immediate), \
             patch("captcha_prefetch.Thread", side_effect=immediate):
            code, calls, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=get, solver=Mock(return_value="fresh-verification"))
        self.assertEqual(code, 0)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(calls), 1)
