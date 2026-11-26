"""Browser exit detection and direct-only localhost CDP without launching real apps."""
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import io
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

import cdp_cookie
import auto
import booking_menu
from auto_grab import RequestError


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests += 1
        data = json.dumps(self.server.payload).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class LoginStartupTests(unittest.TestCase):
    def test_login_network_error_does_not_claim_cookie_expired(self):
        with patch.object(auto, "_req", return_value={"e": "EXC", "m": "SSLError"}), \
             patch.object(auto, "record") as record, redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(auto.ensure_login("PRIVATE_COOKIE"))
        self.assertIn("TLS", output.getvalue())
        self.assertNotIn("Cookie 失效", output.getvalue())
        record.assert_called_once_with("failure", stage="login", reason="TLS_ERROR")

    def test_startup_menu_distinguishes_tls_timeout_login_and_rate_limit(self):
        for response, phrase in (
            ({"e": "EXC", "m": "SSLError"}, "TLS"),
            ({"e": "EXC", "m": "ReadTimeoutError"}, "超时"),
            ({"e": 10042, "m": "PRIVATE_MESSAGE"}, "登录已失效"),
            ({"e": 429, "m": "PRIVATE_MESSAGE"}, "限流"),
        ):
            with self.subTest(response=response), patch.object(booking_menu, "load_cookie", return_value="fake"), \
                 patch.object(booking_menu, "choose_booking", side_effect=RequestError(response)), \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(booking_menu._main(), 1)
                self.assertIn(phrase, output.getvalue())
                self.assertNotIn("PRIVATE_", output.getvalue())
                self.assertNotIn("/v2/reserve/reserveHall 核对", output.getvalue())

    def test_local_cdp_never_uses_the_system_http_proxy(self):
        target = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        target.payload, proxy.payload = [], [{"id": "proxy-was-used"}]
        target.requests = proxy.requests = 0
        for server in (target, proxy):
            Thread(target=server.serve_forever, daemon=True).start()
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        try:
            with patch.dict("os.environ", {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url,
                                           "no_proxy": "", "NO_PROXY": ""}), \
                 patch("urllib.request._opener", None):
                self.assertEqual(cdp_cookie.list_targets(target.server_port), [])
            self.assertEqual(target.requests, 1)
            self.assertEqual(proxy.requests, 0)
        finally:
            for server in (target, proxy):
                server.shutdown()
                server.server_close()

    def test_launch_returns_process_for_startup_diagnostics(self):
        parent = Path(__file__).resolve().parent.parent / ".venv" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        profile = Path(tempfile.mkdtemp(prefix="login-startup-", dir=parent))
        process = Mock()
        with patch.object(cdp_cookie, "PROFILE", profile), \
             patch.object(cdp_cookie, "find_browser", return_value=r"C:\Browser\chrome.exe"), \
             patch.object(cdp_cookie.subprocess, "Popen", return_value=process), redirect_stdout(io.StringIO()):
            result = cdp_cookie.launch(9222)
        self.assertIs(result, process)

    def test_nonzero_browser_exit_stops_waiting_instead_of_claiming_port_conflict(self):
        self.assertIn("process", inspect.signature(cdp_cookie.wait_cdp).parameters)
        process = Mock()
        process.poll.return_value = 2147483651
        with patch.object(cdp_cookie, "list_targets", side_effect=URLError("synthetic")), \
             patch.object(cdp_cookie.time, "sleep") as sleep:
            self.assertFalse(cdp_cookie.wait_cdp(9222, timeout=30, process=process))
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
