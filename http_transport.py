"""Verified HTTP pools with explicit credentials and no automatic request replay."""
from functools import lru_cache
import json
import ssl
import time
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

import urllib3
from urllib3.connection import HTTPSConnection
from run_diagnostics import record


class MeasuredHTTPSConnection(HTTPSConnection):
    def connect(self):
        started = time.perf_counter()
        self.request_connecting = True
        self.request_connected = True
        try:
            return super().connect()
        finally:
            self.request_connect_ms += (time.perf_counter() - started) * 1000
            self.request_connecting = False

    def send(self, data):
        # CONNECT writes belong to the connection phase, including proxy TLS.
        if self.request_connecting:
            return super().send(data)
        started, prior_connect_ms = time.perf_counter(), self.request_connect_ms
        try:
            return super().send(data)
        finally:
            finished = time.perf_counter()
            self.request_send_ms += max(0, (finished - started) * 1000
                                        - (self.request_connect_ms - prior_connect_ms))
            self.request_last_send_at = finished


class MeasuredHTTPSConnectionPool(urllib3.HTTPSConnectionPool):
    ConnectionCls = MeasuredHTTPSConnection

    def _get_conn(self, timeout=None):
        connection = super()._get_conn(timeout=timeout)
        # Reset before urllib3 prepares a proxy tunnel or validates HTTPS.
        connection.request_connect_ms = 0.0
        connection.request_send_ms = 0.0
        connection.request_connected = False
        connection.request_last_send_at = None
        connection.request_connecting = False
        return connection


class HttpTransport:
    def __init__(self, *, proxies=None):
        self.proxies = dict(getproxies() if proxies is None else proxies)
        context = ssl.create_default_context()
        context.set_alpn_protocols(["http/1.1"])
        options = {"ssl_context": context, "num_pools": 4, "maxsize": 2}
        self.direct = urllib3.PoolManager(**options)
        self.proxy_pools = {}
        for proxy in {self.proxies[key] for key in ("http", "https") if self.proxies.get(key)}:
            address = proxy if "://" in proxy else "http://" + proxy
            parsed = urlsplit(address)
            headers = None
            if parsed.username is not None:
                auth = unquote(parsed.username) + ":" + unquote(parsed.password or "")
                headers = urllib3.make_headers(proxy_basic_auth=auth)
            self.proxy_pools[proxy] = urllib3.ProxyManager(address, proxy_headers=headers, **options)
        for manager in (self.direct, *self.proxy_pools.values()):
            manager.pool_classes_by_scheme = dict(manager.pool_classes_by_scheme,
                                                 https=MeasuredHTTPSConnectionPool)

    def _pool(self, url):
        parsed = urlsplit(url)
        proxy = self.proxies.get(parsed.scheme)
        if proxy and not proxy_bypass(parsed.netloc):
            return self.proxy_pools[proxy]
        return self.direct

    def request_json(self, url, *, headers, data=None, timeout=12.0):
        started = time.perf_counter()
        metrics = {"path": urlsplit(url).path, "method": "POST" if data is not None else "GET"}
        try:
            pool = self._pool(url)
            metrics["via_proxy"] = pool is not self.direct
            response = pool.request(
                "POST" if data is not None else "GET", url, body=data, headers=headers,
                timeout=urllib3.Timeout(total=timeout, connect=min(3.0, timeout), read=timeout),
                retries=False, redirect=False, preload_content=False,
            )
            headers_at = time.perf_counter()
            metrics.update(status=response.status, headers_ms=round((headers_at - started) * 1000, 1))
            connection = response.connection
            if isinstance(connection, MeasuredHTTPSConnection):
                metrics.update(connect_ms=round(connection.request_connect_ms, 3),
                               send_ms=round(connection.request_send_ms, 3),
                               connection_reused=not connection.request_connected)
                if connection.request_last_send_at is not None:
                    metrics["response_wait_ms"] = round(
                        max(0, headers_at - connection.request_last_send_at) * 1000, 3)
            metrics["compressed"] = response.headers.get("Content-Encoding", "").lower() in ("gzip", "deflate")
            try:
                raw = response.read()
            except Exception:
                response.close()
                raise
            finally:
                response.release_conn()
            metrics["body_ms"] = round((time.perf_counter() - headers_at) * 1000, 1)
            # Do not follow redirects with the caller's Cookie header.
            if 300 <= response.status < 400:
                return {"e": response.status, "m": "HTTPRedirect"}
            # An error response cannot prove a write was accepted or safely refused.
            if response.status >= 400:
                return {"e": response.status, "m": "HTTPError"}
            result = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(result, dict):
                return {"e": "SHAPE", "m": "Expected a JSON object"}
            return result
        except Exception as exc:
            metrics["error_type"] = type(exc).__name__
            # Exception messages can contain proxy credentials or request URLs.
            return {"e": "EXC", "m": type(exc).__name__}
        finally:
            record("http", duration_ms=round((time.perf_counter() - started) * 1000, 1), **metrics)

    def close(self):
        self.direct.clear()
        for pool in self.proxy_pools.values():
            pool.clear()


@lru_cache(maxsize=1)
def get_transport():
    return HttpTransport()
