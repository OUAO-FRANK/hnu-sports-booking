#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【最终方案】22:00 卡点抢次日场地 —— 全自动，验证码也自动。

流程（全部自动，约 3-5 秒完成）：
    1. 21:59:30 开始，用 HTTP 高频轮询 resource-info-margin
       发现目标时段 status 变成可约、margin>0 就立刻动手
    2. 抓验证码（/site/captcha/click-get），模板匹配出 4 个字的坐标
    3. AES 加密坐标，提交 /site/captcha/click-check 换 captchaVerification
    4. 带 position_data 提交 /site/reservation/launch
    5. 复核

验证码自动识别的把握：
    · 模板匹配用系统字体渲染候选字，多字号多角度
    · 候选位置来自"字模板族平均响应"的局部极大值，天然互斥
    · 用全排列求"字→位置"最优分配，避免多个字抢同一块
    · 分数偏低时自动重试（重新取图）

    python auto_grab.py --venue 57 --slots 18-22 --at 22:00:00
    python auto_grab.py --venue 57 --slots 18-22 --now      # 不等，立刻试
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

from captcha_solver import aes_ecb_encrypt_b64  # noqa: E402
from captcha_v4 import solve as solve_captcha  # noqa: E402
from shape_adapter import parse_margin  # noqa: E402

BASE = "https://eportal.hnu.edu.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
def _req(path: str, params: dict | None = None, form: dict | None = None,
         cookie: str = "", timeout: float = 12.0) -> dict:
    from urllib import error as urlerror
    from urllib import parse, request

    url = f"{BASE}{path}"
    headers = {
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{BASE}/v2/reserve/reserveDetail?id=57",
        "Cookie": cookie,
    }
    data = None
    if form is not None:
        data = parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    elif params:
        url += "?" + parse.urlencode(params)
    req = request.Request(url, data=data, headers=headers,
                          method="POST" if data else "GET")
    try:
        with request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urlerror.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return {"e": exc.code, "m": "HTTPError"}
    except Exception as exc:
        return {"e": "EXC", "m": str(exc)}


def load_cookie() -> str:
    p = HERE / "cookie.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


# --------------------------------------------------------------------------- #
class RequestError(RuntimeError):
    def __init__(self, response: dict):
        self.code = str(response.get("e", "SHAPE"))
        self.retryable = self.code in ("EXC", "408", "429", "500", "502", "503", "504")
        message = ("登录已失效，请运行 python cdp_cookie.py --wait"
                   if self.code == "10042" else str(response.get("m") or "接口响应结构异常"))
        super().__init__(f"e={self.code}: {message}")


def require_success(response: dict) -> None:
    if response.get("e") not in (0, "0"):
        raise RequestError(response)


def fetch_slots(cookie: str, venue: int, date: str) -> list[dict]:
    js = _req("/site/reservation/resource-info-margin",
              {"resource_id": venue, "start_time": date, "end_time": date},
              cookie=cookie)
    require_success(js)
    slots = parse_margin(js)
    if slots is None:
        raise RuntimeError("预约接口结构不认识，停止查询；请检查接口适配")
    return slots


def get_captcha(cookie: str) -> dict | None:
    js = _req("/site/captcha/click-get", cookie=cookie)
    require_success(js)
    return js.get("d")


def solve_points(img_bytes: bytes, words: list[str], idx: int) -> tuple[list, float]:
    png = HERE / "out" / f"auto_{idx}.png"
    png.parent.mkdir(exist_ok=True)
    png.write_bytes(img_bytes)
    res, avg, _pts, _S = solve_captcha(png, words)
    return [[r["x"], r["y"]] for r in res], avg


def verify_captcha(cookie: str, token: str, enc: str) -> dict:
    return _req("/site/captcha/click-check", cookie=cookie, form={
        "data[token]": token,
        "data[pointJson]": enc,
        "data[captchaType]": "clickWord",
    })


def launch(cookie: str, venue: int, date: str, slot: dict,
           verification: str) -> dict:
    data = json.dumps([{
        "date": date,
        "period": selected["period"],
        "sub_resource_id": selected.get("sub") or 0,
    } for selected in slot.get("slots", [slot])], ensure_ascii=False)
    # position_data 必须是 PHP 数组风格的表单名：position_data[captchaVerification]
    # 实测三种写法：
    #   传 JSON 对象字符串   -> Illegal string offset 'captchaVerification'
    #   传裸字符串           -> 同上
    #   position_data[...]   -> e=0 操作成功  ✅
    form = {
        "resource_id": str(venue),
        "code": "",
        "remarks": "",
        "deduct_num": "",
        "data": data,
    }
    if verification:
        form["position_data[captchaVerification]"] = verification
    return _req("/site/reservation/launch", cookie=cookie, form=form)


# --------------------------------------------------------------------------- #
def try_book(cookie: str, venue: int, date: str, slot: dict,
             max_captcha_tries: int = 6) -> bool:
    """对一个 slot 尝试完整预约（含自动过验证码）。"""
    for attempt in range(max_captcha_tries):
        cap = get_captcha(cookie)
        if not cap:
            print("    [!] 取验证码失败")
            return False
        token = cap["token"]
        secret = cap.get("secretKey", "")
        words = cap.get("wordList") or []
        if len(words) != 4:
            print(f"    [!] 词表异常 {words}")
            continue

        img = base64.b64decode(cap["originalImageBase64"].split(",")[-1])
        pts, avg = solve_points(img, words, attempt)
        if len(pts) != 4:
            print(f"    [!] 第{attempt+1}次识别出的点不足: {len(pts)}")
            continue

        pj = json.dumps(pts, separators=(",", ":"))
        enc = aes_ecb_encrypt_b64(pj, secret) if secret else pj
        vr = verify_captcha(cookie, token, enc)
        ok = vr.get("e") in (0, "0")
        print(f"    [验证码 {attempt+1}] {words} -> {pts}  均分{avg:.2f}  "
              f"{'通过' if ok else '失败'} {vr.get('m') if not ok else ''}")
        if not ok:
            continue

        verification = (vr.get("d") or {}).get("captchaVerification") or vr.get("d")
        if isinstance(verification, dict):
            verification = verification.get("captchaVerification", "")
        print(f"    [提交] position_data={str(verification)[:40]}...")
        res = launch(cookie, venue, date, slot, str(verification or ""))
        e = res.get("e")
        print(f"    [结果] e={e!r} m={res.get('m')!r}")
        if e in (0, "0"):
            return True
        # 提交失败（比如被别人抢走），直接放弃这个 slot
        return False
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", type=int, default=57)
    ap.add_argument("--slots", default="18-22")
    ap.add_argument("--date", default="", help="默认自动推算放号对应的日期")
    ap.add_argument("--at", default="22:00:00", help="放号时刻 HH:MM:SS")
    ap.add_argument("--now", action="store_true", help="不等，立刻开始")
    ap.add_argument("--interval", type=float, default=0.3)
    ap.add_argument("--max-wait", type=float, default=3600)
    a = ap.parse_args()

    cookie = load_cookie()
    if not cookie:
        print("没有 cookie.txt，先跑 run.py login")
        return 1

    # 目标日期：22:00 放的是次日号
    if a.date:
        target = a.date
    elif a.now:
        target = datetime.now().strftime("%Y-%m-%d")
    else:
        hh, mm, ss = (int(x) for x in a.at.split(":"))
        now = datetime.now()
        rel = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if rel <= now:
            rel += timedelta(days=1)
        target = (rel + timedelta(days=1)).strftime("%Y-%m-%d")

    ranges = []
    for part in a.slots.split(";"):
        if "-" in part:
            x, y = part.split("-", 1)
            ranges.append((int(x), int(y)))

    print(f"目标 {target}  场馆 {a.venue}  时段 {a.slots}")
    if not a.now:
        hh, mm, ss = (int(x) for x in a.at.split(":"))
        now = datetime.now()
        rel = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if rel <= now:
            rel += timedelta(days=1)
        wait = (rel - now).total_seconds()
        print(f"放号时刻 {rel:%H:%M:%S}  还有 {wait:.0f}s")
        if wait > 60:
            print(f"等待中...（会提前 30 秒开始轮询）")
            time.sleep(wait - 30)

    deadline = time.time() + a.max_wait
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        slots = fetch_slots(cookie, a.venue, target)
        hits = [s for s in slots if any(
            lo <= int(str(s["time"]).split(":")[0]) < hi for lo, hi in ranges)]
        if hits:
            print(f"\n[+] 第{attempt}轮 命中 {len(hits)} 个：")
            for s in hits[:5]:
                print(f"    {s['court']} {s['time']} 余{s['margin']}/{s['total']}")
            for s in hits[:3]:
                print(f"\n[*] 尝试 {s['court']} {s['time']}")
                if try_book(cookie, a.venue, target, s):
                    print(f"\n[✓] 预约成功: {s['court']} {s['time']}")
                    return 0
            print("[!] 本轮全部失败，继续")
        else:
            if attempt % 10 == 1:
                print(f"    [{datetime.now():%H:%M:%S}] 第{attempt}轮 "
                      f"无可用（共{len(slots)}个可约）", flush=True)
        time.sleep(a.interval)

    print("[!] 超时")
    return 1


if __name__ == "__main__":
    sys.exit(main())
