"""One bounded challenge preparation alongside availability; server verification stays serial."""
from concurrent.futures import Future, TimeoutError as FutureTimeout
from threading import Thread
from time import monotonic

from auto_grab import RequestError
from run_diagnostics import record


class CaptchaPrefetch:
    def __init__(self, fetch, *, wait_limit=12.0, max_age=8.0, clock=monotonic):
        self._clock = clock
        self._started = clock()
        self._wait_limit = wait_limit
        self._max_age = max_age
        self._completed = Future()
        self._taken = False

        def run():
            try:
                challenge = fetch()
                self._completed.set_result((challenge, clock()))
            except Exception as exc:
                self._completed.set_exception(exc)

        Thread(target=run, daemon=True, name="captcha-prefetch").start()

    def peek(self):
        """Inspect a fresh completed preparation without waiting or consuming it."""
        if self._taken or not self._completed.done():
            return None
        challenge, ready_at = self._completed.result()
        return challenge if 0 <= self._clock() - ready_at <= self._max_age else None

    def take(self, *, wait_budget=None):
        if self._taken:
            return None
        self._taken = True
        remaining = max(0, self._started + self._wait_limit - self._clock())
        if wait_budget is not None:
            remaining = min(remaining, max(0, wait_budget))
        try:
            challenge, ready_at = self._completed.result(timeout=remaining)
        except FutureTimeout:
            record("timing", stage="captcha_prefetch", code="CAPTCHA_TIMEOUT")
            # The old GET may still be in flight. Do not race it with another captcha GET.
            raise RequestError({"e": "CAPTCHA_TIMEOUT", "m": "验证码获取超过总等待上限，请检查网络后重试"}) from None
        fresh = 0 <= self._clock() - ready_at <= self._max_age
        record("timing", stage="captcha_prefetch", code="READY" if fresh else "STALE",
               duration_ms=round((ready_at - self._started) * 1000, 1))
        return challenge if fresh else None
