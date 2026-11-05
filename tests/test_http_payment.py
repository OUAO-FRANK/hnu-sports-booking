"""Regression checks for pure HTTP payment; no real requests or credentials."""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch, Mock

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

    def cross_court_receipt(self):
        row = json.loads(json.dumps(RESERVATION))
        row["pay_money"] = "40.00"
        row["period_text"] = {
            "second": {"sub_name": "2号场", "date": "2026-10-30", "text": ["21:10-22:10\r\n"]},
            "first": {"sub_name": "1号场", "date": "2026-10-30", "text": ["20:10-21:10\r\n"]},
        }
        return row

    def test_contiguous_cross_court_receipt_creates_one_verified_total_order(self):
        self.reservation = self.cross_court_receipt()
        canonical = "1号场 20:10-21:10；2号场 21:10-22:10"
        original_api = self.api
        def request(path, **kwargs):
            response = original_api(path, **kwargs)
            if path == "/site/data-source/detail":
                response["d"]["list"]["time"] = canonical
            return response
        with patch.object(pure_pay, "_req", side_effect=request):
            try:
                result = self.create()
            except RuntimeError as exc:
                self.fail(f"valid complete cross-court receipt was rejected: {exc}")
        self.assertEqual(result["court"], "1号场 → 2号场")
        self.assertEqual(result["slot"], "20:10-21:10; 21:10-22:10")
        self.assertEqual(result["amount"], "40.00")
        precheck = next(form for path, _, form in self.calls if path == "/site/apps/launch")
        self.assertEqual(json.loads(precheck["data"])["form_data"]["1645"]["Input_7"], canonical)
        self.assertEqual(sum(path.endswith("create-order") for path, _, _ in self.calls), 1)

    def test_changed_cross_court_period_mapping_stops_before_order_creation(self):
        initial = self.cross_court_receipt()
        for change in ("swapped", "partial", "extra"):
            with self.subTest(change=change):
                self.reservation = json.loads(json.dumps(initial))
                self.calls = []
                self.created = False
                original_api = self.api
                detail_count = 0
                def request(path, **kwargs):
                    nonlocal detail_count
                    response = original_api(path, **kwargs)
                    if path == "/site/reservation/my-list":
                        detail_count += 1
                        if detail_count == 2:
                            latest = json.loads(json.dumps(initial))
                            periods = latest["period_text"]
                            if change == "swapped":
                                periods["first"]["sub_name"], periods["second"]["sub_name"] = "2号场", "1号场"
                            elif change == "partial":
                                periods.pop("second")
                            else:
                                periods["second"]["text"].append("22:10-23:10")
                            response = {"e": 0, "d": {"data": [latest]}}
                    return response
                with patch.object(pure_pay, "_req", side_effect=request), self.assertRaises(RuntimeError):
                    self.create()
                self.assertEqual(detail_count, 2, "valid initial cross-court receipt must reach fresh receipt validation")
                self.assertFalse(self.created)

    def test_two_period_data_source_timeout_retries_read_before_one_order(self):
        self.reservation["period_text"]["label"]["text"] = ["20:10-21:10", "21:10-22:10"]
        self.reservation["pay_money"] = "40.00"
        reads = []
        original_api = self.api
        def flaky(path, **kwargs):
            if path == "/site/data-source/detail":
                reads.append(kwargs)
                if len(reads) == 1:
                    return {"e": "EXC", "m": "ReadTimeoutError"}
            return original_api(path, **kwargs)
        with patch.object(pure_pay, "_req", side_effect=flaky), patch.object(pure_pay.time, "sleep"):
            result = self.create()
        self.assertEqual(result["amount"], "40.00")
        self.assertEqual(len(reads), 2)
        self.assertTrue(all(0 < call.get("timeout", 12) <= 5 for call in reads))
        self.assertEqual(sum(path.endswith("create-order") for path, _, _ in self.calls), 1)

    def test_two_period_data_source_repeated_timeout_never_creates_an_order(self):
        self.reservation["period_text"]["label"]["text"] = ["20:10-21:10", "21:10-22:10"]
        reads = []
        original_api = self.api
        def failed(path, **kwargs):
            if path == "/site/data-source/detail":
                reads.append(True)
                return {"e": "EXC", "m": "ReadTimeoutError"}
            return original_api(path, **kwargs)
        with patch.object(pure_pay, "_req", side_effect=failed), patch.object(pure_pay.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "本次尚未发送创建订单请求"):
                self.create()
        self.assertEqual(len(reads), 2)
        self.assertFalse(self.created)

    def test_data_source_retry_does_not_extend_original_payment_deadline(self):
        self.reservation["period_text"]["label"]["text"] = ["20:10-21:10", "21:10-22:10"]
        elapsed = [0.0]
        reads = []
        original_api = self.api
        def expired(path, **kwargs):
            if path == "/site/data-source/detail":
                reads.append(kwargs)
                elapsed[0] += 1
                return {"e": "EXC", "m": "ReadTimeoutError"}
            return original_api(path, **kwargs)
        with patch.object(pure_pay, "_req", side_effect=expired), \
             patch.object(pure_pay.time, "monotonic", side_effect=lambda: elapsed[0]), \
             patch.object(pure_pay.time, "sleep"):
            with self.assertRaises(RuntimeError):
                pure_pay.create_payment("fake", RID, now=NOW, preparation_deadline=1)
        self.assertEqual(len(reads), 1)
        self.assertLessEqual(reads[0]["timeout"], 1)
        self.assertFalse(self.created)

    def test_data_source_auth_rate_limit_and_structure_errors_do_not_retry(self):
        self.reservation["period_text"]["label"]["text"] = ["20:10-21:10", "21:10-22:10"]
        for response in ({"e": 10042}, {"e": 10043}, {"e": 429},
                         {"e": "EXC", "m": "JSONDecodeError"}, {"e": "SHAPE"}):
            with self.subTest(response=response):
                reads = []
                original_api = self.api
                def failed(path, **kwargs):
                    if path == "/site/data-source/detail":
                        reads.append(True)
                        return response
                    return original_api(path, **kwargs)
                with patch.object(pure_pay, "_req", side_effect=failed), \
                     patch.object(pure_pay.time, "sleep") as sleep, self.assertRaises(RuntimeError):
                    self.create()
                self.assertEqual(len(reads), 1)
                sleep.assert_not_called()
                self.assertFalse(self.created)

    def test_recovered_data_source_still_requires_matching_identity_and_amount(self):
        self.reservation["period_text"]["label"]["text"] = ["20:10-21:10", "21:10-22:10"]
        for changes in ({"id": 42}, {"resource_id": 84}, {"pay_money": "99.00"}):
            with self.subTest(changes=changes):
                reads = []
                original_api = self.api
                def mismatch(path, **kwargs):
                    if path == "/site/data-source/detail":
                        reads.append(True)
                        if len(reads) == 1:
                            return {"e": "EXC", "m": "ReadTimeoutError"}
                        response = original_api(path, **kwargs)
                        response["d"]["list"].update(changes)
                        return response
                    return original_api(path, **kwargs)
                with patch.object(pure_pay, "_req", side_effect=mismatch), \
                     patch.object(pure_pay.time, "sleep"), self.assertRaises(RuntimeError):
                    self.create()
                self.assertEqual(len(reads), 2)
                self.assertFalse(self.created)

    def test_deadline_expiring_after_payment_precheck_stops_order_creation(self):
        elapsed = [0.0]
        original_api = self.api
        def slow_check(path, **kwargs):
            response = original_api(path, **kwargs)
            if path == "/site/apps/launch":
                elapsed[0] = 2
            return response
        with patch.object(pure_pay, "_req", side_effect=slow_check), \
             patch.object(pure_pay.time, "monotonic", side_effect=lambda: elapsed[0]):
            with self.assertRaises(RuntimeError):
                pure_pay.create_payment("fake", RID, now=NOW, preparation_deadline=1)
        self.assertFalse(self.created)

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

    def test_reservation_and_process_reads_overlap(self):
        detail_started, process_started = threading.Event(), threading.Event()
        original_api = self.api

        def synchronized(path, **kwargs):
            if path == "/site/reservation/my-list":
                detail_started.set()
                self.assertTrue(process_started.wait(1), "process read must overlap detail")
            elif path == "/site/process/start-info":
                process_started.set()
                self.assertTrue(detail_started.wait(1))
            return original_api(path, **kwargs)

        with patch.object(pure_pay, "_req", side_effect=synchronized):
            self.prepare()
        self.assertFalse(self.created)

    def prepared_config(self, cookie="fake", clock=None):
        cls = getattr(pure_pay, "PaymentConfigPrefetch", None)
        self.assertIsNotNone(cls, "bounded in-memory configuration preload is required")
        job = cls(cookie, clock=clock)
        job._completed.result(timeout=1)
        return job

    def test_prepared_config_and_receipt_remove_redundant_reads(self):
        job = self.prepared_config()
        self.calls = []
        result = pure_pay.create_payment("fake", RID, now=NOW,
            reservation=self.reservation, preparation=job)
        self.assertEqual(result["order_id"], 999999)
        self.assertFalse(any(path in ("/site/process/start-info", "/site/form/get-formv") for path, _, _ in self.calls))
        self.assertEqual(sum(path == "/site/reservation/my-list" for path, _, _ in self.calls), 1)

    def test_prepared_config_is_cookie_session_age_bound_and_single_use(self):
        for mismatch in ("cookie", "session", "age", "clock_reversed"):
            with self.subTest(mismatch=mismatch):
                clock = Mock(return_value=10)
                job = self.prepared_config(clock=clock)
                cookie, sess = "fake", 0
                if mismatch == "cookie":
                    cookie = "another-login"
                if mismatch == "session":
                    sess = 7
                if mismatch == "age":
                    clock.return_value = 70.1
                if mismatch == "clock_reversed":
                    clock.return_value = 9
                self.assertIsNone(job.take(cookie, sess))
                self.assertIsNone(job.take("fake", 0))
        job = self.prepared_config()
        self.assertIsNotNone(job.take("fake", 0))
        self.assertIsNone(job.take("fake", 0))

    def test_unfinished_config_does_not_wait_or_get_reused(self):
        entered, release = threading.Event(), threading.Event()
        original_api = self.api
        cls = getattr(pure_pay, "PaymentConfigPrefetch", None)
        self.assertIsNotNone(cls)
        def pending(path, **kwargs):
            if path == "/site/process/start-info":
                entered.set()
                release.wait(1)
            return original_api(path, **kwargs)
        with patch.object(pure_pay, "_req", side_effect=pending):
            job = cls("fake")
            self.assertTrue(entered.wait(1))
            try:
                self.assertIsNone(job.take("fake", 0))
            finally:
                release.set()
                job._completed.result(timeout=1)
        self.assertIsNone(job.take("fake", 0))

    def test_failed_config_preparation_falls_back(self):
        cls = getattr(pure_pay, "PaymentConfigPrefetch", None)
        self.assertIsNotNone(cls)
        with patch.object(pure_pay, "_req", return_value={"e": "EXC"}):
            job = cls("fake")
            try:
                job._completed.result(timeout=1)
            except RuntimeError:
                pass
        plan = pure_pay.prepare_payment("fake", RID, now=NOW,
            reservation=self.reservation, preparation=job)
        self.assertEqual(plan["amount"], "20.00")
        self.assertTrue(any(path == "/site/form/get-formv" for path, _, _ in self.calls))

    def test_session_mismatch_reads_the_matching_form_instead(self):
        job = self.prepared_config()
        self.calls = []
        pure_pay.prepare_payment("fake", RID, now=NOW,
            reservation=dict(self.reservation, sess_id=7), preparation=job)
        form_reads = [params for path, params, _ in self.calls if path == "/site/form/get-formv"]
        self.assertEqual(form_reads[0]["sess_id"], 7)

    def test_preload_that_exceeds_budget_does_not_start_another_read(self):
        cls = getattr(pure_pay, "PaymentConfigPrefetch", None)
        self.assertIsNotNone(cls)
        original_api = self.api
        clock = [0]
        def delayed(path, **kwargs):
            response = original_api(path, **kwargs)
            if path == "/site/process/start-info":
                clock[0] = 9
            return response
        with patch.object(pure_pay.time, "perf_counter", side_effect=lambda: clock[0]), \
             patch.object(pure_pay, "_req", side_effect=delayed):
            job = cls("fake")
            with self.assertRaises(RuntimeError):
                job._completed.result(timeout=1)
        self.assertFalse(any(path == "/site/form/get-formv" for path, _, _ in self.calls))

    def test_wrong_receipt_id_stops_before_any_request(self):
        self.calls = []
        with self.assertRaises(RuntimeError):
            pure_pay.create_payment("fake", RID, now=NOW, reservation=dict(self.reservation, id=42))
        self.assertEqual(self.calls, [])

    def test_reused_receipt_still_checks_fresh_status_and_amount(self):
        original = json.loads(json.dumps(self.reservation))
        for changes in ({"status": 5}, {"pay_money": "40.00"}, {"resource_id": 84}):
            with self.subTest(changes=changes):
                self.reservation = {**original, **changes}
                self.calls = []
                with self.assertRaises(RuntimeError):
                    pure_pay.create_payment("fake", RID, now=NOW, reservation=original)
                self.assertFalse(self.created)
                self.assertEqual(sum(path == "/site/reservation/my-list" for path, _, _ in self.calls), 1)

    def test_existing_order_read_keeps_its_freshness_after_form_loading(self):
        orders_started = threading.Event()
        form_loaded = False
        lock = threading.Lock()
        active, peak = 0, 0
        original_api = self.api

        def synchronized(path, **kwargs):
            nonlocal active, peak, form_loaded
            with lock:
                active += 1
                peak = max(peak, active)
                if kwargs.get("form") is not None:
                    self.assertEqual(active, 1, "payment writes must stay serial")
            try:
                if path == "/site/form/get-formv":
                    self.assertFalse(orders_started.wait(.05), "do not pre-read a mutable order list")
                    form_loaded = True
                elif path == "/site/payment-order/my-list" and not self.created:
                    orders_started.set()
                    self.assertTrue(form_loaded)
                    return {"e": 0, "d": {"data": [{"id": 42, "appointment_id": RID}], "total": 1}}
                return original_api(path, **kwargs)
            finally:
                with lock:
                    active -= 1

        with patch.object(pure_pay, "_req", side_effect=synchronized), self.assertRaises(RuntimeError):
            self.create()
        self.assertLessEqual(peak, 2)
        self.assertFalse(self.created)
        self.assertFalse(any(form is not None for _, _, form in self.calls))

    def test_failed_order_read_stops_before_any_payment_write(self):
        original_api = self.api

        def failed_orders(path, **kwargs):
            if path == "/site/payment-order/my-list":
                return {"e": "EXC", "m": "timeout"}
            return original_api(path, **kwargs)

        with patch.object(pure_pay, "_req", side_effect=failed_orders), self.assertRaises(RuntimeError):
            self.create()
        self.assertFalse(self.created)
        self.assertFalse(any(form is not None for _, _, form in self.calls))

    def test_either_parallel_read_failure_stops_before_any_payment_write(self):
        original_api = self.api
        for failed_path in ("/site/reservation/my-list", "/site/process/start-info"):
            with self.subTest(path=failed_path):
                self.calls = []

                def failed_read(path, **kwargs):
                    if path == failed_path:
                        return {"e": 10042, "m": "expired"}
                    return original_api(path, **kwargs)

                with patch.object(pure_pay, "_req", side_effect=failed_read), self.assertRaises(RuntimeError):
                    self.create()
                self.assertFalse(self.created)
                self.assertFalse(any(form is not None for _, _, form in self.calls))

    def test_existing_order_stops_before_payment_precheck(self):
        original_api = self.api

        def existing_order(path, **kwargs):
            if path == "/site/payment-order/my-list":
                return {"e": 0, "d": {"data": [{"id": 42, "appointment_id": RID}], "total": 1}}
            return original_api(path, **kwargs)

        with patch.object(pure_pay, "_req", side_effect=existing_order), self.assertRaises(RuntimeError):
            self.create()
        self.assertFalse(any(path.endswith("apps/launch") for path, _, _ in self.calls))
        self.assertFalse(self.created)

    def test_fresh_reservation_changes_stop_before_order_creation(self):
        original_api = self.api
        for changes in ({"status": 5}, {"resource_id": 84}, {"pay_money": "40.00"},
                        {"period_text": {"label": {"sub_name": "2号场", "date": "2026-10-30",
                                                   "text": ["19:10-20:10"]}}}):
            with self.subTest(changes=changes):
                self.calls = []
                details = 0

                def changed_detail(path, **kwargs):
                    nonlocal details
                    response = original_api(path, **kwargs)
                    if path == "/site/reservation/my-list":
                        details += 1
                        if details == 2:
                            response = {"e": 0, "d": {"data": [{**self.reservation, **changes}]}}
                    return response

                with patch.object(pure_pay, "_req", side_effect=changed_detail), self.assertRaises(RuntimeError):
                    self.create()
                self.assertFalse(self.created)
                self.assertEqual(details, 2)


if __name__ == "__main__":
    unittest.main()
