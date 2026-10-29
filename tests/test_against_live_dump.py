#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用真实抓到的响应跑判定 —— 这是唯一有效的验收。

    python tests/test_against_live_dump.py
    python tests/test_against_live_dump.py out/margin_57_2026-10-29.txt

同时把 status 语义表逐条验证（含 status=5 可约这条关键结论）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from shape_adapter import (  # noqa: E402
    describe_status, parse_margin, status_stats,
)

DEFAULT = REPO / "out" / "margin_57_2026-10-29.txt"


def test_synthetic_semantics() -> int:
    """逐条验证 status 语义（合成数据，不依赖真实响应）。"""
    print("=" * 74)
    print("A. status 语义表（含 status=5 可约这条关键结论）")
    print("=" * 74)
    base = {"abscissa": "1号场", "yaxis": "18:00-19:00", "time_id": 1, "sub_id": 2}
    cases = [
        # (status, margin, total, 期望可约, 说明)
        (0, 0, 1, False, "过期"),
        (1, 1, 1, True, "可约"),
        (2, 1, 1, True, "可约(子场)"),
        (3, 0, 1, False, "约满(默认不抢包场)"),
        (4, 0, 1, False, "失效"),
        (4, 1, 1, False, "失效即使 margin>0 也不抢"),
        (5, 1, 1, True, "可约(部分已占) ← 实测当天 76 个这种"),
        (5, 0, 1, False, "status=5 但 margin=0 不抢"),
        (5, 1, 4, True, "可约，余 1/4"),
    ]
    fails = 0
    for st, mg, tt, expect, note in cases:
        payload = {"e": 0, "d": {"851": [
            dict(base, row={"status": st, "margin": mg, "total": tt})]}}
        got = parse_margin(payload)
        actual = bool(got)
        ok = actual == expect
        fails += 0 if ok else 1
        print(f"  status={st} margin={mg} total={tt}  "
              f"期望={str(expect):5} 实际={str(actual):5} "
              f"{'PASS' if ok else 'FAIL'}   {note}")
    return fails


def test_live_dump(path: Path) -> int:
    if not path.exists():
        print(f"\n[跳过] 没有真实响应文件 {path}")
        print("       先跑： python probe_live.py --venue 57 --date <日期> --dump-dir out")
        return 0

    payload = json.loads(path.read_text(encoding="utf-8"))
    print()
    print("=" * 74)
    print(f"B. 真实响应 {path.name}")
    print("=" * 74)

    e, m = payload.get("e"), payload.get("m")
    print(f"  外层: e={e!r} m={m!r}")

    d = payload.get("d")
    print(f"  d 类型: {type(d).__name__}, 键: {list(d)[:3] if isinstance(d, dict) else '—'}")
    if isinstance(d, dict) and d:
        key = list(d)[0]
        grid = d[key]
        print(f"  键 '{key}' 下的单元格数: {len(grid) if isinstance(grid, list) else '?'}")
        print(f"  注意: 键是资源池 id，不是日期（实测结论）")

    stats = status_stats(payload)
    print(f"  status 分布: {stats}")

    slots = parse_margin(payload)
    print(f"  可约场次: {len(slots) if slots is not None else 'None(结构不认识)'}")

    fails = 0
    if slots is None:
        print("  [FAIL] 结构不认识 —— 需要补别名表")
        fails += 1
    else:
        # 抽查：每个 slot 必须有 period/sub，且 margin>0
        bad = [s for s in slots if s["period"] is None or s["sub"] is None]
        if bad:
            print(f"  [FAIL] {len(bad)} 个 slot 缺 period/sub")
            fails += 1
        else:
            print("  [PASS] 所有 slot 都有 period/sub")

        pos = [s for s in slots if s.get("margin") and int(s["margin"]) > 0]
        if slots and not pos:
            print(f"  [FAIL] {len(slots)} 个 slot 但 margin 都不是正数 —— 判定逻辑有问题")
            fails += 1
        elif pos:
            print(f"  [PASS] {len(pos)}/{len(slots)} 个 slot 的 margin > 0")

        for s in slots[:8]:
            print(f"    {s['court']} {s['time']}  余 {s['margin']}/{s['total']}  "
                  f"¥{s.get('price')}  (period={s['period']} sub={s['sub']})")

        # 和 status 分布交叉验证
        bookable_count = stats.get("可约", 0) + stats.get("可约(子场)", 0)
        if bookable_count and not slots:
            print(f"  [FAIL] status 统计有 {bookable_count} 个可约，但 parse 出来 0 个")
            fails += 1

    return fails


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    fails = test_synthetic_semantics()
    fails += test_live_dump(path)
    print()
    print("=" * 74)
    print(f"失败 {fails} 项")
    print("=" * 74)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
