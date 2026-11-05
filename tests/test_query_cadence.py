"""Polling rate is capped start-to-start without extra waits after a slow round."""
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import auto
from auto_grab import RequestError
import test_booking_flow as flow


class QueryCadenceTests(unittest.TestCase):
    def test_fast_round_waits_only_the_remaining_minimum_interval(self):
        pause = getattr(auto, "wait_query_interval", None)
        self.assertIsNotNone(pause, "round duration should count toward the query interval")
        sleep = Mock()
        with patch.object(auto, "time", SimpleNamespace(perf_counter=lambda: .1, sleep=sleep)):
            pause(0, .3)
        self.assertAlmostEqual(sleep.call_args.args[0], .2)

    def test_slow_round_does_not_add_another_sleep(self):
        pause = getattr(auto, "wait_query_interval", None)
        self.assertIsNotNone(pause, "slow responses must not add another fixed polling pause")
        sleep = Mock()
        with patch.object(auto, "time", SimpleNamespace(perf_counter=lambda: 7, sleep=sleep)):
            pause(0, .3)
        sleep.assert_not_called()

    def test_empty_slow_queries_run_without_extra_point_three_second_gaps(self):
        starts = []
        def slow_empty(*args):
            starts.append(auto.time.monotonic())
            auto.time.sleep(.7)
            return []
        code, launches, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
            fetch=slow_empty, max_wait=1.4)
        self.assertEqual(code, 1)
        self.assertEqual(launches, [])
        self.assertAlmostEqual(starts[1] - starts[0], .7)

    def test_rate_limit_keeps_existing_one_second_backoff(self):
        starts = []
        def rate_limited_once(*args):
            starts.append(auto.time.monotonic())
            if len(starts) == 1:
                raise RequestError({"e": 429, "m": "synthetic rate limit"})
            return [dict(flow.SLOT)]
        code, _, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22), fetch=rate_limited_once)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(starts[1] - starts[0], 1)


if __name__ == "__main__":
    unittest.main()
