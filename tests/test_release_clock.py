"""Release clock measurements never set OS time or create bookings."""
from datetime import datetime
from email.utils import formatdate
import importlib.util
import struct
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch


def stamp(value):
    seconds = int(value) + 2208988800
    return struct.pack("!II", seconds, int((value - int(value)) * 2**32))


class ReleaseClockTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("release_clock"), "release clock calibration required")
        import release_clock
        self.module = release_clock

    def packet(self):
        t1 = 1700000000.0
        request = bytes(40) + stamp(t1)
        response = bytearray(48)
        response[0], response[1] = 0x24, 2
        response[24:32] = request[40:48]
        response[32:40] = stamp(t1 - 1.96)
        response[40:48] = stamp(t1 - 1.959)
        return request, response, t1

    def test_ntp_offset_sign_and_round_trip_delay(self):
        request, response, t1 = self.packet()
        offset, delay = self.module._decode_ntp(response, request, t1, t1 + .08, .08)
        self.assertAlmostEqual(offset, -1.9995, places=5)
        self.assertAlmostEqual(delay, .079, places=5)

    def test_bad_ntp_reply_or_wall_clock_step_is_rejected(self):
        request, response, t1 = self.packet()
        broken = [response[:30]]
        for index, value in ((0, 0xE4), (0, 0x23), (1, 0), (1, 16), (24, 99)):
            altered = bytearray(response)
            altered[index] = value
            broken.append(altered)
        for payload in broken:
            with self.subTest(payload_length=len(payload)), self.assertRaises(ValueError):
                self.module._decode_ntp(payload, request, t1, t1 + .08, .08)
        with self.assertRaises(ValueError):
            self.module._decode_ntp(response, request, t1, t1 + 2.08, .08)

    def test_slow_or_reversed_server_times_are_rejected(self):
        request, response, t1 = self.packet()
        with self.assertRaises(ValueError):
            self.module._decode_ntp(response, request, t1, t1 + .5, .5)
        response[40:48] = stamp(t1 - 1.97)
        with self.assertRaises(ValueError):
            self.module._decode_ntp(response, request, t1, t1 + .08, .08)

    def test_guard_uses_offset_lower_bound_for_fast_and_slow_local_clocks(self):
        for offset in (-2.0, 2.0):
            with self.subTest(offset=offset):
                safe, measured, guard = self.module._choose_offset(
                    [(offset, .08), (offset + .002, .06), (offset - .001, .04)],
                    (offset - .5, offset + .5))
                self.assertAlmostEqual(measured, offset - .001)
                self.assertAlmostEqual(guard, .025)
                self.assertAlmostEqual(safe, measured - guard)

    def test_unstable_or_school_mismatched_offsets_are_rejected(self):
        for samples, bounds in [
            ([(-2, .05), (-2.2, .05), (-2, .05)], (-3, -1)),
            ([(-2, .05)] * 3, (-.5, .5)),
            ([(-2, .05)] * 2, (-3, -1)),
            ([(-2, .002), (-1.98, .002), (-2, .002)], (-3, -1)),
        ]:
            with self.subTest(samples=samples), self.assertRaises(ValueError):
                self.module._choose_offset(samples, bounds)

    def test_portal_probe_is_get_only_without_credentials_or_redirects(self):
        responses = [Mock(status=302, headers={"Date": formatdate(1700000000, usegmt=True)}) for _ in range(3)]
        transport = Mock()
        transport._pool.return_value.request.side_effect = responses
        with patch.object(self.module, "HttpTransport", return_value=transport), \
             patch.object(self.module.time, "time", side_effect=[1700000002.1, 1700000002.2] * 3), \
             patch.object(self.module.time, "monotonic", side_effect=[10, 10.1] * 3):
            lower, upper = self.module._portal_bounds()
        self.assertAlmostEqual(lower, -2.2, places=5)
        self.assertAlmostEqual(upper, -1.1, places=5)
        for call in transport._pool.return_value.request.call_args_list:
            self.assertEqual(call.args[0], "GET")
            self.assertEqual(call.args[1], "https://eportal.hnu.edu.cn/site/user/get-name")
            self.assertNotIn("Cookie", call.kwargs["headers"])
            self.assertFalse(call.kwargs["redirect"])
            self.assertFalse(call.kwargs["retries"])
        for response in responses:
            response.close.assert_called_once()
        transport.close.assert_called_once()

    def test_cached_or_missing_portal_date_is_rejected(self):
        for headers in ({}, {"Date": formatdate(1700000000, usegmt=True), "Age": "0"}):
            transport = Mock()
            response = Mock(status=302, headers=headers)
            transport._pool.return_value.request.return_value = response
            with self.subTest(headers=bool(headers)), \
                 patch.object(self.module, "HttpTransport", return_value=transport):
                with self.assertRaises((ValueError, KeyError)):
                    self.module._portal_bounds()
            response.close.assert_called_once()
            transport.close.assert_called_once()

    def test_valid_calibration_returns_monotonic_deadline(self):
        release = datetime.fromtimestamp(1700000020)
        with patch.object(self.module, "_ntp_samples", return_value=[(-2, .04)] * 3), \
             patch.object(self.module, "_portal_bounds", return_value=(-2.5, -1.5)), \
             patch.object(self.module.time, "time", return_value=1700000000), \
             patch.object(self.module.time, "monotonic", return_value=100):
            deadline, result = self.module._calibrate(release)
        self.assertAlmostEqual(deadline, 122.025)
        self.assertEqual(result["code"], "CLOCK_ALIGNED")

    def test_step_between_ntp_and_final_anchor_is_rejected(self):
        with patch.object(self.module, "_ntp_samples", return_value=[(-2, .04)] * 3), \
             patch.object(self.module, "_portal_bounds", return_value=(-2.5, -1.5)), \
             patch.object(self.module.time, "time", side_effect=[1700000000, 1700000003]), \
             patch.object(self.module.time, "monotonic", side_effect=[100, 101]):
            with self.assertRaises(ValueError):
                self.module._calibrate(datetime.fromtimestamp(1700000020))

    def test_primary_ntp_network_failure_uses_one_backup_source(self):
        with patch.object(self.module, "_ntp_samples", side_effect=[TimeoutError(), [(-2, .04)] * 3]) as ntp, \
             patch.object(self.module, "_portal_bounds", return_value=(-2.5, -1.5)), \
             patch.object(self.module.time, "time", return_value=1700000000), \
             patch.object(self.module.time, "monotonic", return_value=100):
            deadline, result = self.module._calibrate(datetime.fromtimestamp(1700000020))
        self.assertAlmostEqual(deadline, 122.025)
        self.assertEqual(result["code"], "CLOCK_ALIGNED")
        self.assertEqual([call.args for call in ntp.call_args_list],
                         [("time.windows.com",), ("time.cloudflare.com",)])

    def test_both_ntp_sources_failing_uses_narrow_portal_interval_conservatively(self):
        with patch.object(self.module, "_ntp_samples", side_effect=TimeoutError()) as ntp, \
             patch.object(self.module, "_portal_bounds", return_value=(-2.2, -1.8)) as portal, \
             patch.object(self.module.time, "time", return_value=1700000000), \
             patch.object(self.module.time, "monotonic", return_value=100):
            deadline, result = self.module._calibrate(datetime.fromtimestamp(1700000020))
        self.assertAlmostEqual(deadline, 122.22)
        self.assertEqual(result["code"], "CLOCK_ALIGNED_COARSE")
        self.assertEqual(ntp.call_count, 2)
        portal.assert_called_once_with(samples=5)

    def test_coarse_offset_guard_covers_fast_and_slow_clocks(self):
        for lower, upper in [(-2.2, -1.8), (1.8, 2.2)]:
            with patch.object(self.module, "_ntp_samples", side_effect=TimeoutError()), \
                 patch.object(self.module, "_portal_bounds", return_value=(lower, upper)), \
                 patch.object(self.module.time, "time", return_value=1700000000), \
                 patch.object(self.module.time, "monotonic", return_value=100):
                deadline, _ = self.module._calibrate(datetime.fromtimestamp(1700000020))
            for actual_offset in (lower, (lower + upper) / 2, upper):
                self.assertGreaterEqual(deadline - 100 + actual_offset, 20)

    def test_unusable_coarse_portal_bounds_do_not_change_deadline(self):
        for bounds in [(-3, -1), (-1.8, -2.2), (float('nan'), 0), (0, float('inf')), (-40, -39.8)]:
            with self.subTest(bounds=bounds), \
                 patch.object(self.module, "_ntp_samples", side_effect=TimeoutError()), \
                 patch.object(self.module, "_portal_bounds", return_value=bounds):
                deadline, result = self.module.prepare_release_clock(datetime.now())
            self.assertIsNone(deadline)
            self.assertEqual(result["code"], "CLOCK_UNAVAILABLE")

    def test_invalid_ntp_reply_does_not_search_other_sources(self):
        with patch.object(self.module, "_ntp_samples", side_effect=ValueError("invalid reply")) as ntp, \
             patch.object(self.module, "_portal_bounds") as portal:
            deadline, result = self.module.prepare_release_clock(datetime.now())
        self.assertIsNone(deadline)
        self.assertEqual(result["code"], "CLOCK_UNAVAILABLE")
        ntp.assert_called_once_with("time.windows.com")
        portal.assert_not_called()

    def test_slow_worker_cannot_delay_release_preparation(self):
        finish = Event()
        def slow(*args):
            finish.wait(1)
            return 999, {"code": "CLOCK_ALIGNED"}
        try:
            with patch.object(self.module, "_calibrate", side_effect=slow):
                started = time.monotonic()
                deadline, result = self.module.prepare_release_clock(datetime.now(), wait_limit=.02)
                self.assertLess(time.monotonic() - started, .3)
            self.assertIsNone(deadline)
            self.assertEqual(result["code"], "CLOCK_UNAVAILABLE")
        finally:
            finish.set()

    def test_optional_clock_worker_start_failure_falls_back(self):
        with patch.object(self.module, "Thread", side_effect=RuntimeError("no worker available")):
            deadline, result = self.module.prepare_release_clock(datetime.now())
        self.assertIsNone(deadline)
        self.assertEqual(result["code"], "CLOCK_UNAVAILABLE")

    def test_calibration_starts_before_caller_waits_for_result(self):
        started, finish = Event(), Event()

        def calibrate(_):
            started.set()
            finish.wait(1)
            return 999, {"code": "CLOCK_ALIGNED"}

        with patch.object(self.module, "_calibrate", side_effect=calibrate):
            receive = self.module.start_release_clock(datetime.now())
            try:
                self.assertTrue(started.wait(.5))
            finally:
                finish.set()
            self.assertEqual(receive(), (999, {"code": "CLOCK_ALIGNED"}))


if __name__ == "__main__":
    unittest.main()
