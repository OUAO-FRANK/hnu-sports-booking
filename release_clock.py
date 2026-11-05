"""Read-only, bounded clock alignment; never changes the operating system clock."""
from concurrent.futures import Future
from datetime import datetime
from email.utils import parsedate_to_datetime
import math
import socket
import struct
from threading import Thread
import time

from http_transport import HttpTransport


def _decode_ntp(reply, request, sent, received, elapsed):
    if (len(reply) < 48 or reply[0] >> 6 == 3 or reply[0] & 7 != 4
            or (reply[0] >> 3) & 7 not in (3, 4) or not 1 <= reply[1] <= 15
            or reply[24:32] != request[40:48]):
        raise ValueError("Invalid NTP reply")
    def timestamp(raw):
        seconds, fraction = struct.unpack("!II", raw)
        return seconds - 2208988800 + fraction / 2**32
    server_receive = timestamp(reply[32:40])
    server_send = timestamp(reply[40:48])
    delay = (received - sent) - (server_send - server_receive)
    offset = ((server_receive - sent) + (server_send - received)) / 2
    if (server_send < server_receive or abs(offset) > 30 or not 0 <= delay <= .2
            or abs((received - sent) - elapsed) > .02):
        raise ValueError("Unusable NTP timing")
    return offset, delay


def _ntp_samples(host="time.windows.com"):
    samples = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(.7)
        connection.connect((host, 123))
        for index in range(3):
            began = time.monotonic()
            sent = time.time()
            seconds = int(sent)
            stamp = struct.pack("!II", seconds + 2208988800, int((sent - seconds) * 2**32))
            request = bytes([0x23]) + bytes(39) + stamp
            connection.send(request)
            reply = connection.recv(1024)
            received = time.time()
            samples.append(_decode_ntp(reply, request, sent, received, time.monotonic() - began))
            if index < 2:
                time.sleep(.1)
    return samples


def _portal_bounds(*, samples=3):
    url = "https://eportal.hnu.edu.cn/site/user/get-name"
    transport = HttpTransport()
    bounds = []
    try:
        for index in range(samples):
            began, sent = time.monotonic(), time.time()
            response = transport._pool(url).request(
                "GET", url, headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"},
                timeout=.8, retries=False, redirect=False, preload_content=False)
            received = time.time()
            try:
                if (response.status not in (200, 302) or response.headers.get("Age") is not None
                        or abs((received - sent) - (time.monotonic() - began)) > .02):
                    raise ValueError("Unusable portal timing")
                stamp = parsedate_to_datetime(response.headers["Date"])
                if stamp.tzinfo is None:
                    raise ValueError("Portal Date has no timezone")
                bounds.append((stamp.timestamp() - received, stamp.timestamp() + 1 - sent))
            finally:
                response.close()
            if samples > 3 and index + 1 < samples:
                time.sleep(.15)
        lower, upper = max(row[0] for row in bounds), min(row[1] for row in bounds)
        if lower > upper:
            raise ValueError("Inconsistent portal Date")
        return lower, upper
    finally:
        transport.close()


def _choose_offset(samples, portal_bounds):
    if (len(samples) != 3 or not all(math.isfinite(value) for row in samples for value in row)
            or any(abs(offset) > 30 or not 0 <= delay <= .2 for offset, delay in samples)
            or max(row[0] for row in samples) - min(row[0] for row in samples) > .03):
        raise ValueError("Inconsistent NTP samples")
    if max(offset - delay / 2 - .005 for offset, delay in samples) > min(
            offset + delay / 2 + .005 for offset, delay in samples):
        raise ValueError("Disjoint NTP timing bounds")
    offset, delay = min(samples, key=lambda row: row[1])
    guard = delay / 2 + .005
    if not portal_bounds[0] <= offset - guard <= offset + guard <= portal_bounds[1]:
        raise ValueError("NTP and portal Date disagree")
    return offset - guard, offset, guard


def _calibrate(release: datetime):
    began, wall = time.monotonic(), time.time()
    try:
        samples = _ntp_samples("time.windows.com")
    except OSError:
        try:
            samples = _ntp_samples("time.cloudflare.com")
        except OSError:
            samples = None
    code = "CLOCK_ALIGNED"
    if samples is None:
        lower, upper = _portal_bounds(samples=5)
        if (not all(math.isfinite(value) for value in (lower, upper))
                or not -30 <= lower <= upper <= 30 or upper - lower > .5):
            raise ValueError("Portal clock interval is not precise enough")
        offset, guard = (lower + upper) / 2, (upper - lower) / 2 + .02
        safe_offset = lower - .02
        code = "CLOCK_ALIGNED_COARSE"
    else:
        safe_offset, offset, guard = _choose_offset(samples, _portal_bounds())
    anchor, current = time.monotonic(), time.time()
    if abs((current - wall) - (anchor - began)) > .02:
        raise ValueError("System clock changed during calibration")
    deadline = anchor + release.timestamp() - current - safe_offset
    return deadline, {"code": code, "clock_offset_ms": round(offset * 1000, 2),
                      "clock_guard_ms": round(guard * 1000, 2)}


def start_release_clock(release, *, wait_limit=6.0):
    """Start the probes and return a receiver with a shared time budget."""
    expires_at = time.monotonic() + wait_limit
    completed = Future()

    def work():
        try:
            result = _calibrate(release)
            if time.monotonic() > expires_at:
                raise TimeoutError
            completed.set_result(result)
        except Exception as exc:
            completed.set_exception(exc)

    try:
        Thread(target=work, daemon=True, name="release-clock").start()
    except Exception as exc:
        completed.set_exception(exc)

    def receive(*, wait_budget=None):
        remaining = max(0, expires_at - time.monotonic())
        if wait_budget is not None:
            remaining = min(remaining, max(0, wait_budget))
        try:
            return completed.result(timeout=remaining)
        except Exception as exc:
            return None, {"code": "CLOCK_UNAVAILABLE", "error_type": type(exc).__name__}

    return receive


def prepare_release_clock(release, *, wait_limit=6.0):
    return start_release_clock(release, wait_limit=wait_limit)()
