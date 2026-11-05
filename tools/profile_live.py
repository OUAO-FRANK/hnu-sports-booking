"""Real query/captcha profiler. Booking, cancellation and payment endpoints are forbidden."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import statistics
import sys
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from auto import infer_captcha
from auto_grab import BASE, UA, RequestError, load_cookie, require_success
from booking_catalog import china_now
from captcha_engine import CaptchaEngine
from captcha_prefetch import CaptchaPrefetch
from captcha_solver import aes_ecb_encrypt_b64
from http_transport import HttpTransport
from run_diagnostics import session
from shape_adapter import parse_margin

CHECK = "/site/captcha/click-check"
ALLOWED = {"/site/user/get-name", "/site/reservation/resource-info",
           "/site/reservation/resource-info-margin", "/site/captcha/click-get", CHECK}


class LiveProbe:
    def __init__(self, cookie, transport):
        self.transport = transport
        self.headers = {"Cookie": cookie, "User-Agent": UA, "Accept": "application/json",
                        "Accept-Encoding": "gzip, deflate", "X-Requested-With": "XMLHttpRequest",
                        "Referer": BASE + "/v2/reserve/reserveHall"}

    def call(self, path, *, params=None, form=None):
        if path not in ALLOWED or (form is not None) != (path == CHECK):
            raise ValueError("Profiler permits only catalog queries and captcha click-check")
        url = BASE + path + (("?" + urlencode(params)) if params else "")
        headers = dict(self.headers)
        data = None
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            data = urlencode(form).encode()
        result = self.transport.request_json(url, headers=headers, data=data, timeout=5)
        if path != CHECK or str(result.get("e")) in ("10042", "10043", "EXC", "429", "500", "502", "503", "504"):
            require_success(result)
        return result


def safe_code(value):
    value = str(value)
    return value if (value.isdigit() and len(value) <= 8) or value in ("EXC", "SHAPE", "CAPTCHA_TIMEOUT") else "OTHER"


def timed(operation):
    started = time.perf_counter()
    result = operation()
    return result, round((time.perf_counter() - started) * 1000, 1)


def profile(probe, *, venue, day, samples=3, captcha=False):
    if not 1 <= samples <= 3:
        raise ValueError("Use 1-3 samples per mode")
    report = {"scope": "real queries and optional captcha verification; no booking or payment",
              "venue": venue, "date": day, "rounds": [], "booking_requests": 0, "payment_requests": 0}
    try:
        _, report["login_ms"] = timed(lambda: probe.call("/site/user/get-name"))
        info, report["resource_ms"] = timed(lambda: probe.call(
            "/site/reservation/resource-info", params={"resource_id": venue}))
        use_captcha = captcha and str(info.get("d", {}).get("is_verify")) == "1"
        engine = None
        if use_captcha:
            engine, report["model_load_ms"] = timed(CaptchaEngine)
            _, report["model_warmup_ms"] = timed(engine.warmup)

        def query(row):
            response, row["query_ms"] = timed(lambda: probe.call(
                "/site/reservation/resource-info-margin",
                params={"resource_id": venue, "start_time": day, "end_time": day}))
            slots = parse_margin(response, include_unavailable=True)
            if slots is None:
                raise RuntimeError("Unknown calendar shape")
            row["calendar_cells"] = len(slots)
            row["available_cells"] = sum(slot["available"] for slot in slots)

        def prepare():
            cap_response, get_ms = timed(lambda: probe.call("/site/captcha/click-get"))
            cap = cap_response.get("d")
            points, infer_ms = timed(lambda: infer_captcha(engine, cap)) if cap else ([], 0)
            return cap, points, {"captcha_get_ms": get_ms, "inference_ms": infer_ms}

        for mode in (("serial", "parallel") if use_captcha else ("query",)):
            for attempt in range(1, samples + 1):
                started = time.perf_counter()
                row = {"mode": mode, "attempt": attempt}
                pending = CaptchaPrefetch(prepare) if mode == "parallel" else None
                query(row)
                if use_captcha:
                    prepared = pending.take() if pending is not None else prepare()
                    cap, points, durations = prepared if prepared is not None else prepare()
                    row.update(durations)
                    row["recognized"] = len(points) == 4
                    row["verified"] = False
                    if row["recognized"]:
                        coordinates = json.dumps([{"x": x, "y": y} for x, y in points], separators=(",", ":"))
                        response, row["verify_ms"] = timed(lambda: probe.call(CHECK, form={
                            "data[token]": cap["token"], "data[pointJson]": aes_ecb_encrypt_b64(coordinates, cap["secretKey"]),
                            "data[captchaType]": "clickWord"}))
                        row["verify_code"] = safe_code(response.get("e"))
                        row["verified"] = response.get("e") in (0, "0")
                row["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
                report["rounds"].append(row)
                time.sleep(.5)
        report["median_total_ms"] = {mode: round(statistics.median(row["total_ms"] for row in report["rounds"]
                                                   if row["mode"] == mode), 1)
                                     for mode in {row["mode"] for row in report["rounds"]}}
        if use_captcha:
            modes = ("serial", "parallel")
            report["verified_rounds"] = {mode: sum(row.get("verified", False) for row in report["rounds"]
                                                   if row["mode"] == mode) for mode in modes}
            report["median_verified_total_ms"] = {}
            for mode in modes:
                successful = [row["total_ms"] for row in report["rounds"]
                              if row["mode"] == mode and row.get("verified")]
                report["median_verified_total_ms"][mode] = round(statistics.median(successful), 1) if successful else None
        report["status"] = "ok"
    except RequestError as exc:
        report.update(status="login_required" if exc.code in ("10042", "10043") else "stopped",
                      code=safe_code(exc.code))
    except Exception as exc:
        report.update(status="stopped", error_type=type(exc).__name__)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", type=int, default=57)
    parser.add_argument("--date", default=china_now().date().isoformat())
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--captcha", action="store_true", help="Also test captcha recognition/check; never reserve")
    parser.add_argument("--direct", action="store_true", help="Direct route for this process only; no system changes")
    args = parser.parse_args()
    try:
        datetime.fromisoformat(args.date)
        if not 1 <= args.samples <= 3 or args.venue <= 0:
            raise ValueError("venue must be positive; samples must be 1-3")
    except ValueError as exc:
        parser.error(str(exc))
    transport = HttpTransport(proxies={} if args.direct else None)
    try:
        with session():
            result = profile(LiveProbe(load_cookie(), transport), venue=args.venue,
                             day=args.date, samples=args.samples, captcha=args.captcha)
            result["route"] = "direct" if args.direct else "system"
            output = ROOT / "out" / "logs" / f"profile-{china_now():%Y%m%d-%H%M%S}-{os.getpid()}.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"Report: {output}")
            return 0 if result["status"] == "ok" else 2
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
