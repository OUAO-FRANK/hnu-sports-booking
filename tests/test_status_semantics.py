#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
status 语义专项测试。

语义来源：eportal 前端 bundle 逆向 + 真实响应验证（2026-10-28）
    0 过期 | 1 可约 | 2 可约(子场) | 3 约满可包场 | 4 失效 | 5 可约

关键：**status 5 是可约的**。实测 10-29 当天 76 个可约格子全部是 status=5，
      status 4（失效）36 个。这条如果搞错，整个脚本会把所有空位都过滤掉。

    python tests/test_status_semantics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shape_adapter import describe_status, parse_margin  # noqa: E402

CELL = {"abscissa": "1号场", "yaxis": "18:00-19:00", "time_id": 901, "sub_id": 301}

CASES = [
    # (status, margin, total, 期望可约, 说明)
    (0, 0, 1, False, "过期"),
    (1, 1, 1, True, "可约"),
    (2, 1, 1, True, "可约(子场)"),
    (3, 0, 1, False, "约满（默认不抢包场）"),
    (4, 0, 1, False, "失效"),
    (4, 1, 1, False, "失效即使 margin>0 也不抢"),
    (5, 1, 1, True, "可约 ← 实测 76 个空位都是这个"),
    (5, 0, 1, False, "status=5 但 margin=0 也不能约"),
    (5, 1, 4, True, "可约，余 1/4"),
    # 字符串写法
    ("1", 1, 1, True, "可约"),
    ("5", 1, 1, True, "可约"),
    ("3", 0, 1, False, "约满"),
    ("available", 1, 1, True, "可约"),
    ("booked", 0, 1, False, "已约"),
    ("full", 0, 1, False, "约满"),
    # 布尔兜底
    (True, 1, 1, True, "可约"),
    (False, 0, 1, False, "不可约"),
]


def main() -> int:
    fails = 0
    print(f"{'status':12}{'margin':8}{'期望':7}{'实际':7}{'含义':26}结果")
    print("-" * 74)
    for st, mg, tt, expect, label in CASES:
        payload = {"e": 0, "d": {"851": [
            dict(CELL, row={"status": st, "margin": mg, "total": tt})]}}
        got = parse_margin(payload)
        actual = bool(got)
        ok = actual == expect
        fails += 0 if ok else 1
        print(f"{str(st):12}{mg:<8}{str(expect):7}{str(actual):7}{label:26}"
              f"{'PASS' if ok else 'FAIL'}")

    print()
    print("describe_status 中文映射:")
    for st in range(6):
        print(f"  {st} -> {describe_status(st)}")

    # total / margin 透传（真实响应里这两个在 row 内）
    payload = {"e": 0, "d": {"851": [
        dict(CELL, row={"status": 5, "total": 4, "margin": 2})]}}
    got = parse_margin(payload)
    ok = bool(got) and got[0]["total"] == 4 and got[0]["margin"] == 2
    print(f"\ntotal/margin 透传: {'PASS' if ok else 'FAIL'} -> {got}")
    fails += 0 if ok else 1

    # 真实结构：d 的键是资源池 id，不是日期
    payload = {"e": 0, "d": {"851": [
        dict(CELL, date="2026-10-29",
             row={"status": 5, "total": 1, "margin": 1, "price": 20})]}}
    got = parse_margin(payload)
    ok = bool(got) and got[0]["price"] == 20
    print(f"d 键为池 id 时仍能解析 + price 透传: {'PASS' if ok else 'FAIL'}")
    fails += 0 if ok else 1

    print(f"\n失败 {fails} 个")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
