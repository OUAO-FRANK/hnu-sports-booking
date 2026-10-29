#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shape_adapter 的离线测试：喂四种返回形态，检查归一化结果。
    python tests/test_shape_adapter.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shape_adapter import parse_margin  # noqa: E402

CASES = []

# 1) 老结构：e/d + row.status==1
CASES.append(("legacy", {
    "e": 0, "m": "ok",
    "d": {"2026-10-28": [
        {"abscissa": "1号场", "yaxis": "18:00-19:00", "time_id": 901, "sub_id": 301,
         "row": {"status": 1}},
        {"abscissa": "2号场", "yaxis": "18:00-19:00", "time_id": 901, "sub_id": 302,
         "row": {"status": 0}},
    ]},
}, 1))

# 2) 新结构：code/msg/data，字段改名，status 是字符串
CASES.append(("new_style", {
    "code": 200, "msg": "success",
    "data": {"slots": [
        {"courtName": "1号场", "timeRange": "20:00-21:00", "periodId": 903,
         "subResourceId": 301, "status": "available"},
        {"courtName": "1号场", "timeRange": "19:00-20:00", "periodId": 902,
         "subResourceId": 301, "status": "booked"},
    ]},
}, 1))

# 3) 扁平列表 + 布尔字段
CASES.append(("flat_bool", [
    {"court": "3号场", "time": "09:00-10:00", "period": 1, "sub": 7, "available": True},
    {"court": "3号场", "time": "10:00-11:00", "period": 2, "sub": 7, "available": False},
], 1))

# 4) 嵌套日期 -> 两层
CASES.append(("nested", {
    "e": 0,
    "d": {"2026-10-28": {"cells": [
        {"abscissa": "4号场", "yaxis": "21:00-22:00", "time_id": 904, "sub_id": 304,
         "row": {"state": 1}},
    ]}},
}, 1))

# 5) 业务失败
CASES.append(("biz_error", {"e": 1, "m": "无权限"}, None))

# 6) 当天无排期
CASES.append(("empty", {"e": 0, "d": {}}, 0))

# 7) 完全不认识的结构
CASES.append(("garbage", {"hello": "world"}, None))


def main() -> int:
    fails = 0
    for name, payload, expect in CASES:
        got = parse_margin(payload)
        n = None if got is None else len(got)
        ok = n == expect
        fails += 0 if ok else 1
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name:12} expect={expect!s:6} got={n!s:6}", end="")
        if got:
            best = got[0]
            print(f"  首个: {best['court']} {best['time']} p={best['period']} s={best['sub']}")
        else:
            print()

    # 排序检查
    got = parse_margin(CASES[1][1])
    times = [s["time"] for s in got]
    if times == sorted(times):
        print("[PASS] 排序：按时间升序")
    else:
        print(f"[FAIL] 排序异常: {times}")
        fails += 1

    print(f"\n{len(CASES)} 个用例，失败 {fails} 个")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
