"""Real localhost requests; no school account or live booking."""
import importlib.util
import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
import zlib
from unittest.mock import Mock, patch


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.connections = 0
        self.requests = []

    def get_request(self):
        connection = super().get_request()
        self.connections += 1
        return connection


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append((self.command, self.path, body, self.headers.get("Cookie")))
        if self.path == "/drop":
            self.close_connection = True
            return
        status = {"/error": 503, "/plain-error": 502, "/redirect": 302,
                  "/misleading-success": 503, "/misleading-refusal": 500}.get(self.path, 200)
        payload = b"invalid json" if self.path in ("/invalid", "/plain-error") else json.dumps({"e": status if status != 200 else 0}).encode()
        if self.path in ("/misleading-success", "/misleading-refusal"):
            payload = json.dumps({"e": 0 if self.path.endswith("success") else 1}).encode()
        compressed = ""
        if self.path in ("/compressed", "/deflated"):
            accepted = self.headers.get("Accept-Encoding", "")
            payload = json.dumps({"e": 0, "d": {"accepted": accepted, "value": "synthetic" * 100}}).encode()
            coding = "gzip" if self.path == "/compressed" else "deflate"
            if coding in accepted:
                payload = gzip.compress(payload) if coding == "gzip" else zlib.compress(payload)
                compressed = coding
        self.send_response(status)
        if compressed:
            self.send_header("Content-Encoding", compressed)
        if self.path == "/redirect":
            self.send_header("Location", "http://example.invalid/no-credentials-here")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class HttpTransportTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.find_spec("http_transport")
        self.assertIsNotNone(spec, "pooled HTTP transport is required")
        import http_transport
        self.module = http_transport
        self.server = Server()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.transport = http_transport.HttpTransport(proxies={})
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        if hasattr(self, "transport"):
            self.transport.close()
            self.server.shutdown()
            self.server.server_close()

    def request(self, path="/json", **kwargs):
        return self.transport.request_json(self.url + path, headers={}, timeout=2, **kwargs)

    def test_three_requests_reuse_one_connection(self):
        for _ in range(3):
            self.assertEqual(self.request()["e"], 0)
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(self.server.connections, 1)

    def test_https_timings_reset_on_reuse_and_exclude_connect_from_writes(self):
        from urllib3.connection import HTTPSConnection
        elapsed = [0.0]
        writes = []

        def connect(connection):
            connection.sock = Mock()
            connection.send(b"synthetic-CONNECT")
            elapsed[0] += .048

        def send(connection, data):
            if connection.sock is None:
                connection.connect()
            elapsed[0] += .002
            writes.append(data)

        def request(connection, *args, **kwargs):
            connection.send(b"synthetic-headers")
            connection.send(b"synthetic-body")

        connection = self.module.MeasuredHTTPSConnection("example.invalid")
        pool = self.module.MeasuredHTTPSConnectionPool("example.invalid")
        with patch.object(HTTPSConnection, "request", request), \
             patch.object(HTTPSConnection, "connect", connect), \
             patch.object(HTTPSConnection, "send", send), \
             patch.object(self.module.urllib3.HTTPSConnectionPool, "_get_conn", return_value=connection), \
             patch.object(self.module.time, "perf_counter", side_effect=lambda: elapsed[0]):
            pool._get_conn()
            # HTTPS pools connect/validate before calling connection.request().
            connection.connect()
            connection.request("POST", "/json", body=b"synthetic-body")
            self.assertAlmostEqual(connection.request_connect_ms, 50)
            self.assertAlmostEqual(connection.request_send_ms, 4)
            self.assertTrue(connection.request_connected)
            pool._get_conn()
            connection.request("POST", "/json", body=b"synthetic-body")
            self.assertAlmostEqual(connection.request_connect_ms, 0)
            self.assertAlmostEqual(connection.request_send_ms, 4)
            self.assertFalse(connection.request_connected)
        self.assertEqual(writes, [b"synthetic-CONNECT"] + [b"synthetic-headers", b"synthetic-body"] * 2)
        connection.close()

    def test_proxy_and_direct_pools_use_their_library_socket_defaults(self):
        from urllib3.connection import HTTPConnection
        import socket
        transport = self.module.HttpTransport(proxies={"https": "http://127.0.0.1:1"})
        try:
            pool = next(iter(transport.proxy_pools.values())).connection_from_url("https://example.invalid")
            self.assertEqual(pool.conn_kw["socket_options"], [])
            connection = transport.direct.connection_from_url("https://example.invalid")._new_conn()
            self.assertIn((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1), connection.socket_options)
            self.assertEqual(connection.socket_options, HTTPConnection.default_socket_options)
            connection.close()
        finally:
            transport.close()

    def test_post_with_lost_response_is_never_replayed(self):
        result = self.request("/drop", data=b"one-booking-only")
        self.assertEqual(result["e"], "EXC")
        self.assertEqual(self.server.requests, [("POST", "/drop", b"one-booking-only", None)])

    def test_json_http_error_remains_a_business_error(self):
        self.assertEqual(self.request("/error")["e"], 503)

    def test_http_error_cannot_be_reclassified_as_accepted_or_safe_to_repeat(self):
        for path, status in (("/misleading-success", 503), ("/misleading-refusal", 500)):
            with self.subTest(path=path):
                result = self.request(path, data=b"one-booking-only")
                self.assertEqual(result["e"], status)
        self.assertEqual(len(self.server.requests), 2, "transport must never replay either POST")

    def test_shared_success_check_rejects_noninteger_zero_and_nonstatus_types(self):
        import auto_grab
        for value in (False, 0.0, [], {}, None):
            with self.subTest(value=value), self.assertRaises(auto_grab.RequestError):
                auto_grab.require_success({"e": value})
        for value in (0, "0"):
            auto_grab.require_success({"e": value})

    def test_non_json_http_error_keeps_http_status(self):
        self.assertEqual(self.request("/plain-error")["e"], 502)

    def test_malformed_success_response_is_not_empty_availability(self):
        self.assertEqual(self.request("/invalid")["e"], "EXC")

    def test_redirect_does_not_forward_credentials(self):
        self.assertEqual(self.request("/redirect")["e"], 302)
        self.assertEqual(len(self.server.requests), 1)

    def test_bypassed_local_host_does_not_use_system_proxy(self):
        self.transport.close()
        self.transport = self.module.HttpTransport(proxies={"http": "http://127.0.0.1:1"})
        with patch("http_transport.proxy_bypass", return_value=True):
            self.assertEqual(self.request()["e"], 0)

    def test_unbypassed_proxy_is_not_silently_ignored(self):
        self.transport.close()
        self.transport = self.module.HttpTransport(proxies={"http": "http://127.0.0.1:1"})
        with patch("http_transport.proxy_bypass", return_value=False):
            self.assertEqual(self.request()["e"], "EXC")
        self.assertEqual(self.server.requests, [])

    def test_cookie_is_explicit_per_request_and_not_leaked_to_next_request(self):
        self.transport.request_json(self.url + "/json", headers={"Cookie": "synthetic-session"}, timeout=2)
        self.request()
        self.assertEqual([r[3] for r in self.server.requests], ["synthetic-session", None])

    def test_active_api_advertises_and_decodes_gzip_json(self):
        import auto_grab
        for path, coding in [("/compressed", "gzip"), ("/deflated", "deflate")]:
            with self.subTest(coding=coding), patch.object(auto_grab, "BASE", self.url), \
                 patch.object(auto_grab, "get_transport", return_value=self.transport):
                result = auto_grab._req(path, cookie="synthetic", timeout=2)
            self.assertEqual(result["e"], 0)
            self.assertIn(coding, result["d"]["accepted"])
            self.assertEqual(result["d"]["value"], "synthetic" * 100)


if __name__ == "__main__":
    unittest.main()
