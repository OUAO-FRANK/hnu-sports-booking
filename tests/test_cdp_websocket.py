"""CDP call deadlines on a fake socket; no browser, credentials or network."""
import json
import struct
import unittest
from unittest.mock import patch

import cdp_cookie


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def now(self):
        return self.elapsed


class Socket:
    def __init__(self, clock, chunks, *, original_timeout=10.0, send_delay=0.0):
        self.clock = clock
        self.chunks = list(chunks)
        self.timeout = original_timeout
        self.send_delay = send_delay
        self.send_count = 0
        self.recv_timeouts = []

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def wait(self, delay):
        if self.timeout is not None and delay > self.timeout:
            self.clock.elapsed += self.timeout
            raise TimeoutError("synthetic socket timeout")
        self.clock.elapsed += delay

    def sendall(self, data):
        self.send_count += 1
        self.wait(self.send_delay)

    def recv(self, size):
        self.recv_timeouts.append(self.timeout)
        delay, chunk = self.chunks.pop(0)
        self.wait(delay)
        self.assert_size(size, chunk)
        return chunk

    @staticmethod
    def assert_size(size, chunk):
        if len(chunk) > size:
            raise AssertionError("fake chunk exceeds recv size")


def frame(value, *, length_bytes=0, header_delay=0, length_delay=0, body_delay=0):
    data = json.dumps(value).encode()
    if length_bytes:
        marker, fmt = (126, ">H") if length_bytes == 2 else (127, ">Q")
        return [(header_delay, bytes([0x81, marker])),
                (length_delay, struct.pack(fmt, len(data))), (body_delay, data)]
    return [(header_delay, bytes([0x81, len(data)])), (body_delay, data)]


class WebSocketDeadlineTests(unittest.TestCase):
    def connection(self, clock, chunks, **kwargs):
        ws = object.__new__(cdp_cookie.WS)
        ws.sock = Socket(clock, chunks, **kwargs)
        ws._id = 0
        ws._deadline = None
        return ws

    def call(self, ws, clock, timeout):
        with patch.object(cdp_cookie.time, "time", side_effect=clock.now), \
             patch.object(cdp_cookie.time, "monotonic", side_effect=clock.now):
            return ws.call("Network.getAllCookies", timeout=timeout)

    def test_call_timeout_bounds_blocked_frame_header(self):
        clock = Clock()
        ws = self.connection(clock, frame({"id": 1, "result": {}}, header_delay=.2))
        with self.assertRaisesRegex(RuntimeError, "超时"):
            self.call(ws, clock, .05)
        self.assertAlmostEqual(clock.elapsed, .05)
        self.assertEqual(ws.sock.gettimeout(), 10)
        self.assertEqual(ws.sock.send_count, 1, "CDP commands must never be replayed")

    def test_fragmented_frame_body_shares_one_total_deadline(self):
        clock = Clock()
        data = json.dumps({"id": 1, "result": {}}).encode()
        ws = self.connection(clock, [(.2, bytes([0x81, len(data)])),
                                    (.4, data[:4]), (.4, data[4:8]), (.4, data[8:])])
        with self.assertRaisesRegex(RuntimeError, "超时"):
            self.call(ws, clock, 1)
        self.assertLessEqual(clock.elapsed, 1.000001)
        self.assertEqual(ws.sock.gettimeout(), 10)

    def test_extended_frame_length_uses_remaining_call_budget(self):
        for length_bytes, padding in ((2, 150), (8, 66000)):
            with self.subTest(length_bytes=length_bytes):
                clock = Clock()
                ws = self.connection(clock, frame({"id": 1, "result": {"padding": "x" * padding}},
                    length_bytes=length_bytes, header_delay=.2, length_delay=.4))
                with self.assertRaisesRegex(RuntimeError, "超时"):
                    self.call(ws, clock, .5)
                self.assertAlmostEqual(clock.elapsed, .5)
                self.assertEqual(ws.sock.gettimeout(), 10)

    def test_sending_a_command_is_part_of_the_same_timeout(self):
        clock = Clock()
        ws = self.connection(clock, frame({"id": 1, "result": {}}), send_delay=.2)
        with self.assertRaisesRegex(RuntimeError, "超时"):
            self.call(ws, clock, .05)
        self.assertAlmostEqual(clock.elapsed, .05)
        self.assertEqual(ws.sock.send_count, 1)
        self.assertEqual(ws.sock.gettimeout(), 10)

    def test_wall_clock_jump_does_not_expire_a_live_call(self):
        clock = Clock()
        ws = self.connection(clock, frame({"method": "Network.event"}, header_delay=.1)
                             + frame({"id": 1, "result": {"cookies": []}}, header_delay=.1))
        with patch.object(cdp_cookie.time, "time", side_effect=[0, 0, 1000]), \
             patch.object(cdp_cookie.time, "monotonic", side_effect=clock.now):
            self.assertEqual(ws.call("Network.getAllCookies", timeout=1), {"cookies": []})
        self.assertAlmostEqual(clock.elapsed, .2)
        self.assertEqual(ws.sock.gettimeout(), 10)

    def test_socket_timeout_is_restored_after_success_and_cdp_error(self):
        for response in ({"id": 1, "result": {"cookies": []}},
                         {"id": 1, "error": {"message": "synthetic failure"}}):
            with self.subTest(response=response):
                clock = Clock()
                ws = self.connection(clock, frame(response), original_timeout=None)
                if "error" in response:
                    with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                        self.call(ws, clock, .5)
                else:
                    self.assertEqual(self.call(ws, clock, .5), {"cookies": []})
                self.assertIsNone(ws.sock.gettimeout())
                self.assertIsNone(ws._deadline)


if __name__ == "__main__":
    unittest.main()
