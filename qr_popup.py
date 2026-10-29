#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
支付二维码弹窗 —— 抢到后立刻弹出，置顶 + 响铃 + 倒计时。

=================== 为什么需要 ===================

    从「抢到」到「你扫码」，中间任何延迟都在烧那 2 分钟窗口。
    通过聊天软件发送消息，延迟可能有几十秒 —— 太晚了。

    这个弹窗做到：
        · 立刻出现在屏幕最前面（topmost）
        · 立刻响铃（winsound，Windows 系统音）
        · 大图二维码（500x500）+ 链接文字（可选中复制）
        · 倒计时显示还剩多少秒
        · 支付成功后自动关闭

=================== 用法 ===================

    from qr_popup import show_qr

    show_qr(
        content="http://wxpay.hnu.edu.cn/third/home?id=xxx",
        title="财院羽毛球 16:20-17:20  ¥10",
        seconds=110,
        on_poll=lambda: check_paid(),   # 返回 True 就自动关
    )

=================== 依赖 ===================

    只用标准库 tkinter + winsound + 已装的 segno/PIL。
    不需要额外安装任何东西。
"""
from __future__ import annotations

import threading
import time
from math import ceil
from queue import Empty, SimpleQueue
from pathlib import Path


class PaymentStopped(RuntimeError):
    """服务端已确认预约结束，不能继续提示用户付款。"""


class _PaymentCheck:
    """网络线程只写结果队列，所有 Tk 操作留在主线程。"""
    def __init__(self, callback):
        self.callback = callback
        self.results = SimpleQueue()
        self.busy = False

    def start(self):
        if self.busy or self.callback is None:
            return
        self.busy = True

        def worker():
            try:
                self.results.put((bool(self.callback()), None))
            except Exception as exc:
                self.results.put((False, exc))

        threading.Thread(target=worker, daemon=True).start()

    def take(self):
        result = self.results.get_nowait()
        self.busy = False
        return result


def _beep_loop(stop: threading.Event):
    """循环响铃，直到 stop 被设置。"""
    try:
        import winsound
        patterns = [
            (winsound.MB_ICONHAND, 700),   # 系统错误音（最响）
            (winsound.MB_ICONHAND, 700),
            (winsound.MB_ICONHAND, 700),
        ]
        for freq, dur in patterns:
            if stop.is_set():
                return
            winsound.PlaySound(
                r"C:\Windows\Media\Alarm01.wav",
                winsound.SND_FILENAME | winsound.SND_ASYNC)
            time.sleep(0.9)
        # 之后持续用系统音
        while not stop.is_set():
            winsound.MessageBeep(winsound.MB_ICONHAND)
            time.sleep(1.4)
    except Exception:
        # 没有 winsound 就退化为打印响铃
        while not stop.is_set():
            print("\a", end="", flush=True)
            time.sleep(0.8)


def show_qr(content: str, title: str = "", seconds: int = 120,
            on_poll=None, png_path: Path | None = None,
            poll_interval: float = 4.0, deadline: float | None = None,
            estimated: bool = True) -> bool:
    """弹出置顶二维码窗口。

    content       二维码内容（wxpay 链接）
    title         标题，如 "财院羽毛球 16:20-17:20 ¥10"
    seconds       倒计时秒数
    on_poll       回调，返回 True 则自动关闭（用于检测支付成功）
    png_path      预生成的二维码 PNG；None 则现场生成
    poll_interval on_poll 的轮询间隔

    返回 True 表示支付成功（由 on_poll 判定），False 表示超时/手动关闭。
    """
    import tkinter as tk
    from tkinter import font as tkfont
    if deadline is None:
        deadline = time.monotonic() + seconds

    # ---- 生成/复用二维码图 ----
    if png_path is None or not Path(png_path).exists():
        import segno
        tmp = Path(__file__).resolve().parent / "out" / "_popup_qr.png"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        segno.make(content, error="m").save(
            str(tmp), scale=14, border=3, kind="png")
        png_path = tmp

    from PIL import Image, ImageTk

    state = {"done": False, "paid": False, "finished": False}
    check = _PaymentCheck(on_poll)
    stop_beep = threading.Event()

    # ---- 主窗口 ----
    root = tk.Tk()
    root.title("扫码支付")
    root.attributes("-topmost", True)
    root.configure(bg="#1a1a1a")

    # 居中显示
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    window_height = min(850, sh - 60)
    qr_side = min(400, max(120, window_height - 450))
    root.geometry(f"620x{window_height}+{(sw-620)//2}+{max(0,(sh-window_height)//2-20)}")

    # 标题
    f_title = tkfont.Font(family="Microsoft YaHei", size=16, weight="bold")
    f_mono = tkfont.Font(family="Consolas", size=10)
    f_big = tkfont.Font(family="Microsoft YaHei", size=28, weight="bold")

    if title:
        tk.Label(root, text=title, font=f_title,
                 fg="#00ff88", bg="#1a1a1a",
                 wraplength=580).pack(pady=(14, 6))

    tk.Label(root, text="请立即扫码支付", font=f_title,
             fg="#ffffff", bg="#1a1a1a").pack(pady=2)

    # 二维码图
    img = Image.open(png_path)
    # 为状态文字和按钮保留空间，避免原 760px 窗口裁掉底部操作。
    side = max(img.size)
    canvas_img = Image.new("RGB", (side, side), "white")
    canvas_img.paste(img.convert("RGB"),
                     ((side - img.size[0]) // 2, (side - img.size[1]) // 2))
    canvas_img = canvas_img.resize((qr_side, qr_side), Image.NEAREST)
    photo = ImageTk.PhotoImage(canvas_img)

    lbl_img = tk.Label(root, image=photo, bg="#1a1a1a", bd=0)
    lbl_img.image = photo
    lbl_img.pack(pady=8)

    # 倒计时
    lbl_time = tk.Label(root, text="", font=f_big,
                        fg="#ff4444", bg="#1a1a1a")
    lbl_time.pack(pady=(2, 4))

    # 链接（可选中复制）
    lbl_url = tk.Text(root, height=2, width=68, font=f_mono,
                      bg="#2a2a2a", fg="#88ccff", bd=0,
                      wrap="char", cursor="hand2")
    lbl_url.insert("1.0", content)
    lbl_url.pack(pady=4)

    def copy_url(_evt=None):
        root.clipboard_clear()
        root.clipboard_append(content)
        lbl_url.tag_add("sel", "1.0", "end")
        status_lbl.config(text="✓ 链接已复制到剪贴板", fg="#00ff88")

    lbl_url.bind("<Button-1>", copy_url)
    btn_copy = tk.Button(root, text="复制链接", command=copy_url,
                         bg="#333", fg="white", bd=0,
                         font=("Microsoft YaHei", 11), padx=16, pady=5)
    btn_copy.pack(pady=4)

    status_lbl = tk.Label(root, text="", font=("Microsoft YaHei", 11),
                          fg="#ffcc00", bg="#1a1a1a", wraplength=580)
    status_lbl.pack(pady=(6, 4))

    def close_all(paid: bool = False):
        state["done"] = True
        state["paid"] = paid or state["paid"]
        stop_beep.set()
        try:
            root.destroy()
        except Exception:
            pass

    def request_check():
        if state["done"] or state["finished"]:
            return
        status_lbl.config(text="正在核对服务端预约状态…", fg="#ffcc00")
        check.start()

    tk.Button(root, text="我已付款，重新检查", command=request_check,
              bg="#00aa55", fg="white", bd=0,
              font=("Microsoft YaHei", 12, "bold"),
              padx=20, pady=8).pack(pady=6)
    tk.Button(root, text="关闭", command=lambda: close_all(False),
              bg="#444", fg="white", bd=0,
              font=("Microsoft YaHei", 11), padx=20, pady=5).pack(pady=(0, 12))

    # ---- 响铃线程 ----
    threading.Thread(target=_beep_loop, args=(stop_beep,),
                     daemon=True).start()

    # ---- 倒计时 ----
    def tick():
        if state["done"] or state["finished"]:
            return
        left = ceil(deadline - time.monotonic())
        if left <= 0:
            lbl_time.config(text="00:00")
            status_lbl.config(text="未确认预约生效，请在网页核对",
                              fg="#ff4444")
            close_all(False)
            return
        m, s = divmod(left, 60)
        label = "预计剩余" if estimated else "剩余"
        lbl_time.config(text=f"{label} {m:02d}:{s:02d}")
        if left <= 20:
            lbl_time.config(fg="#ff0000")
        root.after(500, tick)

    # ---- 支付状态轮询 ----
    def poll():
        if state["done"] or state["finished"] or on_poll is None:
            return
        check.start()
        root.after(int(poll_interval * 1000), poll)

    def receive_result():
        if state["done"] or state["finished"]:
            return
        try:
            paid, error = check.take()
            if paid:
                state["paid"] = True
                state["finished"] = True
                stop_beep.set()
                status_lbl.config(text="✓ 服务端确认预约生效！", fg="#00ff88")
                lbl_time.config(text="", fg="#00ff88")
                root.after(1200, lambda: close_all(True))
                return
            if isinstance(error, PaymentStopped):
                state["finished"] = True
                stop_beep.set()
                status_lbl.config(text=str(error), fg="#ff4444")
                root.after(2000, lambda: close_all(False))
                return
            text = f"查询失败：{error}" if error else "尚未确认预约生效，请等待服务端更新"
            status_lbl.config(text=text, fg="#ffcc00")
        except Empty:
            pass
        root.after(100, receive_result)

    if on_poll:
        poll()
        root.after(100, receive_result)
    tick()

    # 窗口关闭按钮
    root.protocol("WM_DELETE_WINDOW", lambda: close_all(False))

    # 强制置顶 + 抢焦点
    def force_top(remaining=3):
        if state["done"]:
            return
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        if remaining > 1:
            root.after(250, lambda: force_top(remaining - 1))

    force_top()

    try:
        root.mainloop()
    except Exception:
        pass
    stop_beep.set()
    return state["paid"]


if __name__ == "__main__":
    # 自测：用假链接演示
    import sys
    demo = (sys.argv[1] if len(sys.argv) > 1
            else "http://wxpay.hnu.edu.cn/third/home?id=DEMO123456")
    print(f"演示内容: {demo}")
    paid = show_qr(demo, title="演示 · 财院羽毛球 16:20-17:20  ¥10.00",
                   seconds=30)
    print(f"结果: {'已支付' if paid else '未支付/关闭'}")
