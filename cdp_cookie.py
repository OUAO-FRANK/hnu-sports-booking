#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
不关浏览器、不打扰你原来的窗口，直接拿到 eportal 的 Cookie。

原理：Chrome 支持用 --user-data-dir 起第二个实例。我们用独立配置目录
（%LOCALAPPDATA%\\hnu_cdp_profile）加调试端口启动一个专门用来登录的 Chrome，
你在这个窗口里登录一次，脚本通过 DevTools 协议把 Cookie 读出来。

    python cdp_cookie.py --wait          # 启动 Chrome 并等待你登录（推荐）
    python cdp_cookie.py --port 9222     # 已经在跑了，直接读
    python cdp_cookie.py --paste         # 手工粘贴兜底

输出 cookie.txt
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

HOST_FILTER = "hnu.edu.cn"
OUT = Path(__file__).with_name("cookie.txt")
LOGIN_URL = "https://eportal.hnu.edu.cn/v2/site/index"
PROFILE = Path(os.environ.get("LOCALAPPDATA", ".")) / "hnu_cdp_profile"

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser() -> str | None:
    for p in CHROME_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


# --------------------------------------------------------------------------- #
# 极简 WebSocket 客户端（只用标准库）
# --------------------------------------------------------------------------- #
class WS:
    def __init__(self, url: str, timeout: float = 10.0):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        hs = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
              "Upgrade: websocket\r\nConnection: Upgrade\r\n"
              f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(hs.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败")
            resp += chunk
        if b"101" not in resp.split(b"\r\n")[0]:
            raise RuntimeError(f"握手被拒: {resp[:150]}")
        self._id = 0

    def send(self, payload: dict) -> int:
        self._id += 1
        payload["id"] = self._id
        data = json.dumps(payload).encode()
        header = bytearray([0x81])
        n = len(data)
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        return self._id

    def _recv_frame(self) -> str:
        hdr = self._recv_exact(2)
        ln = hdr[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv_exact(8))[0]
        return self._recv_exact(ln).decode("utf-8", "replace")

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("连接断开")
            buf += chunk
        return buf

    def call(self, method: str, params: dict | None = None, timeout: float = 12.0) -> dict:
        want = self.send({"method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self._recv_frame())
            if msg.get("id") == want:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
        raise RuntimeError(f"{method} 超时")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# CDP 操作
# --------------------------------------------------------------------------- #
def list_targets(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
        return json.loads(r.read().decode())


def wait_cdp(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            list_targets(port)
            return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(0.5)
    return False


def grab(port: int) -> tuple[list[dict], str]:
    """返回 (hnu cookies, 当前页面 url)"""
    targets = list_targets(port)
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return [], ""
    # 优先选已经在 hnu 域名下的标签页
    pages.sort(key=lambda t: HOST_FILTER not in t.get("url", ""))
    page = pages[0]
    ws = WS(page["webSocketDebuggerUrl"])
    try:
        ws.call("Network.enable")
        res = ws.call("Network.getAllCookies")
    finally:
        ws.close()
    cookies = res.get("cookies", [])
    hnu = [c for c in cookies if HOST_FILTER in c.get("domain", "")]
    return hnu, page.get("url", "")


def cookie_header(cookies: list[dict]) -> str:
    # 会话主键排前面，纯粹为了可读性；服务端不关心顺序
    priority = ("SESSION", "JSESSIONID", "route", "CASTGC", "atrust_token", "tgw_l7_route")
    cookies = sorted(cookies, key=lambda c: (
        priority.index(c["name"]) if c["name"] in priority else 99,
        c.get("domain", ""), c["name"]))
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def launch(port: int) -> None:
    exe = find_browser()
    if not exe:
        print("[!] 没找到 Chrome 或 Edge")
        sys.exit(1)
    PROFILE.mkdir(parents=True, exist_ok=True)
    print(f"[*] 浏览器: {exe}")
    print(f"[*] 专用配置目录: {PROFILE}")
    print(f"[*] 调试端口: {port}")
    subprocess.Popen(
        [exe, f"--remote-debugging-port={port}",
         f"--user-data-dir={PROFILE}",
         "--no-first-run", "--no-default-browser-check",
         "--new-window", LOGIN_URL],
        close_fds=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--wait", action="store_true", help="启动 Chrome 并等待登录")
    ap.add_argument("--paste", action="store_true", help="手工粘贴 Cookie")
    ap.add_argument("--timeout", type=float, default=300.0, help="等待登录的秒数")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    if a.paste:
        print("把整行 Cookie 粘进来（可带 'Cookie: ' 前缀），然后回车两次：")
        lines = []
        while True:
            line = sys.stdin.readline()
            if not line or not line.strip():
                break
            lines.append(line.strip())
        raw = " ".join(lines)
        if raw.lower().startswith("cookie:"):
            raw = raw[7:].strip()
        if not raw:
            print("[!] 没读到内容")
            return 1
        Path(a.out).write_text(raw, encoding="utf-8")
        print(f"[OK] 写入 {a.out}（{len(raw)} 字符）")
        return 0

    started = False
    if a.wait:
        if wait_cdp(a.port, timeout=1.5):
            print(f"[*] 端口 {a.port} 已经有调试实例，直接复用")
        else:
            launch(a.port)
            started = True
            if not wait_cdp(a.port, timeout=30):
                print("[!] Chrome 起来了但调试端口连不上，换个端口试：--port 9223")
                return 1
            print("[*] Chrome 已启动")

    print()
    print("=" * 66)
    print("  请在弹出的 Chrome 窗口里完成登录：")
    print("  1. 输入学号和密码，登录「一网通办」")
    print("  2. 登录成功后停在这个页面，什么都别关")
    print("  3. 回到这个黑窗口等它自动继续（最多等 5 分钟）")
    print("=" * 66)
    print()

    deadline = time.time() + a.timeout
    last_n = -1
    while time.time() < deadline:
        try:
            hnu, url = grab(a.port)
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            print(f"[!] 读取失败: {exc}")
            time.sleep(1.5)
            continue

        n = len(hnu)
        if n and n != last_n:
            names = ", ".join(c["name"] for c in hnu[:8])
            print(f"[*] 检测到 {n} 个 Cookie: {names}")
            last_n = n

        # 判定"真的登录了"：必须有 eportal 域下的会话票据。
        # 注意：CAS 登录页本身也有 JSESSIONID/PHPSESSID，那不是登录态，
        # 用它去请求 eportal 会返回 e=10042。所以要认准 eportal 域的：
        #   vjuid   —— eportal 的用户 ID
        #   cas_ticket —— CAS 回跳带回来的票据
        ep = [c for c in hnu if "eportal.hnu.edu.cn" in c.get("domain", "")]
        ep_names = {c["name"] for c in ep}
        ready = ("vjuid" in ep_names) and (
            "cas_ticket" in ep_names or "vt" in ep_names)

        if ready:
            # 再等一下，让前端把 ticket 换成正式会话
            time.sleep(1.5)
            hnu, url = grab(a.port)
            header = cookie_header(hnu)
            Path(a.out).write_text(header, encoding="utf-8")
            ep_final = sorted({c["name"] for c in hnu
                               if "eportal.hnu.edu.cn" in c.get("domain", "")})
            print(f"\n[OK] 登录成功，Cookie 已写入 {a.out}（{len(header)} 字符）")
            print(f"[*] eportal 域票据: {', '.join(ep_final)}")
            print(f"[*] 当前页面: {url[:90]}")
            print("\n下一步：双击一键抢场.bat，或运行 python booking_menu.py")
            return 0

        # 还没到 eportal 域，但可能已经离开 CAS 登录页了
        if "cas/login" not in url and n >= 4:
            print(f"[*] 已离开登录页，等 eportal 票据下发...（{url[:60]}）")

        time.sleep(1.5)

    print("\n[!] 等太久了，没检测到登录态。")
    print("    确认你是在刚弹出的那个 Chrome 窗口里登录的（不是原来的窗口）。")
    print("    或者用兜底方案：python cdp_cookie.py --paste")
    return 1


if __name__ == "__main__":
    sys.exit(main())
