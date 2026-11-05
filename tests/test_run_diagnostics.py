"""Only structured, non-sensitive events may reach shareable diagnostics."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("run_diagnostics"), "safe automatic logs are required")
        import run_diagnostics
        self.module = run_diagnostics
        parent = Path(__file__).resolve().parent.parent / ".venv" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="diagnostics-", dir=parent))

    def rows(self, path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_secret_fields_and_query_parameters_are_not_written(self):
        with self.module.session(self.folder) as log:
            self.module.record("http", path="/site/reservation/my-list?token=PRIVATE_TOKEN",
                method="GET", duration_ms=12.3, cookie="PRIVATE_COOKIE",
                connect_ms=0.0, send_ms=.2, response_wait_ms=12.0, connection_reused=True,
                response={"name": "PRIVATE_NAME"}, payment_url="https://example.invalid/PRIVATE_PAYMENT")
        raw = log.path.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_", raw)
        row = next(r for r in self.rows(log.path) if r["event"] == "http")
        self.assertEqual(row["path"], "/site/reservation/my-list")
        self.assertEqual(row["duration_ms"], 12.3)
        self.assertEqual(row["connect_ms"], 0.0)
        self.assertEqual(row["send_ms"], .2)
        self.assertEqual(row["response_wait_ms"], 12.0)
        self.assertTrue(row["connection_reused"])

    def test_nested_session_shares_one_log_and_flushes_on_exit(self):
        with self.module.session(self.folder) as first:
            with self.module.session(self.folder) as second:
                self.assertIs(first, second)
                self.module.record("query", attempt=1, time_counts={"20:10-21:10": 0})
            self.module.record("timing", stage="query", duration_ms=5)
        rows = self.rows(first.path)
        self.assertEqual([r["event"] for r in rows], ["run_start", "query", "timing", "run_end"])
        self.assertEqual(len(list(self.folder.glob("*.jsonl"))), 1)

    def test_exception_closes_log_without_saving_exception_message(self):
        with self.assertRaisesRegex(RuntimeError, "PRIVATE_EXCEPTION"):
            with self.module.session(self.folder) as log:
                raise RuntimeError("PRIVATE_EXCEPTION")
        rows = self.rows(log.path)
        self.assertEqual(rows[-1]["outcome"], "error")
        self.assertNotIn("PRIVATE_EXCEPTION", log.path.read_text(encoding="utf-8"))

    def test_no_session_does_not_create_a_file(self):
        self.module.record("query", attempt=1)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_finished_session_writes_safe_automatic_summary(self):
        with self.module.session(self.folder) as log:
            self.module.record("plan", run_mode="scheduled", release_time="2026-11-04T22:00:00")
            self.module.record("timing", stage="launch", code=0, reason="ACCEPTED",
                               duration_ms=1200.0, release_elapsed_ms=1234.5,
                               cookie="PRIVATE_COOKIE", payment_url="PRIVATE_URL")
            self.module.record("receipt", status="6", outcome="MATCHED")
        summary = log.path.with_suffix(".summary.json")
        self.assertTrue(summary.exists())
        raw = summary.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_", raw)
        self.assertEqual(json.loads(raw)["first_accepted_ms"], 1234.5)

    def test_summary_read_failure_does_not_break_a_completed_session(self):
        with patch("tools.analyze_run.analyze_file", side_effect=RuntimeError("PRIVATE_ERROR")):
            with self.module.session(self.folder) as log:
                self.module.record("result", exit_code=0)
        self.assertEqual(self.rows(log.path)[-1]["outcome"], "finished")


if __name__ == "__main__":
    unittest.main()
