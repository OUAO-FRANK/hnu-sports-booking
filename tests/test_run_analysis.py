"""Replay diagnostics without exposing arbitrary log content or guessing evidence."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import analyze_run


def lines(*rows):
    return io.StringIO("\n".join(json.dumps(row) for row in rows))


class RunAnalysisTests(unittest.TestCase):
    def test_payment_window_exit_reason_is_in_summary(self):
        for reason, outcome, label in (
                ("USER_CLOSED", "UNCONFIRMED", "用户关闭"),
                ("DEADLINE", "UNCONFIRMED", "倒计时结束"),
                ("RESERVATION_ENDED", "UNCONFIRMED", "服务端确认预约结束"),
                ("UI_ERROR", "UNCONFIRMED", "窗口异常"),
                ("INTERRUPTED", "UNCONFIRMED", "用户中断"),
                ("UNEXPECTED_EXIT", "UNCONFIRMED", "窗口意外结束"),
                ("CONFIRMED", "CONFIRMED", "服务端确认生效")):
            with self.subTest(reason=reason):
                report = analyze_run.analyze_lines(lines(
                    {"event": "payment_ui", "reason": reason, "outcome": outcome,
                     "duration_ms": 10, "url": "PRIVATE_PAYMENT_URL"}))
                self.assertEqual(report.get("payment_ui"), {"reason": reason, "outcome": outcome})
                self.assertIn(label, analyze_run.format_report(report))
                self.assertNotIn("PRIVATE_", json.dumps(report))

    def test_unknown_or_inconsistent_payment_ui_events_do_not_claim_confirmation(self):
        for row in ({}, {"reason": "PRIVATE_REASON", "outcome": "CONFIRMED"},
                    {"reason": "USER_CLOSED", "outcome": "CONFIRMED"},
                    {"reason": "CONFIRMED", "outcome": "UNCONFIRMED"}):
            with self.subTest(row=row):
                report = analyze_run.analyze_lines(lines({"event": "payment_ui", **row}))
                self.assertEqual(report.get("payment_ui"), {"reason": "UNKNOWN", "outcome": "UNKNOWN"})
                self.assertNotIn("PRIVATE_", analyze_run.format_report(report))

    def test_measured_dispatch_acceptance_phases_connections_and_clock(self):
        report = analyze_run.analyze_lines(lines(
            {"event": "plan", "run_mode": "scheduled"},
            {"event": "timing", "stage": "release_clock_ready", "code": "CLOCK_ALIGNED",
             "clock_offset_ms": -1992.8, "clock_guard_ms": 50},
            {"event": "launch_start", "release_elapsed_ms": .2},
            {"event": "timing", "stage": "query", "duration_ms": 30},
            {"event": "timing", "stage": "captcha", "duration_ms": 40},
            {"event": "timing", "stage": "launch", "code": 1, "reason": "NOT_RELEASED",
             "duration_ms": 100, "release_elapsed_ms": 100.2},
            {"event": "timing", "stage": "launch", "code": 0, "reason": "ACCEPTED",
             "duration_ms": 220, "release_elapsed_ms": 450},
            {"event": "http", "connection_reused": False, "connect_ms": 50, "send_ms": .2,
             "response_wait_ms": 100},
            {"event": "http", "connection_reused": True, "connect_ms": 0, "send_ms": .4,
             "response_wait_ms": 200},
            {"event": "receipt", "outcome": "MATCHED", "status": 6, "id": 123456},
        ))
        self.assertEqual(report["first_dispatch_ms"], .2)
        self.assertEqual(report["first_accepted_ms"], 450)
        self.assertEqual(report["first_accepted_request_ms"], 220)
        self.assertEqual(report["phases"]["launch"], {"count": 2, "median_ms": 160, "max_ms": 220})
        self.assertEqual(report["launch_results"], {"NOT_RELEASED": 1, "ACCEPTED": 1})
        self.assertEqual(report["connections"]["reuse_percent"], 50)
        self.assertEqual(report["connections"]["connect_ms"]["median_ms"], 25)
        self.assertEqual(report["clock"]["offset_ms"], -1992.8)
        self.assertEqual(report["run_mode"], "scheduled")
        self.assertEqual(report["receipt"], {"outcome": "MATCHED", "status": 6})

    def test_legacy_timestamps_and_later_events_do_not_fill_missing_first_timings(self):
        report = analyze_run.analyze_lines(lines(
            {"event": "launch_start", "time": "2026-11-04T22:00:00"},
            {"event": "launch_start", "release_elapsed_ms": 1000},
            {"event": "timing", "stage": "launch", "code": 1},
            {"event": "timing", "stage": "launch", "code": 0, "duration_ms": 8348.8,
             "time": "2026-11-04T22:00:20"},
            {"event": "timing", "stage": "occupied", "release_elapsed_ms": 20462},
            {"event": "timing", "stage": "launch", "code": 0, "release_elapsed_ms": 22000},
        ))
        self.assertIsNone(report["first_dispatch_ms"])
        self.assertIsNone(report["first_accepted_ms"])
        self.assertEqual(report["first_accepted_request_ms"], 8348.8)
        self.assertIn("首次成功提交请求往返：8348.800 ms", analyze_run.format_report(report))
        self.assertEqual(report["launch_results"]["UNKNOWN"], 1)
        self.assertEqual(report["evidence"], {"kind": "UNVERIFIED", "real_booking": "UNKNOWN", "peak_load": "UNKNOWN"})
        self.assertIn("未知", analyze_run.format_report(report))

    def test_missing_or_invalid_first_accepted_request_duration_is_not_backfilled(self):
        for duration in (None, True, "PRIVATE_DURATION", float("nan"), -1):
            with self.subTest(duration_type=type(duration).__name__):
                report = analyze_run.analyze_lines(lines(
                    {"event": "timing", "stage": "launch", "code": 0, "duration_ms": duration},
                    {"event": "timing", "stage": "launch", "code": 0, "duration_ms": 20},
                ))
                self.assertIsNone(report["first_accepted_request_ms"])

    def test_receipt_summary_uses_the_last_observed_terminal_state(self):
        for status, label in ((5, "已结束"), (7, "已生效")):
            with self.subTest(status=status):
                report = analyze_run.analyze_lines(lines(
                    {"event": "receipt", "outcome": "MATCHED", "status": 6},
                    {"event": "receipt", "outcome": "MATCHED", "status": status},
                ))
                self.assertEqual(report["receipt"], {"outcome": "MATCHED", "status": status})
                self.assertIn(f"回执已匹配：{label}", analyze_run.format_report(report))

    def test_report_heading_separates_provenance_from_observed_receipt(self):
        report = analyze_run.analyze_lines(lines(
            {"event": "receipt", "outcome": "MATCHED", "status": 6},
        ))
        text = analyze_run.format_report(report)
        self.assertTrue(text.startswith("运行日志摘要（是否属于高峰实测，需结合运行现场确认）"))
        self.assertIn("回执已匹配：待付款", text)
        self.assertEqual(report["evidence"]["real_booking"], "UNKNOWN")
        unknown = analyze_run.analyze_lines(lines())
        self.assertIn("回执未知：未知", analyze_run.format_report(unknown))

    def test_first_submission_connection_uses_only_the_first_exact_http_path(self):
        report = analyze_run.analyze_lines(lines(
            {"event": "http", "path": "/site/user/get-name", "method": "GET", "connect_ms": 999},
            {"event": "http", "path": "/site/reservation/launch?token=PRIVATE_TOKEN", "connect_ms": 999},
            {"event": "http", "path": "https://example.invalid/site/reservation/launch", "connect_ms": 999},
            {"event": "http", "path": "/site/reservation/launch", "method": "POST",
             "connection_reused": True, "connect_ms": 0, "send_ms": .2, "response_wait_ms": 1000},
            {"event": "http", "path": "/site/reservation/launch", "connect_ms": 999},
        ))
        self.assertEqual(report["first_submission_connection"], {
            "connection_reused": True, "connect_ms": 0, "send_ms": .2, "response_wait_ms": 1000})
        text = analyze_run.format_report(report)
        self.assertIn("首次提交连接：", text)
        self.assertIn("响应等待 1000.000 ms", text)
        self.assertNotIn("PRIVATE_", text + json.dumps(report))

    def test_missing_first_submission_metrics_stay_unknown_in_legacy_logs(self):
        for rows in ([], [
            {"event": "http", "path": "/site/reservation/launch"},
            {"event": "http", "path": "/site/reservation/launch", "connection_reused": True, "send_ms": 1},
        ]):
            with self.subTest(rows=len(rows)):
                report = analyze_run.analyze_lines(lines(*rows))
                self.assertEqual(report["first_submission_connection"], {
                    "connection_reused": None, "connect_ms": None, "send_ms": None, "response_wait_ms": None})
                text = analyze_run.format_report(report)
                self.assertIn("首次提交连接：复用 未知", text)

    def test_privacy_unknown_strings_ids_and_invalid_numbers_never_escape(self):
        report = analyze_run.analyze_lines(lines(
            {"event": "PRIVATE_EVENT", "stage": "PRIVATE_STAGE", "Cookie": "PRIVATE_COOKIE"},
            {"event": "plan", "run_mode": "PRIVATE_MODE", "venue": 123456},
            {"event": "timing", "stage": "launch", "code": 1, "reason": "PRIVATE_REASON",
             "m": "PRIVATE_NAME", "payment_url": "https://example.invalid/PRIVATE_TOKEN"},
            {"event": "timing", "stage": "query", "duration_ms": "PRIVATE_DURATION"},
            {"event": "timing", "stage": "query", "duration_ms": True},
            {"event": "timing", "stage": "query", "duration_ms": float("nan")},
            {"event": "http", "connect_ms": float("inf"), "send_ms": -1, "connection_reused": 1},
            {"event": "receipt", "outcome": "PRIVATE_OUTCOME", "status": "PRIVATE_STATUS", "id": 123456},
        ))
        output = json.dumps(report) + analyze_run.format_report(report)
        for secret in ("PRIVATE_", "example.invalid", "123456", "NaN", "Infinity"):
            self.assertNotIn(secret, output)
        self.assertNotIn("query", report["phases"])
        self.assertIsNone(report["connections"]["reuse_percent"])

    def test_malformed_trailing_line_is_skipped_without_losing_valid_events(self):
        stream = lines({"event": "timing", "stage": "query", "duration_ms": 12})
        stream = io.StringIO(stream.read() + '\n[]\n{"PRIVATE_TRAILING":')
        report = analyze_run.analyze_lines(stream)
        self.assertEqual(report["malformed_lines"], 2)
        self.assertEqual(report["phases"]["query"]["count"], 1)
        self.assertIn("2", analyze_run.format_report(report))

    def test_finite_numeric_inputs_do_not_overflow_the_median(self):
        report = analyze_run.analyze_lines(lines(
            {"event": "timing", "stage": "query", "duration_ms": 1e308},
            {"event": "timing", "stage": "query", "duration_ms": 1e308},
        ))
        self.assertEqual(report["phases"]["query"]["median_ms"], 1e308)
        json.dumps(report, allow_nan=False)

    def test_explicit_file_and_json_cli_are_read_only_and_do_not_print_path(self):
        log = '{"event":"timing","stage":"launch","code":0,"release_elapsed_ms":1146.6}\n'
        with patch.object(Path, "open", return_value=io.StringIO(log)) as opened:
            report = analyze_run.analyze_file(Path("PRIVATE_PATH.jsonl"))
        opened.assert_called_once_with("r", encoding="utf-8", errors="replace")
        self.assertEqual(report["first_accepted_ms"], 1146.6)
        output = io.StringIO()
        with patch.object(analyze_run, "analyze_file", return_value=report), patch("sys.stdout", output):
            self.assertEqual(analyze_run.main(["PRIVATE_PATH.jsonl", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue()), report)
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_file_error_uses_fixed_message_without_exception_details(self):
        output = io.StringIO()
        with patch.object(analyze_run, "analyze_file", side_effect=OSError("PRIVATE_PATH")), patch("sys.stderr", output):
            self.assertEqual(analyze_run.main(["PRIVATE_PATH.jsonl"]), 2)
        self.assertNotIn("PRIVATE_", output.getvalue())


if __name__ == "__main__":
    unittest.main()
