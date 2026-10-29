#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cookie 自动刷新 —— 让脚本自己处理登录过期，不用人管。

=================== 原理 ===================

HNU 是两层会话：

    CAS 主票据  iPlanetDirectoryPro   存活 13.8 天
        ↓ 用它换
    eportal 会话  vjuid/vt/PHPSESSID   存活 2-24 小时

所以 eportal 会话过期时，只要**在浏览器里访问一次 eportal**，
CAS 会自动用主票据重新签发会话 —— **不需要输密码**。

自动刷新就是复刻这个动作：
    1. 检测到 e=10042
    2. 用 CDP 打开 https://eportal.hnu.edu.cn
    3. 等页面跳转完成（CAS 签名 + 回跳）
    4. 从浏览器读出新的 cookie
    5. 写回 cookie.txt
    6. 继续原任务

=================== 前提 ===================

需要一个调试端口活着的 Chrome（cdp_cookie.py --wait 起的那个）。
如果浏览器关了，自动刷新会失败，只能手动重新登录。

所以设计为：
    · 浏览器在线 → 自动刷新（免密）
    · 浏览器不在 → 提示手动登录
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

BASE = "https://eportal.hnu.edu.cn"
COOKIE_FILE = HERE / "cookie.txt"


def _browser_available(port: int = 9222) -> bool:
    try:
        import cdp_cookie as cdp
        return bool(cdp.list_targets(port))
    except Exception:
        return False


def refresh_cookie(port: int = 9222, verbose: bool = True,
                   timeout: float = 25.0) -> str | None:
    """在浏览器里重走一次 SSO，返回新 cookie 字符串。

    不需要密码 —— CAS 主票据还在，服务端会自动签发新会话。
    """
    if not _browser_available(port):
        if verbose:
            print("    [!] 调试浏览器未运行，无法自动刷新")
            print("        请运行： python cdp_cookie.py --wait")
        return None

    try:
        import cdp_cookie as cdp
        tg = [t for t in cdp.list_targets(port) if t.get("type") == "page"]
        if not tg:
            if verbose:
                print("    [!] 浏览器没有可用页面")
            return None

        ws = cdp.WS(tg[0]["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        ws.call("Network.enable")
    except Exception as exc:
        if verbose:
            print(f"    [!] 连不上 CDP: {exc}")
        return None

    if verbose:
        print("    正在重走 SSO（免密）...")

    # 访问 eportal 首页，触发 CAS 签发新会话
    ws.call("Page.navigate", {"url": f"{BASE}/v2/site/index"})
    time.sleep(3.5)

    # 等页面稳定（最多 timeout 秒）
    t0 = time.time()
    last_url = ""
    while time.time() - t0 < timeout:
        try:
            st = ws.call("Runtime.evaluate", {
                "expression": "location.href", "returnByValue": True},
                timeout=8)
            cur = st.get("result", {}).get("value", "")
        except Exception:
            cur = last_url
        if cur and cur == last_url and "cas/login" not in cur:
            break
        last_url = cur
        time.sleep(0.8)

    if verbose:
        print(f"    当前页面: {last_url[:80]}")

    if "cas/login" in last_url and "ticket" not in last_url:
        if verbose:
            print("    [!] 跳到了 CAS 登录页 —— 主票据也过期了，需要手动登录")
        ws.close()
        return None

    # 读新 cookie
    try:
        res = ws.call("Network.getAllCookies")
        cookies = res.get("cookies", [])
    except Exception as exc:
        if verbose:
            print(f"    [!] 读 cookie 失败: {exc}")
        ws.close()
        return None

    ws.close()

    hnu = [c for c in cookies if "hnu.edu.cn" in c.get("domain", "")]
    if not hnu:
        if verbose:
            print("    [!] 没读到 hnu 的 cookie")
        return None

    # 必须有 eportal 域的会话票据
    ep = {c["name"] for c in hnu
          if "eportal.hnu.edu.cn" in c.get("domain", "")}
    if "vjuid" not in ep:
        if verbose:
            print(f"    [!] eportal 会话没建立（票据: {sorted(ep)}）")
        return None

    # 按优先级排序，拼成 header
    priority = ("SESSION", "JSESSIONID", "route", "CASTGC",
                "atrust_token", "iPlanetDirectoryPro", "vjuid",
                "vjvd", "vt", "cas_ticket", "PHPSESSID")
    hnu.sort(key=lambda c: (
        priority.index(c["name"]) if c["name"] in priority else 99,
        c.get("domain", ""), c["name"]))
    header = "; ".join(f"{c['name']}={c['value']}" for c in hnu)

    COOKIE_FILE.write_text(header, encoding="utf-8")
    if verbose:
        print(f"    [✓] Cookie 已刷新（{len(header)} 字符，"
              f"{len(hnu)} 个字段）")
        print(f"        eportal 票据: {', '.join(sorted(ep))}")
    return header


def ensure_cookie(cookie: str, port: int = 9222,
                  verbose: bool = True) -> str:
    """检查 cookie 有效性，失效则自动刷新。返回可用的 cookie。"""
    from auto_grab import _req

    r = _req("/site/user/get-name", cookie=cookie)
    if r.get("e") in (0, "0"):
        return cookie

    msg = str(r.get("m", ""))
    expired = (r.get("e") == "10042" or "登录" in msg
               or "10042" in str(r.get("e")))
    if not expired:
        return cookie          # 不是登录问题，原样返回

    if verbose:
        print(f"[!] Cookie 失效（e={r.get('e')} {msg}），尝试自动刷新...")

    new = refresh_cookie(port=port, verbose=verbose)
    return new if new else cookie


def main() -> int:
    """命令行入口：手动触发刷新。"""
    c = ensure_cookie(COOKIE_FILE.read_text(encoding="utf-8").strip()
                      if COOKIE_FILE.exists() else "")
    if c:
        print(f"当前 cookie 长度 {len(c)}")
        return 0
    print("刷新失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
