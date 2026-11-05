"""Read one explicit JSONL log and report only allowlisted metrics; never write files."""
import argparse
import json
import math
from pathlib import Path
import sys

PHASES = {"query": "查场", "captcha_get": "验证码取图", "captcha_infer": "识别",
          "captcha_verify": "验证码校验", "captcha": "验证码全流程", "launch": "提交"}
REASONS = ("ACCEPTED", "UNCERTAIN", "NOT_RELEASED", "UNAVAILABLE", "REJECTED", "UNKNOWN")
CLOCKS = ("CLOCK_ALIGNED", "CLOCK_ALIGNED_COARSE", "CLOCK_UNAVAILABLE")
CONNECTION_METRICS = ("connect_ms", "send_ms", "response_wait_ms")
PAYMENT_UI_REASONS = {"USER_CLOSED": "用户关闭", "DEADLINE": "倒计时结束",
                      "RESERVATION_ENDED": "服务端确认预约结束", "UI_ERROR": "窗口异常",
                      "INTERRUPTED": "用户中断", "UNEXPECTED_EXIT": "窗口意外结束",
                      "CONFIRMED": "服务端确认生效", "UNKNOWN": "未知（旧日志或未显示窗口）"}


def _number(value):
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(float(value)) else None
    except OverflowError:
        return None


def _summary(values):
    if not values:
        return None
    ordered, count = sorted(values), len(values)
    middle = ordered[count // 2] if count % 2 else ordered[count // 2 - 1] / 2 + ordered[count // 2] / 2
    return {"count": count, "median_ms": round(middle, 3), "max_ms": round(ordered[-1], 3)}


def analyze_lines(lines):
    report = {"evidence": {"kind": "UNVERIFIED", "real_booking": "UNKNOWN", "peak_load": "UNKNOWN"},
              "run_mode": "UNKNOWN", "first_dispatch_ms": None, "first_accepted_ms": None,
              "first_accepted_request_ms": None,
              "phases": {}, "launch_results": {}, "connections": {},
              "first_submission_connection": dict.fromkeys(("connection_reused", *CONNECTION_METRICS)),
              "clock": {"status": "UNKNOWN", "offset_ms": None, "guard_ms": None},
              "receipt": {"outcome": "UNKNOWN", "status": None},
              "payment_ui": {"reason": "UNKNOWN", "outcome": "UNKNOWN"}, "malformed_lines": 0}
    phases = {stage: [] for stage in PHASES}
    connection_values = {key: [] for key in CONNECTION_METRICS}
    http_count = reuse_samples = reused = 0
    dispatch_seen = accepted_seen = connection_seen = False
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line.lstrip("\ufeff"))
            if not isinstance(row, dict):
                raise ValueError("Expected object")
        except (ValueError, TypeError):
            report["malformed_lines"] += 1
            continue
        event, stage = row.get("event"), row.get("stage")
        if event == "plan" and row.get("run_mode") in ("scheduled", "immediate", "dry_run"):
            report["run_mode"] = row["run_mode"]
        if event == "launch_start" and not dispatch_seen:
            dispatch_seen = True
            report["first_dispatch_ms"] = _number(row.get("release_elapsed_ms"))
        if event == "timing" and stage in tuple(PHASES):
            duration = _number(row.get("duration_ms"))
            if duration is not None and duration >= 0:
                phases[stage].append(duration)
            if stage == "launch":
                accepted = str(row.get("code")) == "0"
                reason = row.get("reason")
                reason = "ACCEPTED" if accepted else reason if reason in REASONS[1:] else "UNKNOWN"
                counts = report["launch_results"]
                counts[reason] = counts.get(reason, 0) + 1
                if accepted and not accepted_seen:
                    accepted_seen = True
                    report["first_accepted_ms"] = _number(row.get("release_elapsed_ms"))
                    report["first_accepted_request_ms"] = duration if duration is not None and duration >= 0 else None
        if event == "timing" and stage in ("release_clock", "release_clock_ready"):
            status = row.get("code")
            if status in CLOCKS:
                report["clock"] = {"status": status, "offset_ms": _number(row.get("clock_offset_ms")),
                                   "guard_ms": _number(row.get("clock_guard_ms"))}
        if event == "receipt" and row.get("outcome") == "MATCHED" and str(row.get("status")) in ("5", "6", "7"):
            report["receipt"] = {"outcome": "MATCHED", "status": int(row["status"])}
        if event == "payment_ui":
            reason = row.get("reason")
            if (isinstance(reason, str) and reason in PAYMENT_UI_REASONS and reason != "UNKNOWN"
                    and row.get("outcome") == ("CONFIRMED" if reason == "CONFIRMED" else "UNCONFIRMED")):
                report["payment_ui"] = {"reason": reason, "outcome": row["outcome"]}
        if event == "http":
            http_count += 1
            first_submission = not connection_seen and row.get("path") == "/site/reservation/launch"
            if first_submission:
                connection_seen = True
            if type(row.get("connection_reused")) is bool:
                reuse_samples += 1
                reused += row["connection_reused"]
                if first_submission:
                    report["first_submission_connection"]["connection_reused"] = row["connection_reused"]
            for key in CONNECTION_METRICS:
                value = _number(row.get(key))
                if value is not None and value >= 0:
                    connection_values[key].append(value)
                    if first_submission:
                        report["first_submission_connection"][key] = value
    report["phases"] = {stage: _summary(values) for stage, values in phases.items() if values}
    report["connections"] = {"requests": http_count, "reuse_samples": reuse_samples, "reused": reused,
                             "reuse_percent": round(reused / reuse_samples * 100, 1) if reuse_samples else None,
                             **{key: _summary(values) for key, values in connection_values.items()}}
    return report


def analyze_file(path):
    with Path(path).open("r", encoding="utf-8", errors="replace") as source:
        return analyze_lines(source)


def format_report(report):
    def ms(value):
        return "未知（缺少显式计时）" if value is None else f"{value:.3f} ms"
    modes = {"scheduled": "定时", "immediate": "立即", "dry_run": "干跑", "UNKNOWN": "未知"}
    text = ["运行日志摘要（是否属于高峰实测，需结合运行现场确认）",
            f"运行方式：{modes[report['run_mode']]}",
            f"首发距本次放号基准：{ms(report['first_dispatch_ms'])}",
            f"首次成功提交响应距本次放号基准：{ms(report['first_accepted_ms'])}",
            f"首次成功提交请求往返：{ms(report['first_accepted_request_ms'])}",
            "上述间隔仅采用 release_elapsed_ms，不从本地时间戳反推；响应成功仍须核对回执。"]
    for stage, label in PHASES.items():
        values = report["phases"].get(stage)
        if values:
            text.append(f"{label}：{values['count']} 次，中位 {values['median_ms']:.3f} ms，最大 {values['max_ms']:.3f} ms")
    reasons = [f"{reason}={report['launch_results'][reason]}" for reason in REASONS if reason in report["launch_results"]]
    text.append("提交分类：" + ("，".join(reasons) or "未知"))
    first = report["first_submission_connection"]
    reuse = {True: "是", False: "否"}.get(first["connection_reused"], "未知")
    text.append(f"首次提交连接：复用 {reuse}；连接 {ms(first['connect_ms'])}；"
                f"发送 {ms(first['send_ms'])}；响应等待 {ms(first['response_wait_ms'])}")
    connections = report["connections"]
    ratio = "未知" if connections["reuse_percent"] is None else f"{connections['reuse_percent']:.1f}%"
    text.append(f"连接复用：{ratio}（{connections['reuse_samples']} 个已记录样本，共 {connections['requests']} 次 HTTP）")
    for key, label in zip(CONNECTION_METRICS, ("连接", "发送", "响应等待")):
        values = connections[key]
        if values:
            text.append(f"{label}中位：{values['median_ms']:.3f} ms，最大 {values['max_ms']:.3f} ms")
    clock = report["clock"]
    text.append(f"校时：{clock['status']}；偏移 {ms(clock['offset_ms'])}；余量 {ms(clock['guard_ms'])}")
    receipt = report["receipt"]
    label = "回执已匹配" if receipt["outcome"] == "MATCHED" else "回执未知"
    states = {5: "已结束", 6: "待付款", 7: "已生效"}
    text.append(f"{label}：{states.get(receipt['status'], '未知')}")
    payment_ui = report.get("payment_ui", {"reason": "UNKNOWN"})
    text.append(f"支付窗口结束原因：{PAYMENT_UI_REASONS[payment_ui['reason']]}")
    text.append(f"跳过损坏或非对象行：{report['malformed_lines']}")
    return "\n".join(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读分析一个指定 JSONL 日志，不输出凭据、ID 或链接")
    parser.add_argument("path", type=Path)
    parser.add_argument("--json", action="store_true", help="仅输出安全的机器可读 JSON")
    args = parser.parse_args(argv)
    try:
        report = analyze_file(args.path)
    except OSError:
        print("无法读取指定日志。", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, allow_nan=False) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
