"""Bounded one-shot challenge GET; no live services or speculative submissions."""
from datetime import datetime, timedelta
import importlib.util
from threading import Event
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

import auto
from auto_grab import RequestError
import test_booking_flow as flow


class CaptchaPrefetchTests(unittest.TestCase):
    def test_login_expiry_and_timeout_codes_are_not_recognition_retries(self):
        for code in ("10043", "408"):
            with self.subTest(code=code), \
                 patch.object(auto, "get_captcha", return_value={"token": "synthetic", "secretKey": "1234567890abcdef"}) as get, \
                 patch.object(auto, "infer_captcha", return_value=[[1, 2], [3, 4], [5, 6], [7, 8]]), \
                 patch.object(auto, "verify_captcha", return_value={"e": code}) as verify:
                with self.assertRaises(RequestError) as raised:
                    auto.solve_captcha(Mock(), "fake")
                self.assertEqual(raised.exception.code, code)
                get.assert_called_once()
                verify.assert_called_once()

    def job(self, fetch, **kwargs):
        self.assertIsNotNone(importlib.util.find_spec("captcha_prefetch"), "parallel challenge GET is required")
        from captcha_prefetch import CaptchaPrefetch
        return CaptchaPrefetch(fetch, **kwargs)

    def test_challenge_is_fetched_once_and_consumed_once(self):
        fetch = Mock(return_value={"token": "synthetic"})
        job = self.job(fetch)
        self.assertEqual(job.take(), {"token": "synthetic"})
        self.assertIsNone(job.take())
        fetch.assert_called_once_with()

    def peek(self, job):
        peek = getattr(job, "peek", None)
        self.assertTrue(callable(peek), "challenge preparation needs a nonblocking peek")
        return peek()

    def test_peek_does_not_wait_for_pending_preparation(self):
        entered, release = Event(), Event()
        def fetch():
            entered.set()
            release.wait(2)
            return {"token": "synthetic"}
        job = self.job(fetch)
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertIsNone(self.peek(job))
            self.assertLess(time.monotonic() - started, .3)
        finally:
            release.set()
            job._completed.result(timeout=1)

    def test_peek_does_not_consume_or_refetch_a_completed_challenge(self):
        challenge = {"token": "synthetic"}
        fetch = Mock(return_value=challenge)
        job = self.job(fetch)
        job._completed.result(timeout=1)
        self.assertIs(self.peek(job), challenge)
        self.assertIs(self.peek(job), challenge)
        self.assertIs(job.take(), challenge)
        self.assertIsNone(self.peek(job))
        fetch.assert_called_once_with()

    def test_peek_respects_the_same_freshness_boundary_as_take(self):
        clock = Mock(return_value=0.0)
        challenge = {"token": "synthetic"}
        job = self.job(lambda: challenge, clock=clock)
        job._completed.result(timeout=1)
        clock.return_value = 8.0
        self.assertIs(self.peek(job), challenge)
        clock.return_value = 8.001
        self.assertIsNone(self.peek(job))
        self.assertIsNone(job.take())
        self.assertIsNone(self.peek(job))

    def test_peek_propagates_completed_errors_without_consuming_them(self):
        error = RequestError({"e": "10042"})
        fetch = Mock(side_effect=error)
        job = self.job(fetch)
        with self.assertRaises(RequestError):
            job._completed.result(timeout=1)
        for _ in range(2):
            with self.assertRaises(RequestError) as raised:
                self.peek(job)
            self.assertIs(raised.exception, error)
        with self.assertRaises(RequestError) as raised:
            job.take()
        self.assertIs(raised.exception, error)
        self.assertIsNone(self.peek(job))
        fetch.assert_called_once_with()

    def test_completed_old_challenge_is_discarded(self):
        clock = Mock(return_value=0.0)
        job = self.job(lambda: {"token": "synthetic"}, clock=clock)
        job._completed.result(timeout=1)
        clock.return_value = 9.0
        self.assertIsNone(job.take())

    def test_server_error_is_not_hidden_as_an_empty_challenge(self):
        job = self.job(Mock(side_effect=RequestError({"e": "10042"})))
        with self.assertRaises(RequestError) as raised:
            job.take()
        self.assertFalse(raised.exception.retryable)

    def test_slow_get_has_a_wall_clock_limit_and_is_not_refetched(self):
        release = Event()
        fetch = Mock(side_effect=lambda: release.wait(1))
        job = self.job(fetch, wait_limit=.03)
        started = time.monotonic()
        try:
            with self.assertRaises(RequestError) as raised:
                job.take()
            self.assertEqual(raised.exception.code, "CAPTCHA_TIMEOUT")
            self.assertFalse(raised.exception.retryable)
            self.assertLess(time.monotonic() - started, .5)
            self.assertIsNone(job.take())
            fetch.assert_called_once_with()
        finally:
            release.set()


class PrefetchFlowTests(unittest.TestCase):
    def test_contrast_budget_is_only_given_to_early_preparation(self):
        for wall, delay, flags, expected in (
                (datetime(2026, 10, 29, 21, 59, 40), 0, (), True),
                (datetime(2026, 10, 29, 22), 0, ("--now", "--date", "2026-10-30"), False),
                (datetime(2026, 10, 29, 21, 59, 40), 5, (), False)):
            with self.subTest(wall=wall, delay=delay, flags=flags):
                observed = []

                def get(*args):
                    auto.time.sleep(delay)
                    return {"token": "synthetic"}

                def recognize(*args, **kwargs):
                    observed.append((auto.time.monotonic(), kwargs.get("contrast_deadline")))
                    return [[1, 2], [3, 4], [5, 6], [7, 8]]

                with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
                    code, _, _, _ = flow.FlowTests().run_flow(wall, captcha_get=get,
                        inference=recognize, solver=Mock(return_value="verified"), extra_flags=flags)
                self.assertEqual(code, 0)
                self.assertTrue(observed)
                for now, deadline in observed:
                    self.assertEqual(deadline is not None, expected)
                    if deadline is not None:
                        self.assertGreater(deadline, now)

    @staticmethod
    def immediate_job(fetch):
        result = fetch()
        return SimpleNamespace(take=lambda **kwargs: result, peek=lambda: None)

    def test_unavailable_starts_next_candidate_captcha_before_terminal_output(self):
        get = Mock(side_effect=[{"token": "first"}, {"token": "second"}])
        solver = Mock(return_value="fresh-verification")
        launches = []
        checks = []

        class Terminal(flow.io.StringIO):
            def write(self, value):
                if '"stage": "launch"' in value:
                    checks.append((get.call_count, len(launches)))
                return super().write(value)

        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            result, calls, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
                fetch=lambda *args: [dict(flow.SLOT), dict(flow.SLOT, court="Court 2", sub=3)],
                captcha_get=get, solver=solver, output_stream=Terminal(),
                launch_observer=lambda: launches.append(True), responses=[
                    {"e": 1, "m": "该场已满"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(result, 0)
        self.assertEqual(checks, [(2, 2)], "next captcha and submission should precede buffered feedback")
        self.assertEqual(get.call_count, 2)
        self.assertEqual([call.kwargs["initial_cap"]["token"] for call in solver.call_args_list],
                         ["first", "second"])
        self.assertEqual([call[2] for call in calls], ["Court 1", "Court 2"])

    def test_quota_refusal_does_not_prepare_another_candidate(self):
        get = Mock(return_value={"token": "first"})
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            result, calls, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
                fetch=lambda *args: [dict(flow.SLOT), dict(flow.SLOT, court="Court 2", sub=3)],
                captcha_get=get, solver=Mock(return_value="verification"),
                responses=[{"e": 1, "m": "预约次数已满"}])
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        get.assert_called_once()

    def test_explicit_rejection_prepares_fresh_challenge_during_next_query(self):
        get = Mock(side_effect=[{"token": "first"}, {"token": "second"}])
        solver = Mock(return_value="verified")
        query_get_counts = []

        def query(*args):
            query_get_counts.append(get.call_count)
            return [dict(flow.SLOT)]

        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
                fetch=query, captcha_get=get, solver=solver, responses=[
                    {"e": 1, "m": "该场已满"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 0)
        self.assertEqual(query_get_counts, [1, 2])
        self.assertEqual(len(launches), 2)
        self.assertEqual([call.kwargs["initial_cap"]["token"] for call in solver.call_args_list],
                         ["first", "second"])

    def test_rearmed_challenge_is_retained_across_empty_queries(self):
        get = Mock(side_effect=[{"token": "first"}, {"token": "second"}])
        solver = Mock(return_value="verified")
        queries = 0

        def query(*args):
            nonlocal queries
            queries += 1
            return [] if queries in (2, 3) else [dict(flow.SLOT)]

        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
                fetch=query, captcha_get=get, solver=solver, responses=[
                    {"e": 1, "m": "该场已满"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 0)
        self.assertEqual(queries, 4)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(launches), 2)
        self.assertEqual(solver.call_args.kwargs["initial_cap"]["token"], "second")

    def test_uncertain_submission_does_not_start_another_preparation(self):
        get = Mock(return_value={"token": "first"})
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job), \
             patch.object(auto, "find_reservation", return_value=None):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
                captcha_get=get, responses=[{"e": "EXC", "m": "ReadTimeoutError"}])
        self.assertEqual(code, 1)
        self.assertEqual(len(launches), 1)
        get.assert_called_once()

    def test_pending_retry_image_is_not_duplicated_by_empty_poll(self):
        entered, finish = Event(), Event()
        gets, queries = [], []

        def get(*args):
            gets.append(len(gets) + 1)
            if len(gets) == 2:
                entered.set()
                self.assertTrue(finish.wait(2))
            return {"token": str(len(gets))}

        def query(*args):
            queries.append(len(queries) + 1)
            if len(queries) == 2:
                self.assertTrue(entered.wait(1))
                return []
            if len(queries) == 3:
                self.assertEqual(len(gets), 2)
                finish.set()
            return [dict(flow.SLOT)]

        try:
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
                fetch=query, captcha_get=get, responses=[
                    {"e": 1, "m": "该场已满"}, {"e": 0, "d": {"id": 101}}])
        finally:
            finish.set()
        self.assertEqual(code, 0)
        self.assertEqual(gets, [1, 2])
        self.assertEqual(len(launches), 2)

    def test_available_release_snapshot_reuses_single_prepared_challenge(self):
        get = Mock(return_value={"token": "synthetic"})
        response = {"e": 0, "d": {"synthetic": [{"date": "2026-10-30", "time_id": 1,
            "sub_id": 2, "abscissa": "Court 1", "yaxis": "19:10-20:10",
            "row": {"status": 5, "margin": 1, "total": 1}}]}}
        result, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
            warm_response=response, captcha_get=get)
        self.assertEqual(result, 0)
        self.assertEqual(len(launches), 1)
        get.assert_called_once()

    def test_no_new_get_is_started_for_every_empty_poll(self):
        get = Mock(return_value=None)
        result, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
            fetch=lambda *a: [], captcha_get=get)
        self.assertEqual(result, 1)
        self.assertEqual(launches, [])
        get.assert_called_once()

    def test_get_overlaps_first_live_query_and_starts_after_release(self):
        started = Event()
        observed = []
        challenge = {"token": "synthetic"}

        def captcha_get(*args):
            observed.append(auto.datetime.now())
            started.set()
            return challenge

        def query(*args):
            self.assertTrue(started.wait(1), "captcha GET should start before the live query finishes")
            return [dict(flow.SLOT)]

        solver = Mock(return_value="synthetic-verified")
        result, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
            fetch=query, solver=solver, captcha_get=captcha_get)
        self.assertEqual(result, 0)
        self.assertEqual(len(launches), 1)
        self.assertGreaterEqual(observed[0], datetime(2026, 10, 29, 22))
        self.assertIs(solver.call_args.kwargs["initial_cap"], challenge)
        self.assertEqual(solver.call_args.kwargs.get("initial_points"), [[1, 2], [3, 4], [5, 6], [7, 8]],
                         "local inference should also finish while waiting for availability")

    def test_scheduled_preparation_starts_before_release_but_submission_waits(self):
        get_times, check_times = [], []
        challenge = {"token": "synthetic"}
        def get(*args):
            get_times.append(auto.datetime.now())
            return challenge
        def verify(*args, **kwargs):
            check_times.append(auto.datetime.now())
            return "synthetic-verified"
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=get, solver=verify)
        self.assertEqual(code, 0)
        self.assertEqual(len(get_times), 1)
        self.assertLess(get_times[0], datetime(2026, 10, 29, 22))
        self.assertLess(check_times[0], datetime(2026, 10, 29, 22))
        self.assertGreaterEqual(launches[0][0], datetime(2026, 10, 29, 22))

    def test_scheduled_verified_result_is_used_once_without_another_check(self):
        moments = []
        def verify(*args, **kwargs):
            moments.append(auto.datetime.now())
            return "synthetic-verified"
        solver = Mock(side_effect=verify)
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=Mock(return_value={"token": "synthetic"}), solver=solver)
        self.assertEqual(code, 0)
        solver.assert_called_once()
        self.assertLess(moments[0], datetime(2026, 10, 29, 22))
        self.assertGreaterEqual(launches[0][0], datetime(2026, 10, 29, 22))

    def test_failed_early_check_retries_once_before_release(self):
        moments = []
        def verify(*args, **kwargs):
            moments.append(auto.datetime.now())
            return None if len(moments) == 1 else "fresh-verification"
        solver = Mock(side_effect=verify)
        get = Mock(side_effect=[{"token": "first"}, {"token": "second"}])
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=get, solver=solver)
        self.assertEqual(code, 0)
        self.assertEqual(len(moments), 2)
        self.assertLess(moments[0], datetime(2026, 10, 29, 22))
        self.assertLess(moments[1], datetime(2026, 10, 29, 22))
        self.assertEqual(get.call_count, 2)
        self.assertEqual([call.kwargs["initial_cap"]["token"] for call in solver.call_args_list],
                         ["first", "second"])
        self.assertEqual(len(launches), 1)

    def test_two_failed_early_checks_use_fresh_challenge_after_release(self):
        moments = []
        def verify(*args, **kwargs):
            moments.append(auto.datetime.now())
            return None if len(moments) <= 2 else "fresh-verification"
        solver = Mock(side_effect=verify)
        get = Mock(side_effect=[{"token": "first"}, {"token": "second"}])
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=get, solver=solver)
        self.assertEqual(code, 0)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(moments), 3)
        self.assertTrue(all(moment < datetime(2026, 10, 29, 22) for moment in moments[:2]))
        self.assertGreaterEqual(moments[2], datetime(2026, 10, 29, 22))
        self.assertNotIn("initial_cap", solver.call_args.kwargs)
        self.assertEqual(len(launches), 1)

    def test_two_incomplete_images_use_third_before_release(self):
        get = Mock(side_effect=[{"token": "first"}, {"token": "second"}, {"token": "third"}])
        infer = Mock(side_effect=[[], [], [[1, 2], [3, 4], [5, 6], [7, 8]]])
        solver = Mock(return_value="verified-third")
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=get, inference=infer, solver=solver)
        self.assertEqual(code, 0)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(infer.call_count, 3)
        solver.assert_called_once()
        self.assertEqual(solver.call_args.kwargs["initial_cap"]["token"], "third")
        self.assertEqual(launches[0][0], datetime(2026, 10, 29, 22))

    def test_three_slow_images_can_finish_verification_before_release(self):
        release = datetime(2026, 10, 29, 22)
        response = {"e": 0, "d": {"synthetic": [{"date": "2026-10-30", "time_id": 1,
            "sub_id": 2, "abscissa": "Court 1", "yaxis": "19:10-20:10",
            "row": {"status": 5, "margin": 1, "total": 1}}]}}
        for get_delay in (.9, 1.1):
            with self.subTest(get_delay=get_delay):
                gets, checks = [], []
                def get(*args):
                    gets.append(auto.datetime.now())
                    auto.time.sleep(get_delay)
                    return {"token": "third" if len(gets) == 3 else "incomplete"}
                def recognize(engine, cap, **kwargs):
                    return [[1, 2], [3, 4], [5, 6], [7, 8]] if cap["token"] == "third" else []
                def verify(*args, **kwargs):
                    checks.append(auto.datetime.now())
                    auto.time.sleep(.2)
                    return "verified"
                with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job), \
                     patch.object(auto, "Thread", side_effect=lambda *, target, **kwargs: SimpleNamespace(start=target)):
                    code, launches, _, _ = flow.FlowTests().run_flow(
                        datetime(2026, 10, 29, 21, 59, 40), warm_response=response,
                        captcha_get=get, inference=recognize, solver=Mock(side_effect=verify))
                self.assertEqual(code, 0)
                self.assertEqual(len(gets), 3)
                self.assertEqual(len(checks), 1)
                self.assertLessEqual(gets[0], release - timedelta(seconds=4))
                self.assertGreaterEqual(checks[0], release - timedelta(seconds=2))
                self.assertLess(checks[0] + timedelta(seconds=.2), release)
                self.assertEqual(launches[0][0], release,
                                 "slow recognition retries must not add a post-release captcha check")

    def test_early_image_does_not_make_verification_earlier_than_two_seconds(self):
        gets, checks = [], []
        release = datetime(2026, 10, 29, 22)
        def get(*args):
            gets.append(auto.datetime.now())
            return {"token": "ready"}
        def verify(*args, **kwargs):
            checks.append(auto.datetime.now())
            auto.time.sleep(.2)
            return "verified"
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=get, solver=Mock(side_effect=verify))
        self.assertEqual(code, 0)
        self.assertLessEqual(gets[0], release - timedelta(seconds=4))
        self.assertEqual(checks, [release - timedelta(seconds=2)])
        self.assertEqual(launches[0][0], release)

    def test_verified_result_older_than_two_seconds_is_discarded(self):
        solver = Mock(return_value="synthetic-verified")
        def slow_query(*args):
            auto.time.sleep(3)
            return [dict(flow.SLOT)]
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=Mock(return_value={"token": "consumed"}), solver=solver,
                fetch=slow_query, max_wait=6)
        self.assertEqual(code, 0)
        self.assertEqual(solver.call_count, 2)
        self.assertNotIn("initial_cap", solver.call_args.kwargs)
        self.assertEqual(len(launches), 1)

    def test_preverified_result_is_not_reused_for_another_court(self):
        moments = []
        def verify(*args, **kwargs):
            moments.append(auto.datetime.now())
            return "synthetic-verified"
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=Mock(return_value={"token": "single-use"}), solver=Mock(side_effect=verify),
                times="19:10-20:10", fetch=lambda *a: [dict(flow.SLOT), dict(flow.SLOT, court="Court 2")],
                responses=[{"e": 1, "m": "该场已满"}, {"e": 0, "d": {"id": 101}}])
        self.assertEqual(code, 0)
        self.assertEqual(len(launches), 2)
        self.assertEqual(len(moments), 2)
        self.assertLess(moments[0], datetime(2026, 10, 29, 22))
        self.assertGreaterEqual(moments[1], datetime(2026, 10, 29, 22))

    def test_early_verification_login_failure_stops_before_submission(self):
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=Mock(return_value={"token": "synthetic"}),
                solver=Mock(side_effect=RequestError({"e": "10042"})))
        self.assertEqual(code, 1)
        self.assertEqual(launches, [])

    def test_failed_release_wait_stops_late_background_verification(self):
        entered, allow = Event(), Event()
        jobs = []
        job_type = auto.CaptchaPrefetch
        solver = Mock(return_value="synthetic-verified")
        clock = flow.Clock(datetime(2026, 10, 29, 21, 59, 58, 500000))
        def get(*args):
            entered.set()
            self.assertTrue(allow.wait(2))
            return {"token": "synthetic"}
        def start_job(fetch):
            job = job_type(fetch)
            jobs.append(job)
            return job
        def failed_wait(*args, before_release=None):
            before_release()
            self.assertTrue(entered.wait(1))
            raise RequestError({"e": "10042"})
        with patch.object(auto, "CaptchaPrefetch", side_effect=start_job), \
             patch.object(auto, "wait_for_release", side_effect=failed_wait), \
             patch.object(auto, "solve_captcha", solver), \
             patch.object(auto, "datetime", SimpleNamespace(now=clock.now)), patch.object(auto, "time", clock):
            try:
                code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                    captcha_get=get, solver=solver)
                self.assertEqual(code, 1)
                self.assertEqual(launches, [])
            finally:
                allow.set()
            jobs[0]._completed.result(timeout=1)
            solver.assert_not_called()

    def test_incomplete_recognition_is_retried_once_while_still_before_release(self):
        attempts = []
        challenges = [{"token": "incomplete"}, {"token": "complete"}]
        def get(*args):
            attempts.append(auto.datetime.now())
            return challenges[len(attempts) - 1]
        def recognize(engine, cap, **kwargs):
            if cap["token"] == "complete":
                return [[1, 2], [3, 4], [5, 6], [7, 8]]
            return []
        solver = Mock(return_value="synthetic-verified")
        with patch.object(auto, "CaptchaPrefetch", side_effect=self.immediate_job):
            code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 21, 59, 40),
                captcha_get=get, inference=recognize, solver=solver)
        self.assertEqual(code, 0)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(moment < datetime(2026, 10, 29, 22) for moment in attempts))
        self.assertEqual(solver.call_args.kwargs["initial_cap"]["token"], "complete")
        self.assertGreaterEqual(launches[0][0], datetime(2026, 10, 29, 22))

    def test_prefetch_login_error_stops_before_launch_or_verification(self):
        solver = Mock(return_value="synthetic-verified")
        result, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
            solver=solver, captcha_get=Mock(side_effect=RequestError({"e": "10042"})))
        self.assertEqual(result, 1)
        self.assertEqual(launches, [])
        solver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
