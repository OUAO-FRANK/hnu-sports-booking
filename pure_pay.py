#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立 HTTP 支付验证入口。默认只读；--create-order 才创建未付款订单。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Thread
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

from auto_grab import _req, load_cookie, require_success  # noqa: E402
from run_diagnostics import record

APP_ID = 616
COMPONENT = "PaymentOrder_11"


class PaymentPreparationError(RuntimeError):
    """This invocation failed before sending any create-order request."""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _get(cookie: str, path: str, params: dict, *, timeout=5) -> dict:
    response = _req(path, cookie=cookie, params=params, timeout=timeout)
    require_success(response)
    return response["d"]


def _rows(cookie: str, path: str, **params) -> list[dict]:
    rows, page, seen = [], 1, set()
    while True:
        data = _get(cookie, path, {**params, "p": page, "page_size": 50})
        batch = data.get("data")
        if not isinstance(batch, list):
            raise RuntimeError("列表响应结构不认识")
        ids = {str(row["id"]) for row in batch}
        if ids & seen:
            raise RuntimeError("分页重复，停止核对")
        rows.extend(batch)
        seen.update(ids)
        total = data.get("total")
        if not batch or (total is not None and len(rows) >= int(total)):
            return rows
        if total is None and len(batch) < 50:
            return rows
        page += 1


def _detail(cookie: str, rid: int) -> dict:
    data = _get(cookie, "/site/reservation/my-list", {"data_type": "reservation_detail", "id": rid})
    return next((row for row in data.get("data", []) if str(row.get("id")) == str(rid)), {})


def _amount(value) -> Decimal:
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount > 0 and amount == amount.quantize(Decimal("0.01")):
            return amount
    except InvalidOperation:
        pass
    raise RuntimeError("服务端预约金额无效，不创建订单")


def _period(row: dict, now: datetime) -> tuple[str, str, str, tuple]:
    if str(row.get("status")) != "6":
        raise RuntimeError("预约不是有效待支付状态，不创建订单")
    periods = row.get("period_text")
    if not isinstance(periods, dict) or not periods:
        raise RuntimeError("缺少明确的预约场次")
    intervals = []
    for period in periods.values():
        if not isinstance(period, dict) or not isinstance(period.get("text"), list) or not period["text"]:
            raise RuntimeError("缺少明确的预约时段")
        day, court = period["date"], period["sub_name"].strip()
        for text in period["text"]:
            slot = text.strip()
            start, end = (datetime.fromisoformat(day + "T" + clock) for clock in slot.split("-"))
            if start <= now:
                raise RuntimeError("预约场次已开场，不创建订单")
            if end <= start:
                raise RuntimeError("预约时段无效")
            intervals.append((start, end, day, court, slot))
    intervals.sort(key=lambda interval: interval[0])
    day = intervals[0][2]
    if any(interval[2] != day for interval in intervals):
        raise RuntimeError("多段预约必须在同一天")
    if any(previous[1] != current[0] for previous, current in zip(intervals, intervals[1:])):
        raise RuntimeError("多段预约时间不连续")
    courts = [interval[3] for interval in intervals]
    court = courts[0] if len(set(courts)) == 1 else " → ".join(courts)
    identity = tuple((interval[2], interval[3], interval[4]) for interval in intervals)
    return day, court, "; ".join(interval[4] for interval in intervals), identity


def _component_config(plugins, key: str) -> dict:
    while isinstance(plugins, str):
        plugins = json.loads(plugins)
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("key") == key:
                found.append(node["attr"]["data"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(plugins)
    if len(found) != 1:
        raise RuntimeError(f"组件 {key} 不唯一或缺失")
    return found[0]


def _payment_config(plugins) -> dict:
    config = _component_config(plugins, COMPONENT)
    expected = {"inst_id": "$EVN.url_inst id", "reserve_id": "$EVN.url_reserve_id"}
    actual = {item["name"]: item["component_key"] for item in config.get("custom_params", [])}
    if (str(config.get("pay_type")) != "2" or str(config.get("url_type")) != "0"
            or str(config.get("motion_simulation")) != "1" or actual != expected):
        raise RuntimeError("支付组件配置已变化，停止使用当前实现")
    return config


def _canonical_labels(cookie: str, reservation: dict, form: dict, *, deadline=None) -> tuple[str, str]:
    """双时段文案沿用前端的只读数据源，避免猜测合并格式。"""
    source = _component_config(form["plugins"], "DataSource_12")
    mapping = source.get("mapConfig") or {}
    if (str(source.get("type")) != "1" or source.get("sourceConfig")
            or mapping.get("Input_7", {}).get("key") != "time"
            or mapping.get("Input_14", {}).get("key") != "name"
            or mapping.get("Numeric_6", {}).get("key") != "pay_money"
            or [(item.get("key"), item.get("type"), item.get("value"))
                for item in source.get("conditions", [])] != [("id", 0, "$EVN.url_reserve_id")]):
        raise RuntimeError("预约支付数据源配置已变化")
    payload = {
        "id": source["sourceid"], "inst_id": 0, "app_id": APP_ID,
        "form_version_id": form["form_version_id"], "component": "DataSource_12",
        "params[formId]": form["id"], "params[pluginKey]": "DataSource_12",
        "params[id]": reservation["id"],
    }
    read_deadline = time.monotonic() + 10
    if deadline is not None:
        read_deadline = min(read_deadline, deadline)
    for attempt in range(2):
        remaining = read_deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("支付资料读取预算已用完")
        started = time.perf_counter()
        response = _req("/site/data-source/detail", cookie=cookie, form=payload, timeout=min(5, remaining))
        code = str(response.get("e"))
        record("timing", stage="payment_labels_read", attempt=attempt + 1,
               code=code if code in ("0", "EXC", "408", "429", "500", "502", "503", "504", "10042", "10043") else "OTHER",
               duration_ms=round((time.perf_counter() - started) * 1000, 1))
        transient = (code in ("408", "500", "502", "503", "504")
                     or (code == "EXC" and str(response.get("m")) in (
                         "ReadTimeoutError", "ConnectTimeoutError", "NewConnectionError",
                         "ProxyError", "ProtocolError", "TimeoutError", "ConnectionResetError")))
        if (attempt == 0 and transient
                and read_deadline - time.monotonic() > .3):
            time.sleep(.3)
            continue
        require_success(response)
        if time.monotonic() >= read_deadline:
            raise RuntimeError("支付资料读取预算已用完")
        break
    data = (response.get("d") or {}).get("list") or {}
    if (str(data.get("id")) != str(reservation["id"])
            or str(data.get("resource_id")) != str(reservation["resource_id"])
            or _amount(data.get("pay_money")) != _amount(reservation.get("pay_money"))
            or not isinstance(data.get("time"), str) or not isinstance(data.get("name"), str)):
        raise RuntimeError("预约支付数据源的身份或金额不匹配")
    return data["time"], data["name"]


def _read_config(cookie: str, sess_id, process=None, *, deadline=None) -> dict:
    def read(path, params):
        timeout = min(5, deadline - time.perf_counter()) if deadline is not None else 5
        if timeout <= 0:
            raise RuntimeError("支付配置预读已过期")
        return _get(cookie, path, params, timeout=timeout)
    if process is None:
        process = read("/site/process/start-info", {"app_id": APP_ID})
    forms = process.get("form") or []
    if len(forms) != 1:
        raise RuntimeError("支付流程不再是单表单，停止使用当前实现")
    form_id, bpmn_id = forms[0]["form_id"], process["bpmn_id"]
    form = read("/site/form/get-formv", {"id": form_id, "bpmn_id": bpmn_id,
                "sess_id": sess_id, "report_id": 0})
    if str(form.get("app_id")) != str(APP_ID) or str(form.get("id")) != str(form_id):
        raise RuntimeError("表单归属不匹配")
    config = _payment_config(form.get("plugins"))
    return {"form": form, "form_id": form_id, "bpmn_id": bpmn_id, "config": config}


class PaymentConfigPrefetch:
    """两次只读配置请求；绑定登录、sess 0，60 秒内仅使用一次，不等待后台。"""
    def __init__(self, cookie: str, *, clock=None):
        self._clock = clock or time.perf_counter
        self._cookie = cookie
        self._taken = False
        self._completed = Future()

        def run():
            try:
                config = _read_config(cookie, 0, deadline=time.perf_counter() + 8)
                self._completed.set_result((config, self._clock()))
            except Exception as exc:
                self._completed.set_exception(exc)
        Thread(target=run, daemon=True, name="payment-config").start()

    def take(self, cookie: str, sess_id):
        if self._taken:
            return None
        self._taken = True
        original_cookie, self._cookie = self._cookie, None
        if cookie != original_cookie or str(sess_id) != "0" or not self._completed.done():
            return None
        try:
            config, ready_at = self._completed.result()
        except Exception as exc:
            record("timing", stage="payment_config_prefetch", error_type=type(exc).__name__)
            return None
        if not 0 <= self._clock() - ready_at <= 60:
            return None
        record("timing", stage="payment_config_prefetch", code="READY")
        return config


def prepare_payment(cookie: str, rid: int, *, now: datetime | None = None,
                    reservation: dict | None = None, preparation=None, preparation_deadline=None) -> dict:
    """只读核对有效预约与现行表单，不创建订单。"""
    now = now or datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    process = None
    if reservation is None:
        if preparation is None:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="payment-read") as reads:
                detail_read = reads.submit(_detail, cookie, rid)
                process_read = reads.submit(_get, cookie, "/site/process/start-info", {"app_id": APP_ID})
                reservation, process = detail_read.result(), process_read.result()
        else:
            reservation = _detail(cookie, rid)
    if str(reservation.get("id")) != str(rid):
        raise RuntimeError("预约回执 ID 不匹配，不创建订单")
    day, court, slot, periods = _period(reservation, now)
    amount = _amount(reservation.get("pay_money"))
    sess_id = reservation.get("sess_id", 0)
    prepared = preparation.take(cookie, sess_id) if preparation is not None else None
    prepared = prepared or _read_config(cookie, sess_id, process)
    form, form_id = prepared["form"], prepared["form_id"]
    bpmn_id, config = prepared["bpmn_id"], prepared["config"]
    label, name = (f"{court}, {day} {slot}", f"{day} {slot}")
    if "; " in slot or any((datetime.fromisoformat(day + "T" + text.strip().split("-")[1]) -
            datetime.fromisoformat(day + "T" + text.strip().split("-")[0])).total_seconds() != 3600
            for period in reservation["period_text"].values() for text in period["text"]):
        label, name = _canonical_labels(cookie, reservation, form, deadline=preparation_deadline)
    return {"rid": rid, "venue": reservation["resource_id"], "day": day,
            "court": court, "slot": slot, "periods": periods, "amount": f"{amount:.2f}",
            "form_id": form_id, "bpmn_id": bpmn_id, "config": config,
            "payload": {"app_id": str(APP_ID), "node_id": "", "form_data": {str(form_id): {
                "Input_7": label, "Input_14": name}}}}


def create_payment(cookie: str, rid: int, *, now: datetime | None = None,
                   reservation: dict | None = None, preparation=None, preparation_deadline=None) -> dict:
    """仅创建一次未付款订单；所有校验通过才返回支付链接。"""
    started = time.perf_counter()
    try:
        plan = prepare_payment(cookie, rid, now=now, reservation=reservation, preparation=preparation,
                               preparation_deadline=preparation_deadline)
        if preparation_deadline is not None and time.monotonic() >= preparation_deadline:
            raise RuntimeError("本次支付准备期限已过")
    except RuntimeError as exc:
        raise PaymentPreparationError(f"{exc}；本次尚未发送创建订单请求") from exc
    orders = _rows(cookie, "/site/payment-order/my-list")
    if any(str(row.get("appointment_id")) == str(rid) for row in orders):
        raise RuntimeError("该预约已有支付订单，不重复创建；请先在网页核对")
    check = _req("/site/apps/launch", cookie=cookie, form={
        "data": _json(plan["payload"]), "reserve_id": str(rid), "check_launch": 1,
        "not_require": _json({"formId": plan["form_id"], "key": COMPONENT}),
    })
    if str(check.get("e")) != "99":
        raise RuntimeError(f"支付前校验未进入支付分支：e={check.get('e')} {check.get('m')}")
    latest = _detail(cookie, rid)
    latest_now = now or datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    if (_period(latest, latest_now) != (plan["day"], plan["court"], plan["slot"], plan["periods"])
            or str(latest.get("resource_id")) != str(plan["venue"])
            or _amount(latest.get("pay_money")) != Decimal(plan["amount"])):
        raise RuntimeError("支付前预约身份或金额发生变化")
    try:
        remaining = float(latest["wait_time"])
        if not 0 <= remaining < float("inf"):
            raise ValueError("invalid remaining time")
        estimated = False
    except (KeyError, TypeError, ValueError):
        try:
            created_at = datetime.fromisoformat(latest["create_time"])
            remaining = 90 - (latest_now - created_at).total_seconds()
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("缺少可核对的支付剩余时间，不创建订单") from exc
        estimated = True
    if preparation_deadline is not None:
        remaining = min(remaining, preparation_deadline - time.monotonic())
    if remaining <= 0:
        raise RuntimeError("预计支付窗口已过，请在网页核对预约状态")
    deadline = time.monotonic() + remaining
    config = plan["config"]
    params = {item["name"]: {"value": str(rid) if item["name"] == "reserve_id" else "",
        "component_key": item["component_key"], "is_show": item.get("checked", False)}
        for item in config["custom_params"]}
    response = _req("/site/payment-order/create-order", cookie=cookie, form={
        "component_key": COMPONENT, "app_id": APP_ID, "bpmn_id": plan["bpmn_id"],
        "goods_id": config["goods_id"], "pay_money": plan["amount"], "params": _json(params),
        "form_id": plan["form_id"], "url": "", "notify_action": config["motion_simulation"],
        "url_type": config["url_type"], "appointment_id": str(rid),
    })
    # 不重试写请求。超时或 5xx 不能推断服务端未受理。
    require_success(response)
    order = response.get("d") or {}
    oid = order.get("order_id")
    if not oid:
        raise RuntimeError("创建结果缺少订单 ID，停止重复提交；请在网页核对")
    print(f"创建订单 id={oid}，正在核对，尚未确认可支付", flush=True)
    orders = _rows(cookie, "/site/payment-order/my-list")
    actual = next((row for row in orders if str(row.get("id")) == str(oid)), {})
    if (str(actual.get("appointment_id")) != str(rid)
            or Decimal(str(actual.get("pay_money", 0))) != Decimal(plan["amount"]) * 100):
        raise RuntimeError(f"订单 {oid} 的预约关联或金额未通过核对，不展示二维码")
    plan["payload"]["form_data"][str(plan["form_id"])][COMPONENT] = {
        "status": 0, "id": oid, "goods_id": config["goods_id"],
        "pay_money": float(plan["amount"]), "params": _json({"reserve_id": str(rid)})}
    writeback = _req("/site/payment-order/set-form-data", cookie=cookie,
                     form={"id": oid, "data": _json(plan["payload"])})
    require_success(writeback)
    result = _req("/site/payment-order/get-pay-result", cookie=cookie, params={"id": oid})
    if str(result.get("e")) not in ("0", "3"):
        raise RuntimeError(f"订单 {oid} 支付状态异常：{result.get('m')}")
    query = parse_qs(urlparse(order.get("code_url", "")).query)
    contents = query.get("content", [])
    content = contents[0] if len(contents) == 1 else ""
    url = urlparse(content)
    if url.scheme not in ("http", "https") or url.hostname != "wxpay.hnu.edu.cn":
        raise RuntimeError(f"订单 {oid} 的二维码链接结构异常")
    return {**plan, "order_id": oid, "content": content,
            "deadline": deadline, "estimated": estimated,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reserve-id", type=int)
    parser.add_argument("--create-order", action="store_true", help="创建真实未付款订单并展示二维码")
    args = parser.parse_args()
    c = load_cookie()
    if not c:
        print("没有 cookie")
        return 1

    try:
        if args.reserve_id is None:
            if args.create_order:
                parser.error("创建订单必须指定 --reserve-id")
            rows = _rows(c, "/site/reservation/my-list", data_type="reservation_list")
            pending = [row for row in rows if str(row.get("status")) == "6"]
            print("待支付预约：", [{key: row.get(key) for key in ("id", "resource_name", "pay_money", "period_text")} for row in pending])
            return 0
        if not args.create_order:
            plan = prepare_payment(c, args.reserve_id)
            print("只读核对：", {key: plan[key] for key in ("rid", "venue", "day", "court", "slot", "amount")})
            return 0
        result = create_payment(c, args.reserve_id)
        print(f"订单 {result['order_id']} 关联和支付状态检查通过，用时 {result['duration_ms']}ms；等待人工扫码")
        from qr_popup import show_qr
        from auto import reservation_paid
        paid = show_qr(result["content"], title=f"{result['day']} {result['court']} {result['slot']} ¥{result['amount']}",
                       on_poll=lambda: reservation_paid(c, args.reserve_id, order_id=result["order_id"]),
                       deadline=result["deadline"], estimated=result["estimated"])
        print("服务端确认预约生效" if paid else "未确认预约生效，请在网页核对")
        return 0 if paid else 1
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"[×] {exc}；停止重复提交，请核对预约和订单记录")
        return 1


if __name__ == "__main__":
    sys.exit(main())
