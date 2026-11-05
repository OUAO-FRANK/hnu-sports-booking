"""Local HTTP/1.1 benchmark. Never connects to the school or submits a booking."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import statistics
import sys
import threading
import time
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from http_transport import HttpTransport


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        connection, address = super().get_request()
        self.connections += 1
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        time.sleep(self.connection_delay)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.requests += 1
        payload = b'{"e":0,"d":{}}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def benchmark(requests, connection_delay_ms):
    server = Server(("127.0.0.1", 0), Handler)
    server.connections = server.requests = 0
    server.connection_delay = connection_delay_ms / 1000
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/benchmark"
    transport = HttpTransport(proxies={})
    opener = build_opener(ProxyHandler({}))
    results = {}
    try:
        for mode in ("previous_urllib", "pooled_after_warmup"):
            if mode == "pooled_after_warmup":
                transport.request_json(url, headers={}, timeout=2)
            connections = server.connections
            durations = []
            for _ in range(requests):
                started = time.perf_counter()
                if mode == "previous_urllib":
                    with opener.open(url, timeout=2) as response:
                        result = json.loads(response.read())
                else:
                    result = transport.request_json(url, headers={}, timeout=2)
                if result.get("e") != 0:
                    raise RuntimeError("Local benchmark request failed")
                durations.append((time.perf_counter() - started) * 1000)
            results[mode] = {"requests": requests, "new_connections": server.connections - connections,
                             "median_ms": round(statistics.median(durations), 3),
                             "max_ms": round(max(durations), 3)}
        return {"scope": "localhost HTTP only; not peak-time booking performance",
                "simulated_new_connection_delay_ms": connection_delay_ms,
                "external_requests": 0, "results": results}
    finally:
        transport.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=12)
    parser.add_argument("--connection-delay-ms", type=float, default=0,
                        help="Optional artificial delay per new connection; default 0")
    args = parser.parse_args()
    if not 1 <= args.requests <= 100 or not 0 <= args.connection_delay_ms <= 200:
        parser.error("requests must be 1-100; connection-delay-ms must be 0-200")
    print(json.dumps(benchmark(args.requests, args.connection_delay_ms), indent=2))
