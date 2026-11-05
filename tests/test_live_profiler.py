"""Profiler must never create bookings, payment orders or raw credential reports."""
import base64
import importlib.util
import io
import json
import unittest
from unittest.mock import Mock, patch

from PIL import Image


class LiveProfilerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("tools.profile_live"), "safe real-service profiling is required")
        from tools import profile_live
        self.module = profile_live
        image = io.BytesIO()
        Image.new("RGB", (80, 60), "white").save(image, format="PNG")
        self.cap = {"wordList": ["甲", "乙", "丙", "丁"], "token": "PRIVATE_TOKEN",
                    "secretKey": "0123456789abcdef", "originalImageBase64": base64.b64encode(image.getvalue()).decode()}
        self.paths = []
        self.transport = Mock()
        self.transport.request_json.side_effect = self.api
        self.probe = profile_live.LiveProbe("PRIVATE_COOKIE", self.transport)

    def api(self, url, **kwargs):
        path = url.split("eportal.hnu.edu.cn", 1)[1].split("?", 1)[0]
        self.paths.append(path)
        if path == "/site/user/get-name":
            return {"e": 0, "d": {"name": "PRIVATE_NAME"}}
        if path == "/site/reservation/resource-info":
            return {"e": 0, "d": {"is_verify": 1}}
        if path == "/site/reservation/resource-info-margin":
            return {"e": 0, "d": {}}
        if path == "/site/captcha/click-get":
            return {"e": 0, "d": self.cap}
        if path == "/site/captcha/click-check":
            return {"e": 0}
        self.fail("Unexpected profiler endpoint")

    def run_profile(self, *, captcha=False):
        engine = Mock()
        engine.solve.return_value = ([[1, 2], [3, 4], [5, 6], [7, 8]], [])
        with patch.object(self.module, "CaptchaEngine", return_value=engine), \
             patch.object(self.module.time, "sleep"), \
             patch.object(self.module, "aes_ecb_encrypt_b64", return_value="PRIVATE_CIPHERTEXT"):
            return self.module.profile(self.probe, venue=57, day="2026-11-03", samples=1, captcha=captcha)

    def test_reservation_payment_and_external_urls_are_rejected(self):
        for path in ["/site/reservation/launch", "/site/reservation/cancel",
                     "/site/payment-order/create-order", "https://example.invalid/"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.probe.call(path, form={})
        self.transport.request_json.assert_not_called()

    def test_post_is_only_permitted_for_captcha_check(self):
        with self.assertRaises(ValueError):
            self.probe.call("/site/reservation/resource-info", form={"id": "57"})
        self.transport.request_json.assert_not_called()

    def test_query_profile_has_no_model_or_captcha_calls(self):
        result = self.run_profile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["rounds"]), 1)
        self.assertFalse(any("captcha" in path for path in self.paths))

    def test_serial_parallel_profile_does_normal_verification_and_hides_raw_data(self):
        result = self.run_profile(captcha=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([row["mode"] for row in result["rounds"]], ["serial", "parallel"])
        self.assertTrue(all(row["verified"] for row in result["rounds"]))
        self.assertEqual(self.paths.count("/site/captcha/click-check"), 2)
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_expired_login_stops_before_query_or_model(self):
        self.transport.request_json.return_value = {"e": "10042", "m": "PRIVATE_MESSAGE"}
        self.transport.request_json.side_effect = None
        result = self.run_profile(captcha=True)
        self.assertEqual(result["status"], "login_required")
        self.assertEqual(self.transport.request_json.call_count, 1)
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_rate_limit_stops_the_remaining_samples(self):
        original = self.api
        def limited(url, **kwargs):
            if "resource-info-margin" in url:
                return {"e": 429, "m": "PRIVATE_MESSAGE"}
            return original(url, **kwargs)
        self.transport.request_json.side_effect = limited
        result = self.run_profile(captcha=True)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["code"], "429")
        self.assertEqual(result["rounds"], [])

    def test_failed_verification_is_not_reported_as_a_successful_fast_round(self):
        original = self.api
        def reject(url, **kwargs):
            if "click-check" in url:
                return {"e": 1}
            return original(url, **kwargs)
        self.transport.request_json.side_effect = reject
        result = self.run_profile(captcha=True)
        self.assertEqual(result.get("verified_rounds"), {"serial": 0, "parallel": 0})
        self.assertEqual(result.get("median_verified_total_ms"), {"serial": None, "parallel": None})


if __name__ == "__main__":
    unittest.main()
