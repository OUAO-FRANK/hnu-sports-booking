"""Regression checks for pure HTTP payment; no real requests or credentials."""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pure_pay

RID = 123456
NOW = datetime(2026, 10, 29, 16)
RESERVATION = {
    "id": RID, "resource_id": 57, "status": 6, "pay_money": "20.00", "sess_id": 0,
    "create_time": "2026-10-29 16:00:00",
    "period_text": {"label": {"sub_name": "1号场", "date": "2026-10-30",
                               "text": ["19:10-20:10\r\n"]}},
}
CONFIG = {
    "key": "PaymentOrder_11", "attr": {"data": {
        "goods_id": 1, "pay_type": 2, "component_key": "Numeric_6",
        "motion_simulation": "1", "url_type": 0,
        "custom_params": [
            {"name": "inst_id", "component_key": "$EVN.url_inst id", "checked": False},
            {"name": "reserve_id", "component_key": "$EVN.url_reserve_id", "checked": False},
        ],
    }},
}
SOURCE = {"key": "DataSource_12", "attr": {"data": {
    "sourceid": 5, "type": 1, "sourceConfig": {},
    "conditions": [{"key": "id", "type": 0, "value": "$EVN.url_reserve_id"}],
    "mapConfig": {"Input_7": {"key": "time"}, "Input_14": {"key": "name"},
                  "Numeric_6": {"key": "pay_money"}},
}}}


class HttpPaymentTests(unittest.TestCase):
    def api(self, path, params=None, form=None, **kwargs):
        self.calls.append((path, params, form))
        if path == "/site/reservation/my-list":
            return {"e": 0, "d": {"data": [self.reservation]}}
        if path == "/site/process/start-info":
            return {"e": 0, "d": {"bpmn_id": 3886, "form": [{"form_id": 1645}]}}
        if path == "/site/form/get-formv":
            return {"e": 0, "d": {"id": 1645, "app_id": 616, "form_version_id": 3040,
                                    "plugins": json.dumps([CONFIG, SOURCE])}}
        if path == "/site/data-source/detail":
            return {"e": 0, "d": {"list": {"id": str(RID), "resource_id": 57,
                "pay_money": self.reservation["pay_money"], "name": "南校区羽毛球馆",
                "time": "1号场, 2026-10-30 20:10-21:10, 21:10-22:10"}}}
        if path == "/site/payment-order/my-list":
            rows = [] if not self.created else [{"id": 999999, "appointment_id": self.association,
                "pay_money": int(Decimal(self.reservation["pay_money"]) * 100), "status": "未支付"}]
            return {"e": 0, "d": {"data": rows, "total": len(rows)}}
        if path == "/site/apps/launch":
            return {"e": self.check_code, "m": "payment callback"}
        if path == "/site/payment-order/create-order":
            self.created = True
            return self.create_response
        if path == "/site/payment-order/set-form-data":
            return {"e": 0}
        if path == "/site/payment-order/get-pay-result":
            return self.pay_response
        self.fail("Unexpected endpoint: " + path)

    def setUp(self):
        self.calls = []
        self.reservation = json.loads(json.dumps(RESERVATION))
        self.created = False
        self.association = RID
        self.check_code = 99
        self.pay_response = {"e": 3, "m": "等待支付或者更新支付状态失败，请重试"}
        self.create_response = {"e": 0, "d": {"order_id": 999999, "pay_status": 0,
            "code_url": "https://eportal.hnu.edu.cn/api/attachment/get-code?content=http%3A%2F%2Fwxpay.hnu.edu.cn%2Fthird%2Fhome%3Fid%3Ddummy"}}
        self.request_patch = patch.object(pure_pay, "_req", side_effect=self.api)
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)

    def prepare(self):
        function = getattr(pure_pay, "prepare_payment", None)
        self.assertIsNotNone(function, "read-only payment preparation is required")
        return function("fake", RID, now=NOW)

    def create(self):
        function = getattr(pure_pay, "create_payment", None)
        self.assertIsNotNone(function, "validated HTTP payment flow is required")
        return function("fake", RID, now=NOW)

    def test_prepare_is_read_only_and_uses_actual_price(self):
        plan = self.prepare()
        self.assertEqual(plan["amount"], "20.00")
        self.assertFalse(any(form is not None for _, _, form in self.calls))

    def test_expired_reservation_cannot_create_an_order(self):
        self.reservation["status"] = 5
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertFalse(self.created)

    def test_started_period_cannot_create_an_order(self):
        self.reservation["period_text"]["label"]["date"] = "2026-10-28"
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertFalse(self.created)

    def test_created_order_matches_extracted_frontend_request(self):
        result = self.create()
        captured = next(form for path, _, form in self.calls if path.endswith("create-order"))
        fixture = Path(__file__).resolve().parent / "fixtures/http_browser_requests.json"
        expected = json.loads(fixture.read_text(encoding="utf-8"))["requests"][0]["data"]
        for key in expected:
            if key == "params":
                self.assertEqual(json.loads(captured[key]), json.loads(expected[key]))
            elif key == "pay_money":
                self.assertEqual(Decimal(str(captured[key])), Decimal(str(expected[key])))
            else:
                self.assertEqual(str(captured[key]), str(expected[key]))
        self.assertEqual(result["order_id"], 999999)
        self.assertEqual(result["content"], "http://wxpay.hnu.edu.cn/third/home?id=dummy")

    def test_precheck_contains_reserve_id_and_exempts_payment_component(self):
        self.create()
        form = next(form for path, _, form in self.calls if path.endswith("apps/launch"))
        self.assertEqual(str(form["reserve_id"]), str(RID))
        self.assertEqual(str(form["check_launch"]), "1")
        self.assertEqual(json.loads(form["not_require"]), {"formId": 1645, "key": "PaymentOrder_11"})

    def test_writeback_has_order_id_and_form_data(self):
        self.create()
        form = next(form for path, _, form in self.calls if path.endswith("set-form-data"))
        data = json.loads(form["data"])["form_data"]["1645"]
        self.assertEqual(form["id"], 999999)
        self.assertEqual(data["PaymentOrder_11"]["id"], 999999)
        self.assertEqual(json.loads(data["PaymentOrder_11"]["params"]), {"reserve_id": str(RID)})

    def test_wrong_order_association_never_exposes_a_qr(self):
        self.association = 42
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertFalse(any(path.endswith("set-form-data") for path, _, _ in self.calls))

    def test_signature_error_is_not_success(self):
        self.pay_response = {"e": 1, "m": "签名错误"}
        with self.assertRaises(RuntimeError):
            self.create()

    def test_uncertain_creation_is_not_retried(self):
        self.create_response = {"e": "EXC", "m": "timeout"}
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertEqual(sum(path.endswith("create-order") for path, _, _ in self.calls), 1)

    def test_ordinary_application_success_is_not_the_payment_callback(self):
        self.check_code = 0
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertFalse(self.created)

    def test_creation_time_is_deducted_from_remaining_payment_window(self):
        self.reservation["wait_time"] = 20
        clock = [0.0]
        original_api = self.api

        def slow_create(path, **kwargs):
            if path.endswith("create-order"):
                clock[0] += 10
            return original_api(path, **kwargs)

        with patch.object(pure_pay, "_req", side_effect=slow_create), \
             patch.object(pure_pay.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.create()
        self.assertIn("deadline", result)
        self.assertEqual(result["deadline"] - clock[0], 10)

    def test_two_period_payment_uses_server_total_and_canonical_form_labels(self):
        self.reservation["period_text"]["label"]["text"] = ["21:10-22:10\r\n", "20:10-21:10"]
        self.reservation["pay_money"] = "40.00"
        result = self.create()
        self.assertEqual(result["amount"], "40.00")
        self.assertEqual(result["slot"], "20:10-21:10; 21:10-22:10")
        form = next(form for path, _, form in self.calls if path.endswith("apps/launch"))
        fields = json.loads(form["data"])["form_data"]["1645"]
        self.assertEqual(fields["Input_7"], "1号场, 2026-10-30 20:10-21:10, 21:10-22:10")
        self.assertEqual(fields["Input_14"], "南校区羽毛球馆")
        self.assertEqual(sum(path.endswith("create-order") for path, _, _ in self.calls), 1)

    def test_nonconsecutive_periods_are_not_paid(self):
        self.reservation["period_text"]["label"]["text"] = ["19:10-20:10", "21:10-22:10"]
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertFalse(self.created)

    def test_paid_single_period_can_be_longer_than_one_hour(self):
        self.reservation["period_text"]["label"]["text"] = ["18:20-19:50"]
        result = self.create()
        self.assertEqual(result["slot"], "18:20-19:50")
        self.assertEqual(sum(path.endswith("create-order") for path, _, _ in self.calls), 1)


if __name__ == "__main__":
    unittest.main()
