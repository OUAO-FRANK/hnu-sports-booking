#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
预热 + 定时 + 抢场 + 扫码弹窗；登录验证和扫码付款需要人工完成。

=================== 和 main.py 的区别 ===================

    main.py   假定浏览器已经预热好、cookie 还有效
    auto.py   自己处理所有前置条件：

        1. 检查 cookie（失效则尝试自动刷新）
        2. 默认纯 HTTP 支付；--pay-mode browser 时检查浏览器
        3. 加载验证码模型；浏览器模式另做支付页预热
        4. 等到放号时刻
        5. 抢场 → 生成二维码 → 弹窗响铃

=================== 预热为什么重要（实测） ===================

    浏览器已在支付页 → router.push 过去    200ms   ← 组件复用
    浏览器在首页     → router.push 过去   5150ms
    浏览器在首页     → Page.navigate      5987ms

    200ms 来自旧组件复用测试，新预约不能直接复用；不作为当前提速保证。

=================== 用法 ===================

    :: 今晚 22:00 抢明天的号（完全自动）
    python auto.py --venue 57 --slots 18-22 --at 22:00:00

    :: 立刻抢
    python auto.py --venue 84 --slots 0-23 --now

    :: 试试看（不真的抢，只走流程）
    python auto.py --venue 84 --slots 16-22 --now --dry-run
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

from auto_grab import (  # noqa: E402
    fetch_slots, get_captcha, launch, load_cookie, verify_captcha, _req,
    RequestError, require_success,
)
from captcha_engine import CaptchaEngine  # noqa: E402
from captcha_solver import aes_ecb_encrypt_b64  # noqa: E402
from qr_popup import PaymentStopped, show_qr  # noqa: E402
from pure_pay import create_payment as create_http_payment  # noqa: E402
from booking_catalog import get_resource_info, parse_times, validate_selection  # noqa: E402

OUT = HERE / "out"
BASE = "https://eportal.hnu.edu.cn"
PORT = 9222
DEFAULT_TIMES = "20:10-21:10;21:10-22:10"


def group_slots(slots: list[dict], times: list[str]) -> list[dict]:
    """只产生同一块场地所有明确时段均齐全的候选。"""
    courts = {}
    for slot in slots:
        if slot["time"].strip() in times:
            courts.setdefault(slot["court"], {}).setdefault(slot["time"].strip(), []).append(slot)
    groups = []
    for court, by_time in sorted(courts.items()):
        if not all(len(by_time.get(time, [])) == 1 for time in times):
            continue
        selected = [by_time[time][0] for time in times]
        groups.append({"court": court, "time": "; ".join(times), "slots": selected,
                       "price": sum(float(slot.get("price") or 0) for slot in selected),
                       "margin": min(int(slot["margin"]) for slot in selected),
                       "total": min(int(slot["total"]) for slot in selected)})
    return groups


def log_timing(stage: str, started: float, **metrics) -> None:
    """可重定向保存的计时行，不记录 Cookie、验证码或支付链接。"""
    print("[timing] " + json.dumps({
        "stage": stage, "time": datetime.now().isoformat(timespec="milliseconds"),
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        **metrics,
    }, ensure_ascii=False), flush=True)


def reservation_rows(cookie: str) -> list[dict]:
    rows, seen, page = [], set(), 1
    while True:
        response = _req("/site/reservation/my-list", cookie=cookie,
                        params={"data_type": "reservation_list", "p": page, "page_size": 50})
        require_success(response)
        data = response.get("d")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise RuntimeError("预约列表结构不认识，无法核对预约身份")
        batch = data["data"]
        ids = {str(row["id"]) for row in batch}
        if ids & seen:
            raise RuntimeError("预约列表分页重复，无法核对预约身份")
        rows.extend(batch)
        seen.update(ids)
        total = data.get("total")
        if not batch or (total is not None and len(rows) >= int(total)):
            return rows
        if total is None and len(batch) < 50:
            return rows
        page += 1


def reservation_detail(cookie: str, rid) -> dict | None:
    response = _req("/site/reservation/my-list", cookie=cookie, timeout=5,
                    params={"data_type": "reservation_detail", "id": rid})
    require_success(response)
    data = response.get("d")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise RuntimeError("预约详情结构不认识")
    return next((row for row in data["data"] if str(row.get("id")) == str(rid)), None)


def reservation_matches(row: dict, venue: int, target: str, slot: dict) -> bool:
    """使用已只读核实的 resource_id / period_text 字段核对预约。"""
    if str(row.get("resource_id")) != str(venue):
        return False
    periods = row.get("period_text")
    if not isinstance(periods, dict):
        return False
    normalize = lambda value: "".join(str(value).split())
    expected = {(target, normalize(selected["court"]), normalize(selected["time"]))
                for selected in slot.get("slots", [slot])}
    actual = []
    for period in periods.values():
        if not isinstance(period, dict) or not isinstance(period.get("text"), list):
            return False
        actual.extend((period.get("date"), normalize(period.get("sub_name")), normalize(text))
                      for text in period["text"])
    return len(actual) == len(expected) and set(actual) == expected


def find_reservation(cookie: str, response: dict, before: set[str],
                     venue: int, target: str, slot: dict) -> dict | None:
    """优先采用提交返回的 ID；否则只接受唯一新增的待支付预约。"""
    data = response.get("d")
    rid = (data.get("appointment_id") or data.get("reserve_id") or data.get("id")) if isinstance(data, dict) else data
    if not isinstance(rid, (str, int)) or isinstance(rid, bool) or not str(rid).isdigit():
        rid = None
    if rid is not None and str(rid) in before:
        raise RuntimeError("提交返回了已有预约 ID，停止自动支付")
    for attempt in range(3):
        if rid is None:
            candidates = [row for row in reservation_rows(cookie)
                          if str(row["id"]) not in before and str(row.get("status")) in ("6", "7")
                          and reservation_matches(row, venue, target, slot)]
            if len(candidates) > 1:
                raise RuntimeError("发现多个新增预约，无法确定本次预约；请在网页核对")
            candidate_id = candidates[0]["id"] if candidates else None
        else:
            candidate_id = rid
        if candidate_id is not None:
            detail = reservation_detail(cookie, candidate_id)
            if detail and str(detail.get("status")) in ("6", "7"):
                if not reservation_matches(detail, venue, target, slot):
                    raise RuntimeError("预约的场馆、日期或场次未通过核对，停止自动支付")
                return detail
        if attempt < 2:
            time.sleep(0.3)
    return None


def reservation_paid(cookie: str, rid) -> bool:
    detail = reservation_detail(cookie, rid)
    if detail is None:
        raise RuntimeError("查不到本次预约，请在网页核对")
    status = str(detail.get("status"))
    if status == "5":
        raise PaymentStopped(f"预约已结束：{detail.get('remake') or '服务端已释放场次'}")
    if status not in ("6", "7"):
        raise RuntimeError(f"预约状态待核对：{status}")
    return status == "7"


def payment_window(detail: dict, reserved_at: float) -> tuple[float, bool]:
    """优先用服务端剩余秒数；缺失时从提交前估计 90 秒，不能在出码时重置。"""
    try:
        remaining = float(detail.get("wait_time"))
        if 0 <= remaining < float("inf"):
            return time.monotonic() + remaining, False
    except (ValueError, TypeError):
        pass
    return reserved_at + 90, True


# --------------------------------------------------------------------------- #
#  前置条件
# --------------------------------------------------------------------------- #
def ensure_browser(verbose: bool = True) -> bool:
    """确保调试浏览器在运行。"""
    try:
        import cdp_cookie as cdp
        tg = [t for t in cdp.list_targets(PORT) if t.get("type") == "page"]
        if tg:
            if verbose:
                print(f"  ✓ 浏览器在线（{len(tg)} 个页面）")
            return True
    except Exception:
        pass

    if verbose:
        print("  浏览器未运行，正在启动...")

    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.exists():
        alt = Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
        if alt.exists():
            chrome = alt
        else:
            if verbose:
                print("  ✗ 找不到 Chrome")
            return False

    profile = Path.home() / "AppData/Local/hnu_cdp_profile"
    profile.mkdir(parents=True, exist_ok=True)

    subprocess.Popen(
        [str(chrome),
         f"--remote-debugging-port={PORT}",
         f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check",
         f"{BASE}/v2/site/index"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 等端口就绪
    for _ in range(30):
        time.sleep(0.5)
        try:
            import cdp_cookie as cdp2
            tg = [t for t in cdp2.list_targets(PORT) if t.get("type") == "page"]
            if tg:
                if verbose:
                    print("  ✓ 浏览器已启动")
                return True
        except Exception:
            continue

    if verbose:
        print("  ✗ 启动超时")
    return False


def ensure_login(cookie: str, verbose: bool = True) -> str | None:
    """检查 cookie 是否有效，失效则尝试自动刷新。"""
    r = _req("/site/user/get-name", cookie=cookie)
    if r.get("e") in (0, "0"):
        if verbose:
            print("  ✓ Cookie 有效")
        return cookie

    msg = str(r.get("m", ""))
    if verbose:
        print(f"  Cookie 失效: e={r.get('e')} {msg}")

    if not (str(r.get("e")) == "10042" or "登录" in msg):
        if verbose:
            print("  ✗ 登录检查失败，请稍后重试")
        return None

    # 尝试自动刷新（需要 CAS 主票据还在）
    try:
        from cookie_refresh import refresh_cookie
        new = refresh_cookie(PORT, verbose=verbose)
        if new:
            return new
    except Exception as exc:
        if verbose:
            print(f"  自动刷新失败: {exc}")

    if verbose:
        print()
        print("  [!] 需要手动登录一次")
        print("      在浏览器里完成手机验证，然后重跑本脚本")
        print("      或者先运行： python cdp_cookie.py --wait")
    return None


def warmup(rid: int | None, verbose: bool = True) -> bool:
    """预热：导航到支付页，让 Vue 组件和 chunk 进缓存。"""
    import cdp_cookie as cdp

    try:
        tg = [t for t in cdp.list_targets(PORT) if t.get("type") == "page"]
        if not tg:
            return False
        ws = cdp.WS(tg[0]["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        ws.sock.settimeout(30.0)
    except Exception as exc:
        if verbose:
            print(f"  预热失败: {exc}")
        return False

    # 用一个历史 rid 或占位 id 打开支付页
    u = (f"{BASE}/v2/matter/start?id=616&reserve_id={rid}"
         if rid else f"{BASE}/v2/matter/start?id=616")
    if verbose:
        print(f"  预热支付页（{u[-30:]}）...")
    t0 = time.time()
    ws.call("Page.navigate", {"url": u})

    # 等「点击支付」按钮出现（说明组件渲染完成）
    JS_BTN = (
        '(() => {'
        ' var want = String.fromCharCode(0x70B9,0x51FB,0x652F,0x4ED8);'
        ' var bs = document.querySelectorAll("button");'
        ' for (var i = 0; i < bs.length; i++) {'
        '  var t = (bs[i].innerText || "").replace(/\\s/g, "");'
        '  var r = bs[i].getBoundingClientRect();'
        '  if (t === want && r.width > 0) return true;'
        ' }'
        ' return false;'
        '})()'
    )
    ok = False
    while time.time() - t0 < 25:
        time.sleep(0.2)
        try:
            r = ws.call("Runtime.evaluate", {
                "expression": JS_BTN, "returnByValue": True}, timeout=15)
            if r.get("result", {}).get("value"):
                ok = True
                break
        except Exception:
            continue

    ws.close()
    if verbose:
        if ok:
            print(f"  ✓ 预热完成（{time.time()-t0:.1f}s，组件已缓存）")
        else:
            print(f"  ⚠ 预热未完成（{time.time()-t0:.1f}s），可能 id 无效")
    return ok


# --------------------------------------------------------------------------- #
#  抢场
# --------------------------------------------------------------------------- #
def solve_captcha(engine, cookie: str, tries: int = 5):
    from PIL import Image
    for _ in range(tries):
        started = time.perf_counter()
        cap = get_captcha(cookie)
        log_timing("captcha_get", started)
        if not cap:
            continue
        words = cap["wordList"]
        OUT.mkdir(exist_ok=True)
        png = OUT / "cap.png"
        png.write_bytes(base64.b64decode(
            cap["originalImageBase64"].split(",")[-1]))
        started = time.perf_counter()
        with Image.open(png) as image:
            pts, heard = engine.solve(image, words)
        log_timing("captcha_infer", started)
        if len(pts) != 4:
            continue
        pj = json.dumps([{"x": a, "y": b} for a, b in pts],
                        separators=(",", ":"))
        pe = aes_ecb_encrypt_b64(pj, cap["secretKey"])
        started = time.perf_counter()
        vr = verify_captcha(cookie, cap["token"], pe)
        log_timing("captcha_verify", started, code=vr.get("e"))
        if vr.get("e") in (0, "0"):
            return aes_ecb_encrypt_b64(
                f"{cap['token']}---{pj}", cap["secretKey"])
        if str(vr.get("e")) in ("10042", "EXC", "429", "500", "502", "503", "504"):
            require_success(vr)
    return None


# --------------------------------------------------------------------------- #
#  支付
# --------------------------------------------------------------------------- #
JS_FIND_BTN = (
    '(() => {'
    ' var want = String.fromCharCode(0x70B9,0x51FB,0x652F,0x4ED8);'
    ' var bs = document.querySelectorAll("button");'
    ' for (var i = 0; i < bs.length; i++) {'
    '  var e = bs[i];'
    '  var t = (e.innerText || "").replace(/\\s/g, "");'
    '  var r = e.getBoundingClientRect();'
    '  if (t === want && r.width > 0) {'
    '   return {x: Math.round(r.left + r.width/2),'
    '           y: Math.round(r.top + r.height/2)};'
    '  }'
    ' }'
    ' return null;'
    '})()'
)

JS_CLICK_BTN = (
    '(() => {'
    ' var want = String.fromCharCode(0x70B9,0x51FB,0x652F,0x4ED8);'
    ' var bs = document.querySelectorAll("button");'
    ' for (var i = 0; i < bs.length; i++) {'
    '  var e = bs[i];'
    '  var t = (e.innerText || "").replace(/\\s/g, "");'
    '  var r = e.getBoundingClientRect();'
    '  if (t === want && r.width > 0) {'
    '   ["mousedown","mouseup","click"].forEach(function(ty){'
    '    e.dispatchEvent(new MouseEvent(ty, {bubbles:true,'
    '      cancelable:true, view:window, button:0,'
    '      buttons: ty === "mousedown" ? 1 : 0}));'
    '   });'
    '   return true;'
    '  }'
    ' }'
    ' return false;'
    '})()'
)

JS_FIND_QR = (
    '(() => {'
    ' var img = document.querySelector("img.qrcode");'
    ' if (!img) img = document.querySelector(".qrcode-dialog img");'
    ' if (!img) {'
    '  var ims = document.querySelectorAll("img");'
    '  for (var i = 0; i < ims.length; i++) {'
    '   if ((ims[i].src||"").indexOf("get-code") >= 0) { img = ims[i]; break; }'
    '  }'
    ' }'
    ' if (!img) return null;'
    ' var r = img.getBoundingClientRect();'
    ' return {src: img.src, w: Math.round(r.width), h: Math.round(r.height)};'
    '})()'
)

JS_ROUTER_PAY = (
    '(() => {'
    ' var rid = %d;'
    ' var all = document.querySelectorAll("div");'
    ' var vm = null;'
    ' for (var i = 0; i < all.length; i++) {'
    '  if (all[i].__vue__ && all[i].__vue__.$router) { vm = all[i].__vue__; break; }'
    ' }'
    ' if (!vm) return false;'
    ' try {'
    '  vm.$router.push({path: "/matter/start",'
    '                   query: {id: "616", reserve_id: String(rid)}});'
    '  return true;'
    ' } catch (e) { return false; }'
    '})()'
)


def get_pay_content(rid: int, verbose: bool = True) -> str | None:
    import cdp_cookie as cdp

    tg = [t for t in cdp.list_targets(PORT) if t.get("type") == "page"]
    if not tg:
        return None
    ws = cdp.WS(tg[0]["webSocketDebuggerUrl"])
    try:
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        ws.sock.settimeout(20.0)

        def js(expr, timeout=20):
            r = ws.call("Runtime.evaluate", {
                "expression": expr, "returnByValue": True,
                "awaitPromise": True, "userGesture": True}, timeout=timeout)
            if "exceptionDetails" in r:
                return None
            return r.get("result", {}).get("value")

        t0 = time.time()
        # 关键：先跳到一个无关页面，让 Vue 卸载支付组件（否则复用旧组件，
        # 新 rid 的按钮不会出现）。实测：预热留在支付页时会失败。
        try:
            cur = js("location.href") or ""
            if "matter/start" in cur:
                ws.call("Page.navigate",
                        {"url": f"{BASE}/v2/site/index"})
                time.sleep(1.2)
        except Exception:
            pass

        ws.call("Page.navigate", {
            "url": f"{BASE}/v2/matter/start?id=616&reserve_id={rid}"})

        btn = None
        while time.time() - t0 < 30:
            time.sleep(0.12)
            btn = js(JS_FIND_BTN)
            if btn:
                break
        if not btn:
            if verbose:
                print("    [!] 按钮没出现")
            return None
        t_btn = time.time() - t0

        if not js(JS_CLICK_BTN):
            if verbose:
                print("    [!] 点击失败")
            return None

        t1 = time.time()
        while time.time() - t1 < 25:
            time.sleep(0.08)
            qr = js(JS_FIND_QR)
            if qr and qr.get("src"):
                q = parse_qs(urlparse(qr["src"]).query)
                if "content" in q:
                    if verbose:
                        print(f"    按钮 {t_btn:.1f}s + 二维码 "
                              f"{time.time()-t1:.1f}s")
                    return unquote(q["content"][0])
        if verbose:
            print("    [!] 二维码没出来")
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--non-interactive", action="store_true", help="明确跳过菜单，直接按命令参数执行预约")
    ap.add_argument("--venue", type=int, default=57)
    selection = ap.add_mutually_exclusive_group()
    selection.add_argument("--slots", help="开场小时范围，约到一场就停，如 20-22")
    selection.add_argument("--times", help="完整时段，多段连续且同场地，如 20:10-21:10;21:10-22:10")
    ap.add_argument("--court", default="", help="限定场地名称；留空不限")
    ap.add_argument("--date", default="")
    ap.add_argument("--at", default="22:00:00")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--interval", type=float, default=0.3)
    ap.add_argument("--max-wait", type=float, default=1800)
    ap.add_argument("--no-pay", action="store_true")
    ap.add_argument("--no-popup", action="store_true")
    ap.add_argument("--pay-mode", choices=("http", "browser"), default="http")
    ap.add_argument("--dry-run", action="store_true",
                    help="只做前置检查和预热，不抢")
    a = ap.parse_args(argv)
    if a.slots is None and a.times is None:
        a.times = DEFAULT_TIMES
    needs_browser = not a.no_pay and a.pay_mode == "browser"

    # 启动时固定本次任务，前置检查跨过放号时刻也不能改变日期。
    started_at = datetime.now()
    try:
        hh, mm, ss = (int(x) for x in a.at.split(":"))
        release = started_at.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        target = a.date or (started_at if a.now else release + timedelta(days=1)).date().isoformat()
        date.fromisoformat(target)
        requested_times = parse_times(a.times) if a.times else []
        ranges = [tuple(map(int, part.split("-", 1))) for part in a.slots.split(";")] if a.slots else []
        if a.slots and (not ranges or any(not 0 <= lo < hi <= 24 for lo, hi in ranges)):
            raise ValueError("时段应在 0-24 小时内")
        if a.interval < 0.3 or a.max_wait <= 0:
            raise ValueError("interval 至少为 0.3 秒，max-wait 必须大于 0")
    except ValueError as exc:
        ap.error(str(exc))

    print("=" * 66)
    print("  HNU 场馆预约 · 全自动")
    print("=" * 66)
    print(f"  现在 {datetime.now():%Y-%m-%d %H:%M:%S}")
    print()

    # ---- 1. cookie ----
    print("[1/4] 检查登录状态")
    cookie = load_cookie()
    if not cookie:
        print("  ✗ 没有 cookie.txt")
        print("    请先运行： python cdp_cookie.py --wait")
        return 1
    cookie = ensure_login(cookie)
    if not cookie:
        return 1
    try:
        resource = get_resource_info(cookie, a.venue)
        if requested_times:
            validate_selection(resource, requested_times, target, now=started_at)
        verify_required = str(resource.get("is_verify")) == "1"
        print(f"  场馆：{resource['name']}；{'需要' if verify_required else '不需要'}点选验证码")
    except (RuntimeError, ValueError) as exc:
        print(f"[×] 场馆规则检查失败：{exc}")
        return 1

    # ---- 2. 浏览器 ----
    print()
    print(f"[2/4] 支付方式：{'只抢场' if a.no_pay else a.pay_mode}")
    if needs_browser and not ensure_browser():
        return 1

    # ---- 3. 预热 ----
    print()
    print("[3/4] 预热")
    if needs_browser:
        if not warmup(None):
            print("  ⚠ 预热未确认完成，支付时将重新打开页面")
    else:
        print("  跳过浏览器和支付页预热")

    engine = None
    if verify_required:
        print("  载入验证码模型...")
        model_started = time.perf_counter()
        try:
            engine = CaptchaEngine()
        except Exception as exc:
            print(f"[×] 模型加载失败：{exc}")
            print(f"  当前解释器：{sys.executable}；请使用一键抢场.bat 选择已配置依赖的 Python")
            return 1
        log_timing("model_load", model_started)
    else:
        print("  场馆不要求验证码，跳过识别模型")

    if a.dry_run:
        print()
        print(f"[dry-run] 目标 {target}，不执行抢场；预热结果见上方")
        return 0

    print()
    print("[4/4] 抢场")
    print(f"  目标 {target}  场馆 {a.venue}  时段 {a.times or a.slots}")
    if requested_times:
        print("  所选时段全部提交；多段必须同一块场地，不自动减少时段")

    # 等放号
    if not a.now:
        wait = (release - datetime.now()).total_seconds()
        if wait > 30:
            print(f"  放号 {release:%Y-%m-%d %H:%M:%S}，等 {wait/60:.1f} 分钟")
            while (release - datetime.now()).total_seconds() > 30:
                time.sleep(min(30, (release - datetime.now()).total_seconds() - 30))

    # 长时间挂机后重新检查登录，并记录已有预约，避免把历史订单当成本次订单。
    cookie = ensure_login(cookie)
    if not cookie:
        return 1
    try:
        before = {str(row["id"]) for row in reservation_rows(cookie)}
    except RuntimeError as exc:
        print(f"[×] 前置检查失败：{exc}")
        return 1
    if not a.now:
        while (release - datetime.now()).total_seconds() > 0:
            time.sleep(min(0.2, (release - datetime.now()).total_seconds()))
        print(f"  开始执行，本机时间较计划放号晚 {(datetime.now()-release).total_seconds():.3f}s")
    grab_started = time.perf_counter()

    def hour_of(t):
        try:
            return int(str(t).split("-")[0].split(":")[0])
        except (ValueError, IndexError):
            return -1

    def is_started(slot_time: str, target_day: str) -> bool:
        """判断这个时段是否已经开场（开场后约了也打不了）。

        服务端对「已开场但未结束」的场次仍返回可约，是个坑：
        15:10-16:10 在 15:37 时 margin=1，能提交成功，但约了没用。
        """
        today = date.today().isoformat()
        if target_day != today:
            return False        # 未来日期不可能已开场
        try:
            start = str(slot_time).split("-")[0].strip()
            hh, mm = (int(x) for x in start.split(":")[:2])
        except (ValueError, IndexError):
            return False
        now = datetime.now()
        return (hh, mm) <= (now.hour, now.minute)

    deadline = time.monotonic() + a.max_wait
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        query_started = time.perf_counter()
        try:
            slots = fetch_slots(cookie, a.venue, target)
        except RequestError as exc:
            print(f"  [!] 查询失败：{exc}")
            if not exc.retryable:
                return 1
            time.sleep(1.0)
            continue
        except RuntimeError as exc:
            print(f"  [×] 查询已停止：{exc}")
            return 1
        log_timing("query", query_started, attempt=attempt)

        hits = [s for s in slots
                if (s["time"].strip() in requested_times if requested_times
                    else any(lo <= hour_of(s["time"]) < hi for lo, hi in ranges))
                and (not a.court or s["court"] == a.court)
                and not is_started(s["time"], target)]
        if requested_times:
            hits = group_slots(hits, requested_times)
        else:
            hits.sort(key=lambda s: s["time"])

        if not hits:
            # 区分「真的没有」和「只有已开场的」
            started = [s for s in slots
                       if any(lo <= hour_of(s["time"]) < hi
                              for lo, hi in ranges)
                       and is_started(s["time"], target)]
            if attempt % 40 == 1:
                extra = f"（另有 {len(started)} 个已开场，跳过）" if started else ""
                print(f"    [{datetime.now():%H:%M:%S}] 第{attempt}轮 无{extra}",
                      flush=True)
            time.sleep(a.interval)
            continue

        print(f"\n[{datetime.now():%H:%M:%S}] 第{attempt}轮 命中 {len(hits)}")
        for s in hits[:4]:
            print(f"    {s['court']} {s['time']} 余{s['margin']}/{s['total']}")

        booked = None
        reservation = None
        for s in hits:
            if time.monotonic() >= deadline:
                break
            print(f"\n  >>> {s['court']} {s['time']}")
            cap_started = time.perf_counter()
            try:
                ver = solve_captcha(engine, cookie) if verify_required else ""
            except RequestError as exc:
                print(f"  [!] 验证码请求失败：{exc}")
                if not exc.retryable:
                    return 1
                break
            log_timing("captcha", cap_started, verified=bool(ver))
            if verify_required and not ver:
                continue
            if is_started(s["time"], target):
                print("  场次已开场，跳过提交")
                continue
            launch_started = time.perf_counter()
            reserved_at = time.monotonic()
            res = launch(cookie, a.venue, target, s, ver)
            e = res.get("e")
            log_timing("launch", launch_started, code=e)
            print(f"      提交: e={e!r} m={res.get('m')!r} "
                  f"({(time.perf_counter()-launch_started)*1000:.0f}ms)")
            if e not in (0, "0"):
                if len(requested_times) > 1 and str(e) == "2":
                    print("[×] 多时段提交返回冲突，请核对预约记录；不自动支付或重复提交")
                    return 1
                if str(e) in ("EXC", "408", "500", "502", "503", "504"):
                    # POST 超时不代表服务端没受理，先对账，不能盲目重复提交。
                    try:
                        reservation = find_reservation(cookie, {}, before, a.venue, target, s)
                    except RuntimeError as exc:
                        print(f"[×] 提交结果不明：{exc}；请在网页核对")
                        return 1
                    if reservation is None:
                        print("[×] 提交结果不明，停止重复提交；请在网页核对预约记录")
                        return 1
                elif str(e) == "10042" or any(text in str(res.get("m", ""))
                        for text in ("登录", "未结束", "无预约权限", "次数上限", "预约次数已")):
                    print("[×] 无法继续预约，请处理上述原因后重试")
                    return 1
                else:
                    # 未放号时刷新；单场拒绝则尝试下一场，不永久封禁。
                    if "未达到" in str(res.get("m", "")):
                        break
                    continue
            booked = s
            break

        if not booked:
            time.sleep(a.interval)
            continue

        print(f"\n{'='*66}")
        print(f"  已提交 {booked['court']} {booked['time']}，正在核对预约")
        print("=" * 66)

        log_timing("occupied", grab_started,
                   release_elapsed_ms=round((datetime.now()-release).total_seconds()*1000, 1)
                   if not a.now else None)
        try:
            reservation = reservation or find_reservation(cookie, res, before, a.venue, target, booked)
        except RuntimeError as exc:
            print(f"[×] 无法核对预约：{exc}；请在网页查看预约记录")
            return 1
        if not reservation:
            print("[!] 已提交但无法确认本次预约 ID；请在网页核对，不再重复抢场")
            return 1
        rid = reservation["id"]
        print(f"  reserve_id = {rid}")

        if str(reservation.get("status")) == "7":
            print("[✓] 服务端已确认预约生效")
            return 0

        if reservation.get("pay_money") is not None and Decimal(str(reservation["pay_money"])) == 0:
            print("[!] 免费预约尚未确认生效，请在网页核对；不创建支付订单或重复预约")
            return 1

        if a.no_pay:
            print(f"\n  手动支付： {BASE}/v2/matter/start?id=616&reserve_id={rid}")
            return 0

        payment_deadline, estimated = payment_window(reservation, reserved_at)
        print("\n  生成支付二维码...")
        pay_started = time.perf_counter()
        try:
            if a.pay_mode == "http":
                payment = create_http_payment(cookie, rid)
                content = payment["content"]
                payment_deadline, estimated = payment["deadline"], payment["estimated"]
                booked["price"] = payment["amount"]
            else:
                content = get_pay_content(rid)
        except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
            print(f"  [!] {a.pay_mode} 支付失败：{exc}；请核对已有订单，不自动重复创建")
            content = None
        if not content:
            print("  [!] 拿不到二维码")
            print(f"  手动打开： {BASE}/v2/matter/start?id=616&reserve_id={rid}")
            return 1
        log_timing("qr_ready", pay_started)

        try:
            latest = reservation_detail(cookie, rid) if a.pay_mode == "browser" else None
            if latest:
                reservation = latest
                # 仅用这次新响应计算期限；查询失败时不能重新使用旧剩余秒数。
                if latest.get("wait_time") is not None:
                    payment_deadline, estimated = payment_window(latest, reserved_at)
            if str(reservation.get("status")) == "7":
                log_timing("confirmed", grab_started)
                print("[✓] 服务端已确认预约生效")
                return 0
            if str(reservation.get("status")) == "5":
                print(f"[×] 预约已结束：{reservation.get('remake')}")
                return 1
        except RuntimeError as exc:
            # 保留二维码供人工处理，窗口会继续查询，不把查询失败当成未付款。
            print(f"  [!] 支付前状态查询失败：{exc}")

        print(f"  {content}")
        OUT.mkdir(exist_ok=True)
        (OUT / "pay_url.txt").write_text(content, encoding="utf-8")
        try:
            import segno
            segno.make(content, error="m").save(
                str(OUT / "pay_qr.png"), scale=14, border=3, kind="png")
        except Exception as exc:
            print(f"[×] 二维码图片生成失败：{exc}")
            print(f"  请打开上方支付链接，或在网页查看预约 {rid}")
            return 1

        def check_paid() -> bool:
            return reservation_paid(cookie, rid)

        if a.no_popup:
            print("\n" + "=" * 66)
            print(f"  {content}")
            print(f"  {OUT / 'pay_qr.png'}")
            print("=" * 66)
            for _ in range(10):
                print("\a", end="", flush=True)
                time.sleep(0.15)
            while time.monotonic() < payment_deadline:
                try:
                    if check_paid():
                        log_timing("confirmed", grab_started)
                        print("\n[✓] 服务端确认预约生效")
                        return 0
                except PaymentStopped as exc:
                    print(f"\n[×] {exc}")
                    return 1
                except RuntimeError as exc:
                    print(f"  [!] 状态查询失败：{exc}")
                time.sleep(min(4, max(0, payment_deadline - time.monotonic())))
            print("[!] 未确认预约生效；若已扣款，请在网页核对")
            return 1

        title = f"{booked['court']} {booked['time']}"
        if booked.get("price"):
            title += f"  ¥{booked['price']}"
        paid = show_qr(content, title=title,
                       on_poll=check_paid, png_path=OUT / "pay_qr.png",
                       poll_interval=4.0, deadline=payment_deadline, estimated=estimated)
        if paid:
            log_timing("confirmed", grab_started)
        print("\n[✓] 服务端确认预约生效" if paid else "\n[!] 未确认预约生效；若已扣款，请在网页核对")
        return 0 if paid else 1

    print("[×] 超时没抢到")
    return 1


def cli_main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(flag in args for flag in ("--non-interactive", "--dry-run", "--help", "-h")):
        return main(args)
    from booking_menu import main as menu_main
    return menu_main()


if __name__ == "__main__":
    sys.exit(cli_main())
