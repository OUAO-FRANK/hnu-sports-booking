#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证码点选工具（人工点，脚本提交）—— 这是绕过 OCR 最省事的做法。

流程：
    1. GET /site/captcha/click-get  取图和词表
    2. 弹一个窗口显示图片，你用鼠标依次点中词表里的字
    3. 脚本用 secretKey 做 AES 加密，POST /site/captcha/click-check
    4. 拿到 captchaVerification，写进 position_data 提交预约

先单独验证第 1 和第 3 步格式能否跑通：

    python tools/captcha_solver.py --selftest
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib import parse

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BASE = "https://eportal.hnu.edu.cn"


# --------------------------------------------------------------------------- #
# AES-128-ECB + PKCS7（AJ-Captcha 的 JS 端就是 CryptoJS AES 默认模式）
# 纯标准库实现，不依赖 pycryptodome
# --------------------------------------------------------------------------- #
SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _expand_key(key: bytes) -> list[list[int]]:
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = list(w[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [SBOX[b] for b in temp]
        w.append([w[i - nk][j] ^ temp[j] for j in range(4)])
    return [w[4 * r:4 * r + 4] for r in range(nr + 1)]


def _encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_rk(rk):
        for r in range(4):
            for c in range(4):
                s[r][c] ^= rk[c][r]

    def sub_bytes():
        for r in range(4):
            for c in range(4):
                s[r][c] = SBOX[s[r][c]]

    def shift_rows():
        for r in range(1, 4):
            s[r] = s[r][r:] + s[r][:r]

    def mix_columns():
        for c in range(4):
            a = [s[r][c] for r in range(4)]
            s[0][c] = _xtime(a[0]) ^ (_xtime(a[1]) ^ a[1]) ^ a[2] ^ a[3]
            s[1][c] = a[0] ^ _xtime(a[1]) ^ (_xtime(a[2]) ^ a[2]) ^ a[3]
            s[2][c] = a[0] ^ a[1] ^ _xtime(a[2]) ^ (_xtime(a[3]) ^ a[3])
            s[3][c] = (_xtime(a[0]) ^ a[0]) ^ a[1] ^ a[2] ^ _xtime(a[3])

    add_rk(round_keys[0])
    n = len(round_keys) - 1
    for rnd in range(1, n):
        sub_bytes()
        shift_rows()
        mix_columns()
        add_rk(round_keys[rnd])
    sub_bytes()
    shift_rows()
    add_rk(round_keys[n])
    return bytes(s[r][c] for c in range(4) for r in range(4))


def aes_ecb_encrypt_b64(plaintext: str, key: str) -> str:
    """等价于 CryptoJS.AES.encrypt(plaintext, key).toString()（ECB + Pkcs7 + base64）。"""
    data = plaintext.encode("utf-8")
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    rk = _expand_key(key.encode("utf-8"))
    out = b"".join(_encrypt_block(data[i:i + 16], rk) for i in range(0, len(data), 16))
    return base64.b64encode(out).decode()


def selftest_aes() -> bool:
    """用已知向量校验 AES 实现（NIST FIPS-197 附录 C.1）。"""
    rk = _expand_key(bytes.fromhex("000102030405060708090a0b0c0d0e0f"))
    ct = _encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff"), rk)
    expect = "69c4e0d86a7b0430d8cdb78070b4c55a"
    ok = ct.hex() == expect
    print(f"AES-128 已知向量: {'PASS' if ok else 'FAIL'}  got={ct.hex()}")
    return ok


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
def api_get(path: str, params: dict | None, cookie: str) -> dict:
    url = f"{BASE}{path}" + (("?" + parse.urlencode(params)) if params else "")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{BASE}/v2/reserve/reserveDetail?id=57",
        "Cookie": cookie,
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def api_post(path: str, form: dict, cookie: str) -> dict:
    data = parse.urlencode(form).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers={
        "User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{BASE}/v2/reserve/reserveDetail?id=57",
        "Cookie": cookie,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="只测 AES 与取图")
    a = ap.parse_args()

    print("=" * 66)
    print("AES 实现自检")
    print("=" * 66)
    if not selftest_aes():
        print("AES 实现有问题，停。")
        return 1

    cookie = (HERE / "cookie.txt").read_text(encoding="utf-8").strip()
    outdir = HERE / "out"
    outdir.mkdir(exist_ok=True)

    print()
    print("=" * 66)
    print("取验证码")
    print("=" * 66)
    js = api_get("/site/captcha/click-get", None, cookie)
    if js.get("e") not in (0, "0"):
        print(f"失败: {js}")
        return 1
    d = js["d"]
    token, secret = d["token"], d.get("secretKey", "")
    words = d.get("wordList") or []
    img = base64.b64decode(d["originalImageBase64"].split(",")[-1])
    png = outdir / "captcha.png"
    png.write_bytes(img)
    print(f"  token      = {token}")
    print(f"  secretKey  = {secret}")
    print(f"  wordList   = {words}")
    print(f"  图片        = {png}  ({len(img)} 字节)")

    # 模拟一次坐标提交，看服务端认不认字段格式
    print()
    print("=" * 66)
    print("用假坐标试提交，看字段格式对不对")
    print("=" * 66)
    points = [[100, 80], [200, 60], [150, 120], [220, 110]]
    point_json = json.dumps(points, separators=(",", ":"))
    if secret:
        point_enc = aes_ecb_encrypt_b64(point_json, secret)
        verification = aes_ecb_encrypt_b64(f"{token}---{point_json}", secret)
    else:
        point_enc = point_json
        verification = f"{token}---{point_json}"
    print(f"  加密后 pointJson 长度: {len(point_enc)}")

    body = {"captchaType": "clickWord", "pointJson": point_enc, "token": token}
    form = {"data": json.dumps(body, separators=(",", ":"))}
    res = api_post("/site/captcha/click-check", form, cookie)
    print(f"  e={res.get('e')!r}  m={res.get('m')!r}")
    print(f"  d={str(res.get('d'))[:200]}")

    if res.get("e") in (0, "0"):
        print("\n  [!!] 假坐标居然通过了？")
    elif "验证" in str(res.get("m", "")) or "error" in str(res.get("m", "")).lower():
        print("\n  [OK] 服务端正确拒绝了假坐标 —— 说明字段格式与加密方式都对，")
        print("       只差点对位置了。")
        print(f"       成功时 d 里会有 captchaVerification，等价于:")
        print(f"       {verification[:50]}...")
    else:
        print("\n  [??] 没看懂这个错误，可能需要调整字段名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
