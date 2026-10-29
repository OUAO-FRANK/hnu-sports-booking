#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shape_adapter —— eportal 场馆预约返回结构适配层

所有语义都来自实测 + 前端 bundle 逆向，不是猜的。

【实测响应】（2026-10-28，venue=57，date=2026-10-29，已登录）
    d 的键是资源资源池 id（如 "851"），**不是日期**；值为 112 个单元格。
    单元格样例：
        {"time_id": 10420, "sub_id": 62719, "abscissa": "1号场",
         "yaxis": "08:00-09:00", "is_sub": 1, "lock_model": 2,
         "local_time": "00:24:05", "date": "2026-10-29",
         "row": {"status": 5, "time": "2026-10-29", "total": 1,
                 "margin": 1, "info": [], "price": 20, "is_sub": 1, "data": []}}
    当天 status 分布：{5: 76, 4: 36} —— 没有 1/2/3。

【status 语义】（来自 chunk-c7b441f0 的分支骨架）
    0  过期        class=due，无 click
    1  可约        走 setReserve(key, setNum, "one")
    2  可约(子场)   canReserve:1 + canReserveChild，click=setReserve(childKey, true)
    3  约满        class=full + fullActive，仍可点（走 setFull 包场）
    4  失效        class=disable + disableActive，click=setReserve(key,"stop")
    5  可约/部分占用 class 含 canReserve:1，click=setReserve(key, setNum, "one")

    注意 5 和 1 的可点击性完全一样，差别只在渲染时多显示一个 infoPop
    （显示"已占 x/y"的浮层）。所以 **1 和 5 都是可约的**。

【真正的可约判据】（比 status 更可靠）
    前端渲染 `el-icon-unlock`（未锁）的条件是：
        row.total != row.margin
    即"总名额 != 剩余名额" —— margin 是剩余，total 是总数。
    margin > 0 才有余位；status 4（失效）时 margin 恒为 0。

    综合判定：
        可约 = status in (1, 2, 3, 5) AND margin > 0
        （status 3 是"约满可包场"，普通单场预约不该抢，默认排除）

【提交 payload】（来自 chunk-be985c8e 的 n.push 原文）
    {date: "YYYY-MM-DD", period: <time_id>, sub_resource_id: <sub_id 或 0>}
"""

from __future__ import annotations

from typing import Any

# --- status 常量（逆向所得）------------------------------------------------
STATUS_EXPIRED = 0      # 过期
STATUS_FREE = 1         # 可约
STATUS_FREE_SUB = 2     # 可约（子场/团队子资源）
STATUS_FULL = 3         # 约满（可包场）
STATUS_INVALID = 4      # 失效（不可点）
STATUS_OPEN = 5         # 可约（可能部分已占，margin 告诉还剩几个）

#: 能下单的 status。注意 5 在内！
BOOKABLE_STATUS = {STATUS_FREE, STATUS_FREE_SUB, STATUS_OPEN}
#: 默认也允许 3（约满但可包场）—— 只有显式打开才会去抢
ALLOW_FULL = False

# 兼容字符串/布尔写法
FREE_STRINGS = {"1", "2", "5", "true", "yes", "available", "free", "可约", "空闲", "normal", "idle"}
BUSY_STRINGS = {"0", "4", "false", "no", "booked", "invalid", "occupied",
                "已约", "约满", "占用", "过期", "失效", "disabled"}

# 字段别名表（真实字段名在前，兼容别名在后）
ALIASES = {
    "period": ("time_id", "periodId", "period_id", "period", "timeId"),
    "sub": ("sub_id", "subResourceId", "sub_resource_id", "subResourceID",
            "resourceSubId", "sub", "subId", "siteId"),
    "court": ("abscissa", "courtName", "court", "resourceName", "siteName"),
    "time": ("yaxis", "timeRange", "time_range", "time", "periodName",
             "iso_time", "isoTime", "startTime", "start_time", "timeStr"),
    "date": ("date", "day", "book_date"),
    "total": ("total", "total_num", "totalNum", "capacity"),
    "margin": ("margin", "remain", "remaining", "left", "surplus"),
    "price": ("price", "fee", "amount"),
}
STATUS_KEYS = ("status", "state", "statusCode", "bookStatus", "reserveStatus")


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _first_deep(cell: dict, keys: tuple[str, ...]) -> Any:
    """先找 cell 顶层，再找 cell.row（total/margin/price 实际挂在 row 上）。"""
    v = _first(cell, keys)
    if v is not None:
        return v
    row = cell.get("row")
    if isinstance(row, dict):
        return _first(row, keys)
    return None


def _status_of(cell: dict) -> Any:
    row = cell.get("row")
    if isinstance(row, dict):
        v = _first(row, STATUS_KEYS)
        if v is not None:
            return v
    v = _first(cell, STATUS_KEYS)
    if v is not None:
        return v
    for k in ("available", "isFree", "canBook", "free"):
        if isinstance(cell.get(k), bool):
            return cell[k]
    return None


def _to_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _is_bookable(status: Any, margin: Any = None) -> bool:
    """按实测语义判定能否下单。

    优先级：margin > 0 是最硬的证据；status 决定类别。
    """
    st = _to_int(status)
    if st is None:
        if isinstance(status, str):
            s = status.strip().lower()
            if s in FREE_STRINGS:
                return True
            if s in BUSY_STRINGS:
                return False
        if isinstance(status, bool):
            return status
        return False

    if st == STATUS_INVALID:
        return False
    if st == STATUS_EXPIRED:
        return False

    allowed = set(BOOKABLE_STATUS)
    if ALLOW_FULL:
        allowed.add(STATUS_FULL)
    if st not in allowed:
        return False

    # 有 margin 信息时，剩余必须 > 0
    m = _to_int(margin)
    if m is not None and m <= 0:
        return False
    return True


def unwrap(payload: Any) -> tuple[bool, Any]:
    """剥外层。

    实测：{"e": 0, "m": "操作成功", "d": {"851": [cell, ...]}}
    未登录：{"e": "10042", "m": "该操作需要登录，请登录后重试", "d": []}
    e 可能是 int 也可能是字符串。
    """
    if isinstance(payload, list):
        return True, payload
    if not isinstance(payload, dict):
        return False, None
    if "e" in payload:
        e = payload.get("e")
        ok = e in (0, "0") or str(e).strip() == "0"
        return ok, payload.get("d")
    code = payload.get("code", payload.get("status"))
    ok = code in (0, 200, "0", "200", "success", "ok", None)
    for key in ("data", "d", "result", "list", "slots", "cells", "rows"):
        if key in payload:
            return ok, payload[key]
    values = list(payload.values())
    if values and any(isinstance(v, (list, dict)) for v in values):
        return ok, payload
    return False, None


def explain_error(payload: Any) -> str:
    """把 eportal 的 e 码翻成人话。"""
    if not isinstance(payload, dict):
        return str(payload)[:200]
    e = payload.get("e")
    m = payload.get("m") or payload.get("msg") or ""
    hints = {
        "10042": "未登录 / Cookie 失效 —— 重新抓 Cookie",
        "10043": "登录态过期 —— 重新抓 Cookie",
        "10001": "参数错误 —— 检查 resource_id / 日期格式",
    }
    hint = hints.get(str(e).strip())
    if hint:
        return f"e={e} {m}（{hint}）"
    return f"e={e} {m}" if e is not None else str(payload)[:200]


def _flatten(data: Any) -> list[dict]:
    """网格可能是 {"851": [cell]}、[cell]、{"2026-10-29": [...]}、多层嵌套。

    实测是第一种：键是资源池 id，值是单元格列表。
    """
    out: list[dict] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and any(
                    k in item for k in ("slots", "list", "cells", "rows")):
                out.extend(_flatten(item))
            elif isinstance(item, dict):
                out.append(item)
        return out
    if isinstance(data, dict):
        for key in ("slots", "list", "cells", "rows"):
            if key in data:
                return _flatten(data[key])
        for value in data.values():
            out.extend(_flatten(value))
    return out


def parse_margin(payload: dict, *, include_unavailable: bool = False) -> list[dict] | None:
    """归一化成 [{period, sub, court, time, date, total, margin, price}]，按时间排序。

    返回 None = 业务失败或结构不认识（调用方应报错，不能当"没空位"去刷）
    返回 []   = 接口正常，但没有任何可约场次
    """
    ok, data = unwrap(payload)
    if not ok:
        return None
    if data in (None, "", [], {}):
        return []
    cells = _flatten(data)
    if not cells:
        return None

    out = []
    for c in cells:
        if not isinstance(c, dict):
            continue
        total = _first_deep(c, ALIASES["total"])
        margin = _first_deep(c, ALIASES["margin"])
        available = _is_bookable(_status_of(c), margin)
        if not include_unavailable and not available:
            continue
        slot = {
            "period": _first(c, ALIASES["period"]),
            "sub": _first(c, ALIASES["sub"]),
            "court": _first(c, ALIASES["court"]),
            "time": _first(c, ALIASES["time"]),
            "date": _first(c, ALIASES["date"]),
            "total": total,
            "margin": margin,
            "price": _first_deep(c, ALIASES["price"]),
        }
        if slot["period"] is None:
            continue
        if str(slot.get("sub") or 0) == "0" and slot["court"] == slot["date"]:
            slot["court"] = ""
        if include_unavailable:
            slot["available"] = available
        out.append(slot)
    out.sort(key=lambda s: (str(s["time"] or ""), str(s["court"] or "")))
    return out


def build_launch_form(resource_id: int, date: str, slot: dict,
                      code: str = "", remarks: str = "", deduct_num: Any = "") -> dict:
    """提交表单，字段与前端 `o = {resource_id, code, remarks, deduct_num, data}` 一致。

    data 是 [{date, period, sub_resource_id}]，date 用 YYYY-MM-DD。
    """
    import json
    period = slot.get("period", slot.get("period_id"))
    sub = slot.get("sub", slot.get("sub_id"))
    return {
        "resource_id": resource_id,
        "code": code,
        "remarks": remarks,
        "deduct_num": deduct_num,
        "data": json.dumps([{
            "date": date,
            "period": period,
            "sub_resource_id": 0 if sub is None else sub,
        }], ensure_ascii=False),
    }


def describe_status(v: Any) -> str:
    """日志用：status 翻中文。"""
    mapping = {
        STATUS_EXPIRED: "过期",
        STATUS_FREE: "可约",
        STATUS_FREE_SUB: "可约(子场)",
        STATUS_FULL: "约满可包场",
        STATUS_INVALID: "失效",
        STATUS_OPEN: "可约",
    }
    st = _to_int(v)
    if st is None:
        return str(v)
    return mapping.get(st, f"未知({st})")


def status_stats(payload: dict) -> dict[str, int]:
    """统计整份响应的 status 分布（含不可约的），给诊断用。"""
    ok, data = unwrap(payload)
    if not ok or not data:
        return {}
    cells = _flatten(data)
    counts: dict[str, int] = {}
    for c in cells:
        if not isinstance(c, dict):
            continue
        key = describe_status(_status_of(c))
        counts[key] = counts.get(key, 0) + 1
    return counts
