#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AJ-Captcha clickWord 求解器 v4 —— 滑窗找峰 + 互斥分配。

v2 的问题：4 个字各自全图搜索，都吸到最像自己的那个位置（撞车）。
v3 的问题：干扰线把字连成一片，连通块切不开。

v4 换思路：把"位置"和"认字"解耦，做成经典的 4x4 分配问题。
    1. 先在图上找出**所有**候选位置：用"字模板族的平均响应"做一次滑窗，
       取局部极大值，得到 N 个候选点（N >= 4）
    2. 对每个候选点裁 patch，与 4 个字的模板逐一打分，得到 4xN 得分矩阵
    3. 匈牙利/全排列求"字→位置"最大权匹配，且每个位置只能用一次

    这一步的关键是候选点的召回要够（有 4 个字的位置在里面），
    再用分配约束保证不撞车。
"""

from __future__ import annotations

import sys
from itertools import permutations
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = Path(r"C:\Windows\Fonts")
FONT_CANDIDATES = ["simhei.ttf", "msyh.ttc", "simsun.ttc",
                   "simkai.ttf", "simfang.ttf"]
SIZES = (27, 31, 35)
ANGLES = (-12, 0, 12)


def prep(img_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """返回 (原图灰度, 前景强度 0..1)。"""
    rgb = np.asarray(Image.open(img_path).convert("RGB"), dtype=np.float32)
    a = np.asarray(Image.open(img_path).convert("L"), dtype=np.float32)
    bg = float(np.median(np.concatenate([
        a[:5, :].ravel(), a[-5:, :].ravel(),
        a[:, :5].ravel(), a[:, -5:].ravel()])))
    fg = np.clip((bg - a) / max(1.0, bg), 0, 1)
    fg = np.asarray(
        Image.fromarray((fg * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(1.0)), dtype=np.float32) / 255.0
    return a, fg


def render(ch: str, size: int, angle: float, fname: str) -> np.ndarray | None:
    fpath = FONT_DIR / fname
    if not fpath.exists():
        return None
    try:
        font = ImageFont.truetype(str(fpath), size)
    except OSError:
        return None
    img = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(img).text((size // 2, size // 2), ch, fill=255, font=font)
    if angle:
        img = img.rotate(angle, resample=Image.BILINEAR, expand=False)
    a = np.asarray(img, dtype=np.float32) / 255.0
    ys, xs = np.nonzero(a > 0.35)
    if len(ys) < 40:
        return None
    t = a[max(0, ys.min() - 1):ys.max() + 2, max(0, xs.min() - 1):xs.max() + 2]
    return t - t.mean()


def ncc_at(fg: np.ndarray, tpl: np.ndarray, x: int, y: int) -> float:
    th, tw = tpl.shape
    if y + th > fg.shape[0] or x + tw > fg.shape[1]:
        return -1.0
    w = fg[y:y + th, x:x + tw]
    wz = (w - w.mean())
    wn = float(np.sqrt((wz * wz).sum()))
    if wn < 1e-6:
        return -1.0
    tn = float(np.sqrt((tpl * tpl).sum()))
    return float((wz * tpl).sum() / (wn * tn))


def all_templates(chars: list[str]) -> list[tuple[str, np.ndarray]]:
    out = []
    for ch in chars:
        for fname in FONT_CANDIDATES:
            for size in SIZES:
                for angle in ANGLES:
                    t = render(ch, size, angle, fname)
                    if t is not None:
                        out.append((ch, t))
    return out


def find_candidates(fg: np.ndarray, tpls, max_n: int = 8) -> list[tuple[int, int]]:
    """用所有模板的平均响应找候选位置（局部极大）。"""
    h, w = fg.shape
    score = np.full((h, w), -1.0, dtype=np.float32)
    for _, t in tpls:
        th, tw = t.shape
        for y in range(0, h - th + 1, 3):
            for x in range(0, w - tw + 1, 3):
                v = ncc_at(fg, t, x, y)
                if v > score[y + th // 2, x + tw // 2]:
                    score[y + th // 2, x + tw // 2] = v

    # 局部极大抑制
    pts = []
    work = score.copy()
    for _ in range(max_n * 3):
        idx = int(work.argmax())
        if work.ravel()[idx] < 0.25:
            break
        cy, cx = divmod(idx, w)
        pts.append((cx, cy, float(work.ravel()[idx])))
        # 抑制邻域
        r = 30
        work[max(0, cy - r):cy + r, max(0, cx - r):cx + r] = -1
        if len(pts) >= max_n:
            break
    return pts


def score_matrix(fg: np.ndarray, words: list[str], pts) -> np.ndarray:
    """words × pts 的得分矩阵。"""
    n, m = len(words), len(pts)
    S = np.full((n, m), -1.0, dtype=np.float32)
    for j, (cx, cy, _) in enumerate(pts):
        for i, ch in enumerate(words):
            best = -1.0
            for fname in FONT_CANDIDATES:
                for size in SIZES:
                    for angle in ANGLES:
                        t = render(ch, size, angle, fname)
                        if t is None:
                            continue
                        th, tw = t.shape
                        # 以候选点为中心对齐
                        v = ncc_at(fg, t, max(0, cx - tw // 2), max(0, cy - th // 2))
                        if v > best:
                            best = v
            S[i, j] = best
    return S


def solve(img_path: Path, words: list[str]):
    _, fg = prep(img_path)
    tpls = all_templates(words)
    pts = find_candidates(fg, tpls, max_n=max(6, len(words) + 2))
    if len(pts) < len(words):
        return [], -1.0, pts, None

    S = score_matrix(fg, words, pts)
    n = len(words)
    best_perm, best_total = None, -1e9
    for perm in permutations(range(len(pts)), n):
        total = sum(S[i, perm[i]] for i in range(n))
        if total > best_total:
            best_total, best_perm = total, perm

    res = []
    for i, ch in enumerate(words):
        j = best_perm[i]
        res.append({
            "char": ch, "score": round(float(S[i, j]), 3),
            "x": pts[j][0], "y": pts[j][1],
        })
    return res, best_total / max(1, n), pts, S


def main() -> int:
    args = sys.argv[1:]
    img_path = Path(__file__).resolve().parent.parent / "out" / "captcha.png"
    if "--img" in args:
        i = args.index("--img")
        img_path = Path(args[i + 1])
        args = args[:i] + args[i + 2:]
    words = [a for a in args if len(a) == 1]
    if not words:
        print("用法: python tools/captcha_v4.py 字1 字2 字3 字4 [--img 图]")
        return 2
    if not img_path.exists():
        print(f"没有图片 {img_path}")
        return 1

    import time
    t0 = time.time()
    res, avg, pts, S = solve(img_path, words)
    dt = time.time() - t0

    print(f"图片 {img_path.name}  词表 {words}  耗时 {dt:.1f}s")
    print(f"\n候选位置 {len(pts)} 个:")
    for i, (x, y, s) in enumerate(pts):
        print(f"  [{i}] ({x},{y}) 峰={s:.3f}")

    if S is not None:
        print(f"\n得分矩阵 ({len(words)}字 × {len(pts)}位置):")
        print("       " + "".join(f"{i:>8}" for i in range(len(pts))))
        for i, ch in enumerate(words):
            print(f"  {ch}    " + "".join(f"{S[i,j]:8.3f}" for j in range(len(pts))))

    print(f"\n分配结果:")
    for r in res:
        print(f"  {r['char']} -> ({r['x']},{r['y']})  score={r['score']}")
    print(f"\n平均 {avg:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
