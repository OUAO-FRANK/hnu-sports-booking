"""Allowlisted JSONL events; raw console output and API bodies are never captured."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
from queue import SimpleQueue
import re
from threading import Thread
from urllib.parse import urlsplit
from uuid import uuid4


_active = None
_FIELDS = frozenset({
    "stage", "duration_ms", "attempt", "code", "verified", "release_elapsed_ms",
    "path", "method", "status", "headers_ms", "body_ms", "via_proxy", "error_type",
    "total_cells", "available_cells", "slots_count", "matching_count", "candidate_count",
    "time_counts", "venue", "date", "times", "outcome", "exit_code", "source", "compressed",
    "points_count", "exact_ocr_matches",
    "clock_offset_ms", "clock_guard_ms",
    "reason",
    "connect_ms", "send_ms", "response_wait_ms", "connection_reused",
    "run_mode", "release_time",
})


class RunLog:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"run-{stamp}-{os.getpid()}-{uuid4().hex[:8]}.jsonl"
        self.file = self.path.open("x", encoding="utf-8", buffering=1)
        self.queue = SimpleQueue()
        self.failed = False
        self.worker = Thread(target=self._write, daemon=True, name="diagnostics")
        self.worker.start()

    def _write(self):
        try:
            while (line := self.queue.get()) is not None:
                self.file.write(line + "\n")
        except OSError:
            self.failed = True
        finally:
            self.file.close()

    def emit(self, event, fields):
        safe = {key: value for key, value in fields.items() if key in _FIELDS}
        if "path" in safe:
            path = urlsplit(str(safe["path"])).path
            safe["path"] = path if re.fullmatch(r"/site/[a-z0-9/-]+", path) else "other"
        row = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
               "event": event, **safe}
        self.queue.put(json.dumps(row, ensure_ascii=False, separators=(",", ":")))

    def close(self):
        self.queue.put(None)
        self.worker.join(timeout=2)


def record(event, **fields):
    if _active is not None:
        _active.emit(event, fields)


@contextmanager
def session(directory=None):
    global _active
    if _active is not None:
        yield _active
        return
    try:
        log = RunLog(Path(directory) if directory is not None else Path(__file__).resolve().parent / "out" / "logs")
    except OSError:
        print("[!] 无法保存诊断日志，本次只显示终端输出")
        yield None
        return
    _active = log
    print(f"诊断日志：{log.path}（不保存登录凭据、验证码或支付链接）")
    record("run_start")
    outcome = "finished"
    try:
        yield log
    except BaseException as exc:
        outcome = "error"
        record("error", error_type=type(exc).__name__)
        raise
    finally:
        record("run_end", outcome=outcome)
        _active = None
        log.close()
        if log.failed:
            print("[!] 诊断日志写入失败，文件可能不完整")
        elif not log.worker.is_alive():
            try:
                from tools.analyze_run import analyze_file, format_report
                report = analyze_file(log.path)
                if report["run_mode"] != "UNKNOWN":
                    summary = log.path.with_suffix(".summary.json")
                    summary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    print(format_report(report))
                    print(f"复盘报告：{summary}")
            except Exception:
                print("[!] 自动复盘未生成，可稍后用 tools/analyze_run.py 读取原日志")
