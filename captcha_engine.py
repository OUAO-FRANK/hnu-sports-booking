#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证码识别器 —— YOLO 定位 + PPOCR 认字 + 词表配对。

模型都在项目内的 models/ 目录，不依赖 %TEMP%。
    models/yolo_detect.onnx   来自 MgArcher/Text_select_captcha（best_v3.onnx）
    models/ppocr.onnx         来自 81NewArk/AntiCAP（[Dddd]-OCR.onnx）
    models/charsets.txt       来自 81NewArk/AntiCAP（[Dddd]-CharSets.txt）

用法：
    from captcha_engine import CaptchaEngine
    eng = CaptchaEngine()
    pts = eng.solve(png_path, ["泉","啸","躲","恼"])   # -> [[x,y],...]
"""

from __future__ import annotations

import json
import re
import sys
from os import cpu_count
from pathlib import Path
from time import monotonic

import numpy as np
from PIL import Image, ImageEnhance

MODELS = Path(__file__).resolve().parent / "models"

# 尺寸过滤：真字框实测 w/h 都在 22-46，长宽比 < 1.5
SIZE_TIERS = (
    (0.45, 22, 46, 1.5),
    (0.35, 20, 50, 1.7),
    (0.28, 18, 60, 2.0),
)


class _Yolo:
    """best_v3.onnx 的输出是 NMS 后的 [1, 300, 6]。

    实测：
        输入 images [1,3,640,640] float
        输出 output0 [1,300,6] float
    每行 = [x1, y1, x2, y2, conf, cls]
    坐标已经是 **letterbox 之后的 640 画布像素坐标**，不是归一化值。
    """

    def __init__(self, onnx_path: Path):
        import onnxruntime as ort
        _o = ort.SessionOptions()
        _o.log_severity_level = 4
        _o.intra_op_num_threads = min(4, cpu_count() or 1)
        self.sess = ort.InferenceSession(
            str(onnx_path), sess_options=_o,
            providers=["CPUExecutionProvider"])
        self.in_name = self.sess.get_inputs()[0].name
        shape = self.sess.get_inputs()[0].shape
        self.in_h = shape[2] if isinstance(shape[2], int) else 640
        self.in_w = shape[3] if isinstance(shape[3], int) else 640

    def infer(self, pil: Image.Image) -> list[list[float]]:
        """返回 [[x1,y1,x2,y2,conf,cls], ...]（已换算回原图坐标）。"""
        import cv2
        rgb = np.asarray(pil.convert("RGB"))
        h0, w0 = rgb.shape[:2]

        scale = min(self.in_w / w0, self.in_h / h0)
        nw, nh = int(w0 * scale), int(h0 * scale)
        canvas = np.full((self.in_h, self.in_w, 3), 114, np.uint8)
        dx, dy = (self.in_w - nw) // 2, (self.in_h - nh) // 2
        canvas[dy:dy + nh, dx:dx + nw] = cv2.resize(
            rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)

        blob = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32)
        blob /= 255.0
        out = self.sess.run(None, {self.in_name: blob})[0]   # [1, 300, 6]

        boxes = []
        for row in out[0]:
            if len(row) < 6:
                continue
            x1, y1, x2, y2, conf, cls = (float(v) for v in row[:6])
            if conf <= 0:
                continue
            # letterbox 逆变换
            x1 = (x1 - dx) / scale
            y1 = (y1 - dy) / scale
            x2 = (x2 - dx) / scale
            y2 = (y2 - dy) / scale
            x1, y1 = max(0.0, x1), max(0.0, y1)
            x2, y2 = min(float(w0), x2), min(float(h0), y2)
            if x2 - x1 < 4 or y2 - y1 < 4:
                continue
            boxes.append([x1, y1, x2, y2, conf, int(cls)])
        return boxes


class _Ocr:
    def __init__(self, onnx_path: Path, charset_path: Path):
        import onnxruntime as ort
        _o = ort.SessionOptions()
        _o.log_severity_level = 4
        _o.intra_op_num_threads = min(4, cpu_count() or 1)
        self.sess = ort.InferenceSession(
            str(onnx_path), sess_options=_o,
            providers=["CPUExecutionProvider"])
        self.name = self.sess.get_inputs()[0].name
        self.chars = self._load(charset_path)

    @staticmethod
    def _load(path: Path) -> list[str]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        tokens = re.findall(r'"((?:[^"\\]|\\.)*)"', raw)
        out = []
        for t in tokens:
            try:
                out.append(json.loads(f'"{t}"'))
            except json.JSONDecodeError:
                out.append(t)
        return out if len(out) > 100 else [" "] + list(raw.replace("\n", ""))

    def read(self, pil: Image.Image) -> str:
        im = pil.convert("L")
        h = 64                       # 模型硬要求，写 48 会 InvalidArgument
        w = max(16, min(320, int(im.size[0] * h / max(1, im.size[1]))))
        im = im.resize((w, h), Image.LANCZOS)
        a = np.asarray(im, np.float32) / 255.0
        a = ((a - 0.5) / 0.5)[None, None, :, :]
        out = self.sess.run(None, {self.name: a})[0]
        idx = out.argmax(axis=2).reshape(-1)
        res, last = [], 0
        for k in idx:
            if k == last:
                continue
            last = k
            if k != 0 and k < len(self.chars):
                res.append(self.chars[k])
        return "".join(res)


def _dedupe(boxes: list[list[float]], dist: float = 16.0) -> list[list[float]]:
    """按置信度降序去重：中心距 < dist 视为同一个框。"""
    out = []
    for b in sorted(boxes, key=lambda x: -x[4]):
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if any(abs(cx - (u[0] + u[2]) / 2) < dist and
               abs(cy - (u[1] + u[3]) / 2) < dist for u in out):
            continue
        out.append(b)
    return out


class CaptchaEngine:
    def __init__(self, models_dir: Path = MODELS):
        self.yolo = _Yolo(models_dir / "yolo_detect.onnx")
        self.ocr = _Ocr(models_dir / "ppocr.onnx", models_dir / "charsets.txt")

    def warmup(self):
        """Initialize both inference paths locally, without requesting a captcha."""
        self.yolo.infer(Image.new("RGB", (310, 155), "white"))
        self.ocr.read(Image.new("L", (32, 32), 255))

    def detect(self, pil: Image.Image) -> list[list[int]]:
        """保持常规检测条件；低置信度补救另需 solve 的精确 OCR 校验。"""
        return self._detect(pil)[0]

    def _detect(self, pil: Image.Image, *, rescue=False, strict_four=False) -> tuple[list[list[int]], bool]:
        """检测字框，返回按 x 排序的框（通常 4 个）。

        坑：模型输出 [1,300,6] 里除了 4 个真框，还有 165+ 个 conf≈0 的
        幽灵框，它们位置和真框重叠。如果先按 conf 降序再"距离<16 就去重"，
        低分幽灵框会先把位置占掉，导致真框被丢掉。

        正确做法：先按 conf 阈值过滤掉幽灵框，再做去重。
        """
        raw = self.yolo.infer(pil)
        # 先卡一个基础置信度，滤掉 conf≈0 的幽灵框
        solid = [d for d in raw if d[4] >= 0.25]

        for min_conf, lo, hi, ar_max in SIZE_TIERS:
            cand = []
            for d in solid:
                if d[4] < min_conf:
                    continue
                w, h = d[2] - d[0], d[3] - d[1]
                if not (lo <= w <= hi and lo <= h <= hi):
                    continue
                if max(w, h) / max(1, min(w, h)) > ar_max:
                    continue
                cand.append(d)
            uniq = _dedupe(cand)
            if len(uniq) >= 4:
                if strict_four and len(uniq) != 4:
                    return [], False
                uniq = sorted(uniq, key=lambda b: b[0])[:4]
                return [[int(b[0]), int(b[1]), int(b[2]), int(b[3])] for b in uniq], False

        if strict_four:
            return [], False
        uniq = _dedupe(solid)
        needs_exact_ocr = False
        # Only a previously incomplete result may use lower-confidence geometry.
        if rescue and len(uniq) < 4:
            _, lo, hi, ar_max = SIZE_TIERS[-1]
            candidates = []
            for d in raw:
                w, h = d[2] - d[0], d[3] - d[1]
                if (d[4] >= 0.20 and lo <= w <= hi and lo <= h <= hi
                        and max(w, h) / min(w, h) <= ar_max):
                    candidates.append(d)
            recovered = _dedupe(candidates)
            if len(recovered) == 4:
                uniq, needs_exact_ocr = recovered, True
        uniq = sorted(uniq, key=lambda b: b[0])
        return [[int(b[0]), int(b[1]), int(b[2]), int(b[3])] for b in uniq], needs_exact_ocr

    def solve(self, pil: Image.Image, words: list[str], *, contrast_deadline=None
              ) -> tuple[list[list[int]], list[str]]:
        """返回 (按词表顺序的坐标, 每个框的 OCR 结果)。

        contrast_deadline 是显式传入的单调时钟截止时间；默认不额外增强检测。

        配对策略（实测重要）：
            词表 4 个字互不相同，而且服务端校验时要求「4 个点全对」。
            OCR 会有认错的情况，但如果 OCR 结果里有 2 个以上能对上词表，
            说明这批框的质量不错，剩下的用排除法补齐。

            实测：精确匹配数越多通过率越高，
            但 0/4 精确匹配时也有通过的情况 —— 说明 OCR 错但位置对
            （因为字库相近字被混淆，位置本身没错）。
        """
        four_distinct = len(words) == 4 and len(set(words)) == 4
        boxes, needs_exact_ocr = self._detect(pil, rescue=four_distinct)
        if (len(boxes) < 4 and four_distinct
                and all(isinstance(word, str) and len(word) == 1 for word in words)
                and contrast_deadline is not None and monotonic() < contrast_deadline):
            enhanced = ImageEnhance.Contrast(pil).enhance(1.5)
            boxes, _ = self._detect(enhanced, strict_four=True)
            needs_exact_ocr = True
        if len(boxes) != len(words):
            return [], []
        heard = []
        for b in boxes:
            crop = pil.crop((b[0], b[1], b[2], b[3]))
            heard.append(self.ocr.read(crop) if crop.width and crop.height else "")
        if needs_exact_ocr and set(heard) != set(words):
            return [], heard

        # 打分矩阵：字 × 框
        n = len(words)
        score = [[0.0] * n for _ in range(n)]
        for i, ch in enumerate(words):
            for j, h in enumerate(heard):
                if not h:
                    continue
                if h == ch:
                    score[i][j] = 2.0           # 完全一致
                elif ch in h:
                    score[i][j] = 1.5           # OCR 多认了几个字
                elif any(c in h for c in ch):
                    score[i][j] = 0.5           # 部分重合
        # 全排列求最大权匹配
        from itertools import permutations
        best_perm, best_tot = None, -1.0
        for perm in permutations(range(n), n):
            tot = sum(score[i][perm[i]] for i in range(n))
            if tot > best_tot:
                best_tot, best_perm = tot, perm

        pts = []
        for i in range(n):
            b = boxes[best_perm[i]]
            pts.append([(b[0] + b[2]) // 2, (b[1] + b[3]) // 2])
        return pts, heard
