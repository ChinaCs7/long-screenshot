# -*- coding: utf-8 -*-
"""
滚动长截图工具（Windows）

流程：运行 → 框选屏幕区域 → 点底部「长截图」→ 在选区内用滚轮上下滚动 →
      右侧实时预览长图 → 保存（选路径）/ 取消 / 确认（复制到剪贴板）

快捷键：Esc 随时取消。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import io
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox

if sys.platform != "win32":
    sys.exit("本工具目前只支持 Windows。")

# 必须在创建任何窗口之前声明 DPI 感知，否则高分屏下 Tk 坐标与截图像素对不上
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import mss  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageEnhance, ImageTk  # noqa: E402

from stitcher import Stitcher  # noqa: E402

GREEN = "#07C160"
RED = "#FA5151"
INK = "#333333"

# ── Win32 ────────────────────────────────────────────────────────────

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32")

user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, wt.BOOL]
user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.OpenClipboard.argtypes = [wt.HWND]
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wt.UINT
gdi32.CreateRectRgn.argtypes = [ctypes.c_int] * 4
gdi32.CreateRectRgn.restype = wt.HRGN
gdi32.CombineRgn.argtypes = [wt.HRGN, wt.HRGN, wt.HRGN, ctypes.c_int]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]

GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
RGN_DIFF = 4
WDA_EXCLUDEFROMCAPTURE = 0x11  # Win10 2004+：窗口不出现在任何截图里
VK_ESCAPE = 0x1B
CF_DIB = 8
GMEM_MOVEABLE = 0x0002


def hwnd_of(win: tk.Misc) -> int:
    win.update_idletasks()
    return user32.GetAncestor(win.winfo_id(), GA_ROOT)


def make_passive(win: tk.Misc) -> None:
    """点击不抢焦点（滚轮继续发给底下的应用），且不被截进画面。"""
    h = hwnd_of(win)
    user32.SetWindowLongW(h, GWL_EXSTYLE, user32.GetWindowLongW(h, GWL_EXSTYLE) | WS_EX_NOACTIVATE)
    user32.SetWindowDisplayAffinity(h, WDA_EXCLUDEFROMCAPTURE)


def punch_hole(win: tk.Misc, width: int, height: int, hole: tuple[int, int, int, int]) -> None:
    """把窗口形状设成「整块减去一个矩形洞」：洞里既看不到遮罩，鼠标也直接落到底下的窗口。"""
    full = gdi32.CreateRectRgn(0, 0, width, height)
    x0, y0, x1, y1 = hole
    cut = gdi32.CreateRectRgn(x0, y0, x1, y1)
    gdi32.CombineRgn(full, full, cut, RGN_DIFF)
    gdi32.DeleteObject(cut)
    user32.SetWindowRgn(hwnd_of(win), full, True)  # 成功后区域归系统所有，不能再删


def copy_image_to_clipboard(img: Image.Image) -> None:
    """同时放 CF_DIB（通用）和 PNG（微信 / 浏览器 / Office 等优先用）。"""
    bmp = io.BytesIO()
    img.convert("RGB").save(bmp, "BMP")
    png = io.BytesIO()
    img.save(png, "PNG")
    payloads = [(CF_DIB, bmp.getvalue()[14:]),  # 去掉 14 字节 BMP 文件头就是 DIB
                (user32.RegisterClipboardFormatW("PNG"), png.getvalue())]

    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("剪贴板被其它程序占用")
    try:
        user32.EmptyClipboard()
        for fmt, data in payloads:
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                raise MemoryError("GlobalAlloc 失败")
            ctypes.memmove(kernel32.GlobalLock(h), data, len(data))
            kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(fmt, h):
                kernel32.GlobalFree(h)
                raise RuntimeError("SetClipboardData 失败")
    finally:
        user32.CloseClipboard()


# ── 小组件 ───────────────────────────────────────────────────────────

def _icon_long(cv: tk.Canvas, s: int) -> None:
    f = s / 36
    cv.create_rectangle(11 * f, 6 * f, 25 * f, 30 * f, outline=INK, width=1.6 * f)
    cv.create_line(18 * f, 11 * f, 18 * f, 25 * f, fill=INK, width=1.6 * f,
                   arrow="both", arrowshape=(5 * f, 6 * f, 2.5 * f))


def _icon_cancel(cv: tk.Canvas, s: int) -> None:
    f = s / 36
    cv.create_line(11 * f, 11 * f, 25 * f, 25 * f, fill=RED, width=2.2 * f, capstyle="round")
    cv.create_line(25 * f, 11 * f, 11 * f, 25 * f, fill=RED, width=2.2 * f, capstyle="round")


def _icon_ok(cv: tk.Canvas, s: int) -> None:
    f = s / 36
    cv.create_line(9 * f, 18 * f, 15.5 * f, 24.5 * f, 27 * f, 12 * f, fill=GREEN,
                   width=2.4 * f, capstyle="round", joinstyle="round")


def _icon_save(cv: tk.Canvas, s: int) -> None:
    f = s / 36
    cv.create_line(18 * f, 8 * f, 18 * f, 22 * f, fill=INK, width=1.8 * f,
                   arrow="last", arrowshape=(6 * f, 7 * f, 3 * f))
    cv.create_line(10 * f, 20 * f, 10 * f, 27 * f, 26 * f, 27 * f, 26 * f, 20 * f,
                   fill=INK, width=1.8 * f, joinstyle="round")


class IconButton(tk.Canvas):
    def __init__(self, master, draw, command, tip: str, size: int):
        super().__init__(master, width=size, height=size, bg="white",
                         highlightthickness=0, bd=0, cursor="hand2")
        self._draw, self._size, self._tip_text = draw, size, tip
        self._tip: tk.Toplevel | None = None
        self._paint("white")
        self.bind("<Enter>", lambda e: (self._paint("#EDEDED"), self._show_tip()))
        self.bind("<Leave>", lambda e: (self._paint("white"), self._hide_tip()))
        self.bind("<ButtonRelease-1>", lambda e: (self._hide_tip(), command()))

    def _paint(self, bg: str) -> None:
        self.delete("all")
        self.configure(bg=bg)
        self._draw(self, self._size)

    def _show_tip(self) -> None:
        if self._tip or not self._tip_text:
            return
        tip = tk.Toplevel(self)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=self._tip_text, bg="#4C4C4C", fg="white",
                 font=("Microsoft YaHei UI", 9), padx=6, pady=2).pack()
        tip.update_idletasks()
        x = self.winfo_rootx() + (self._size - tip.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + self._size + 6
        tip.geometry(f"+{x}+{y}")
        make_passive(tip)
        self._tip = tip

    def _hide_tip(self) -> None:
        if self._tip:
            self._tip.destroy()
            self._tip = None


def borderless(root: tk.Misc, x: int, y: int, w: int, h: int, bg: str) -> tk.Toplevel:
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg=bg)
    win.geometry(f"{max(1, w)}x{max(1, h)}+{x}+{y}")
    return win


def toast(root: tk.Misc, text: str, center: tuple[int, int], ms: int = 1300, then=None) -> None:
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.attributes("-alpha", 0.92)
    tk.Label(win, text=text, bg="#2B2B2B", fg="white",
             font=("Microsoft YaHei UI", 11), padx=18, pady=10).pack()
    win.update_idletasks()
    win.geometry(f"+{center[0] - win.winfo_reqwidth() // 2}+{center[1] - win.winfo_reqheight() // 2}")
    make_passive(win)
    win.after(ms, lambda: (win.destroy(), then and then()))


# ── 第一步：框选 ─────────────────────────────────────────────────────
#
# 只框选一次：框好之后不能重新框，只能调整——拖四角 / 四边中点拉伸，
# 框内拖动整体移动。右键取消选区（可重新框），没有选区时右键退出。

Sel = tuple[int, int, int, int]  # (x0, y0, x1, y1)，x0<x1、y0<y1

_CURSORS = {"l": "size_we", "r": "size_we", "t": "size_ns", "b": "size_ns",
            "lt": "size_nw_se", "rb": "size_nw_se", "rt": "size_ne_sw", "lb": "size_ne_sw",
            "move": "fleur"}


def hit_test(sel: Sel, x: int, y: int, tol: int) -> str | None:
    """鼠标落在选区的哪里：'l'/'r'/'t'/'b' 及组合（角）、'move'（框内）、None（框外）。"""
    x0, y0, x1, y1 = sel
    if not (x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol):
        return None
    edges = ""
    if abs(x - x0) <= tol:
        edges += "l"
    elif abs(x - x1) <= tol:
        edges += "r"
    if abs(y - y0) <= tol:
        edges += "t"
    elif abs(y - y1) <= tol:
        edges += "b"
    if edges:
        return edges
    return "move" if x0 < x < x1 and y0 < y < y1 else None


def resize_sel(sel: Sel, edges: str, x: int, y: int, w: int, h: int) -> tuple[Sel, str]:
    """把 edges 指定的边拖到 (x, y)。拖过对边时自动翻转，并返回翻转后的边，
    这样继续拖动手感连贯（和微信 / QQ 截图一致）。"""
    x = min(max(x, 0), w)
    y = min(max(y, 0), h)
    x0, y0, x1, y1 = sel
    if "l" in edges:
        x0 = x
    if "r" in edges:
        x1 = x
    if "t" in edges:
        y0 = y
    if "b" in edges:
        y1 = y
    if x0 > x1:
        x0, x1 = x1, x0
        edges = edges.translate(str.maketrans("lr", "rl"))
    if y0 > y1:
        y0, y1 = y1, y0
        edges = edges.translate(str.maketrans("tb", "bt"))
    return (x0, y0, x1, y1), edges


def move_sel(sel: Sel, dx: int, dy: int, w: int, h: int) -> Sel:
    x0, y0, x1, y1 = sel
    dx = min(max(dx, -x0), w - x1)
    dy = min(max(dy, -y0), h - y1)
    return x0 + dx, y0 + dy, x1 + dx, y1 + dy


def ensure_min_size(sel: Sel, min_size: int, w: int, h: int) -> Sel:
    x0, y0, x1, y1 = sel
    if x1 - x0 < min_size:
        x1 = min(w, x0 + min_size)
        x0 = max(0, x1 - min_size)
    if y1 - y0 < min_size:
        y1 = min(h, y0 + min_size)
        y0 = max(0, y1 - min_size)
    return x0, y0, x1, y1


class Selector:
    MIN_SIZE = 40

    def __init__(self, app: "App"):
        self.app = app
        s = app.scale
        with mss.MSS() as sct:
            v = sct.monitors[0]  # 所有显示器拼成的虚拟桌面
            shot = sct.grab(v)
        self.ox, self.oy = v["left"], v["top"]
        self.bright = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        dark = ImageEnhance.Brightness(self.bright).enhance(0.55)

        w, h = shot.size
        self.w, self.h = w, h
        self.tol = round(6 * s)             # 手柄 / 边线的命中容差
        self.handle = max(5, round(6 * s))  # 手柄方块边长
        self.win = borderless(app.root, self.ox, self.oy, w, h, "black")
        self.cv = tk.Canvas(self.win, width=w, height=h, highlightthickness=0, bd=0,
                            cursor="crosshair")
        self.cv.pack()
        self._dark_tk = ImageTk.PhotoImage(dark)
        self.cv.create_image(0, 0, anchor="nw", image=self._dark_tk)
        self._crop_tk = None
        self.crop_item = self.cv.create_image(0, 0, anchor="nw")
        self.rect_item = self.cv.create_rectangle(0, 0, 0, 0, outline=GREEN,
                                                  width=max(2, round(2 * s)), state="hidden")
        self.handle_items = [self.cv.create_rectangle(0, 0, 0, 0, fill=GREEN, outline="white",
                                                      state="hidden") for _ in range(8)]
        self.size_item = self.cv.create_text(0, 0, anchor="sw", fill="white",
                                             font=("Consolas", 10), state="hidden")
        px, pw = app.primary[0] - self.ox, app.primary[2]
        self.hint_item = self.cv.create_text(
            px + pw // 2, 40 * s, fill="white", font=("Microsoft YaHei UI", 12),
            text="拖动鼠标框选要长截图的区域　·　右键 / Esc 退出")

        self.sel: Sel | None = None
        self.mode: str | None = None  # None / 'new' / 'move' / 'resize'
        self.edges = ""
        self.anchor = (0, 0)
        self.sel_at_press: Sel | None = None
        self.toolbar_item = None

        self.cv.bind("<Motion>", self._hover)
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Double-Button-1>", self._double_click)
        self.cv.bind("<Button-3>", self._right_click)
        self.win.bind("<Escape>", lambda e: self.app.quit())
        self.win.bind("<Return>", lambda e: self.sel and self._start_long())
        self.win.focus_force()

    # 鼠标事件 ------------------------------------------------------

    def _hover(self, e):
        if self.mode:
            return
        if self.sel is None:
            self.cv.configure(cursor="crosshair")
        else:
            self.cv.configure(cursor=_CURSORS.get(hit_test(self.sel, e.x, e.y, self.tol), "arrow"))

    def _press(self, e):
        self.cv.itemconfigure(self.hint_item, state="hidden")
        if self.sel is None:
            self.mode = "new"
            self.anchor = (e.x, e.y)
            return
        hit = hit_test(self.sel, e.x, e.y, self.tol)
        if hit is None:
            return  # 已经有选区：框外点击不重新框选
        self._clear_toolbar()
        self.sel_at_press = self.sel
        self.anchor = (e.x, e.y)
        if hit == "move":
            self.mode = "move"
        else:
            self.mode, self.edges = "resize", hit

    def _drag(self, e):
        if self.mode == "new":
            x0, y0 = self.anchor
            x1 = min(max(e.x, 0), self.w)
            y1 = min(max(e.y, 0), self.h)
            self.sel = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        elif self.mode == "move":
            self.sel = move_sel(self.sel_at_press, e.x - self.anchor[0], e.y - self.anchor[1],
                                self.w, self.h)
        elif self.mode == "resize":
            self.sel, self.edges = resize_sel(self.sel, self.edges, e.x, e.y, self.w, self.h)
            self.cv.configure(cursor=_CURSORS[self.edges])
        else:
            return
        self._draw()

    def _release(self, e):
        mode, self.mode = self.mode, None
        if mode is None or self.sel is None:
            return
        x0, y0, x1, y1 = self.sel
        if mode == "new" and (x1 - x0 < self.MIN_SIZE or y1 - y0 < self.MIN_SIZE):
            self._reset()  # 只是点了一下或框得太小：当作没框
            return
        self.sel = ensure_min_size(self.sel, self.MIN_SIZE, self.w, self.h)
        self._draw()
        self._show_toolbar()
        self._hover(e)

    def _double_click(self, e):
        if self.sel and hit_test(self.sel, e.x, e.y, self.tol) == "move":
            self._start_long()

    def _right_click(self, e):
        if self.sel is None:
            self.app.quit()
        else:
            self._reset()

    # 绘制 ----------------------------------------------------------

    def _reset(self):
        self.sel = None
        self.mode = None
        self._clear_toolbar()
        for item in (self.rect_item, self.size_item, self.crop_item, *self.handle_items):
            self.cv.itemconfigure(item, state="hidden")
        self.cv.itemconfigure(self.hint_item, state="normal")
        self.cv.configure(cursor="crosshair")

    def _draw(self):
        x0, y0, x1, y1 = self.sel
        if x1 - x0 < 1 or y1 - y0 < 1:
            return
        self._crop_tk = ImageTk.PhotoImage(self.bright.crop(self.sel))
        self.cv.itemconfigure(self.crop_item, image=self._crop_tk, state="normal")
        self.cv.coords(self.crop_item, x0, y0)
        self.cv.coords(self.rect_item, x0, y0, x1, y1)
        self.cv.itemconfigure(self.rect_item, state="normal")
        cx, cy, r = (x0 + x1) // 2, (y0 + y1) // 2, self.handle // 2
        points = ((x0, y0), (cx, y0), (x1, y0), (x1, cy), (x1, y1), (cx, y1), (x0, y1), (x0, cy))
        for item, (px, py) in zip(self.handle_items, points):
            self.cv.coords(item, px - r, py - r, px + r, py + r)
            self.cv.itemconfigure(item, state="normal")
        ty = y0 - 6 if y0 > 26 else y0 + 22
        self.cv.coords(self.size_item, x0, ty)
        self.cv.itemconfigure(self.size_item, text=f"{x1 - x0} x {y1 - y0}", state="normal")

    def _show_toolbar(self):
        self._clear_toolbar()
        size = round(36 * self.app.scale)
        bar = tk.Frame(self.cv, bg="white")
        IconButton(bar, _icon_long, self._start_long, "长截图", size).pack(padx=4, pady=4)
        x0, y0, x1, y1 = self.sel
        gap = round(8 * self.app.scale)
        bar_h = size + 8
        x = max(x1, size + 8)
        if y1 + gap + bar_h <= self.h:      # 优先放在选区下方
            self.toolbar_item = self.cv.create_window(x, y1 + gap, anchor="ne", window=bar)
        elif y0 - gap - bar_h >= 0:          # 下方放不下放上方
            self.toolbar_item = self.cv.create_window(x, y0 - gap, anchor="se", window=bar)
        else:                                # 都放不下放选区内右下角
            self.toolbar_item = self.cv.create_window(x - gap, y1 - gap, anchor="se", window=bar)

    def _clear_toolbar(self):
        if self.toolbar_item:
            self.cv.delete(self.toolbar_item)
            self.toolbar_item = None

    def _start_long(self):
        x0, y0, x1, y1 = self.sel
        region = (self.ox + x0, self.oy + y0, x1 - x0, y1 - y0)
        self.win.destroy()
        # 冻结画面的遮罩刚销毁，等底下的窗口重绘完再开始抓帧
        self.app.root.after(200, lambda: Session(self.app, region))


# ── 第二步：滚动截图 ─────────────────────────────────────────────────

class Session:
    INTERVAL_MS = 100
    PREVIEW_MS = 250

    def __init__(self, app: "App", region: tuple[int, int, int, int],
                 save_to: str | None = None, duration: float | None = None):
        self.app = app
        self.root = app.root
        self.x, self.y, self.w, self.h = region
        self.sct = mss.MSS()
        self.stitcher = Stitcher()
        self.windows: list[tk.Toplevel] = []
        self.running = True
        self.last_preview = 0.0
        self.preview_version = -1
        self.frames = 0
        self.stitch_ms = 0.0

        self.mon = self._monitor_of_region()
        self._build_mask()
        self._build_border()
        self._build_hint()
        self._build_toolbar()
        self._build_preview()
        for win in self.windows:
            make_passive(win)

        if save_to:
            self.root.after(int(duration * 1000), lambda: self._finish_to_file(save_to))
        self.root.after(0, self._tick)

    # 界面 ----------------------------------------------------------

    def _monitor_of_region(self) -> dict:
        cx, cy = self.x + self.w // 2, self.y + self.h // 2
        for m in self.sct.monitors[1:]:
            if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                return m
        return self.sct.monitors[1]

    def _build_mask(self):
        v = self.sct.monitors[0]
        mask = borderless(self.root, v["left"], v["top"], v["width"], v["height"], "black")
        mask.attributes("-alpha", 0.4)
        mask.update()
        hx, hy = self.x - v["left"], self.y - v["top"]
        punch_hole(mask, v["width"], v["height"], (hx, hy, hx + self.w, hy + self.h))
        self.windows.append(mask)

    def _build_border(self):
        t = max(2, round(2 * self.app.scale))
        x, y, w, h = self.x, self.y, self.w, self.h
        for bx, by, bw, bh in ((x - t, y - t, w + 2 * t, t), (x - t, y + h, w + 2 * t, t),
                               (x - t, y, t, h), (x + w, y, t, h)):
            self.windows.append(borderless(self.root, bx, by, bw, bh, GREEN))

    def _build_hint(self):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        tk.Label(win, text="滚动页面截取更多内容", bg="#2B2B2B", fg="white",
                 font=("Microsoft YaHei UI", 10), padx=10, pady=4).pack()
        win.update_idletasks()
        hh = win.winfo_reqheight()
        gap = round(8 * self.app.scale)
        y = self.y - hh - gap if self.y - hh - gap >= self.mon["top"] else self.y + gap
        x = self.x if self.y - hh - gap >= self.mon["top"] else self.x + gap
        win.geometry(f"+{x}+{y}")
        self.windows.append(win)

    def _build_toolbar(self):
        s = self.app.scale
        size = round(36 * s)
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="white")
        # 固定字符宽度：尺寸文字变长时不能把右侧按钮挤出窗口
        self.status = tk.Label(win, text="", width=13, anchor="e", bg="white", fg="#888888",
                               font=("Microsoft YaHei UI", 9), padx=4)
        self.status.pack(side="left")
        for draw, cmd, tip in ((_icon_save, self.save, "保存"),
                               (_icon_cancel, self.cancel, "取消（Esc）"),
                               (_icon_ok, self.confirm, "完成（复制到剪贴板）")):
            IconButton(win, draw, cmd, tip, size).pack(side="left", padx=2, pady=4)
        win.update_idletasks()
        tw, th = win.winfo_reqwidth() + round(4 * s), win.winfo_reqheight()
        gap = round(10 * s)
        mon_bottom = self.mon["top"] + self.mon["height"]
        x = max(self.mon["left"], self.x + self.w - tw)
        y = self.y + self.h + gap
        if y + th > mon_bottom:  # 下面放不下就放到选区内底部（已设置不被截图）
            y = self.y + self.h - th - gap
        win.geometry(f"{tw}x{th}+{x}+{y}")
        self.windows.append(win)

    def _build_preview(self):
        s = self.app.scale
        self.pw = round(180 * s)
        pad = round(4 * s)
        gap = round(12 * s)
        mon_left, mon_right = self.mon["left"], self.mon["left"] + self.mon["width"]
        x = self.x + self.w + gap
        if x + self.pw + 2 * pad > mon_right:
            x = self.x - gap - self.pw - 2 * pad
        if x < mon_left:
            x = self.x + self.w - self.pw - 2 * pad - gap
        self.preview_max_h = max(self.h, round(200 * s))
        self.preview_max_h = min(self.preview_max_h, self.mon["top"] + self.mon["height"] - self.y - gap)
        self.preview_pad = pad
        self.preview_x = x
        win = borderless(self.root, x, self.y, self.pw + 2 * pad, 10, "white")
        self.preview_cv = tk.Canvas(win, bg="white", highlightthickness=0, bd=0)
        self.preview_cv.pack(padx=pad, pady=pad)
        self._preview_tk = None
        self.preview_win = win
        self.windows.append(win)

    def _refresh_preview(self):
        step = max(1, self.w // self.pw)
        arr = self.stitcher.preview(step)
        if arr is None:
            return
        img = Image.fromarray(np.ascontiguousarray(arr[:, :, ::-1]))
        tw = self.pw
        th = max(1, round(img.height * tw / img.width))
        if th > self.preview_max_h:  # 太长就整体缩小，始终显示完整长图
            tw = max(1, round(tw * self.preview_max_h / th))
            th = self.preview_max_h
        img = img.resize((tw, th), Image.LANCZOS)
        self._preview_tk = ImageTk.PhotoImage(img)
        self.preview_cv.delete("all")
        self.preview_cv.configure(width=tw, height=th)
        self.preview_cv.create_image(0, 0, anchor="nw", image=self._preview_tk)
        p = self.preview_pad
        self.preview_win.geometry(f"{tw + 2 * p}x{th + 2 * p}+{self.preview_x}+{self.y}")
        self.status.configure(text=f"{self.w} × {self.stitcher.total_height()}")

    # 抓帧循环 ------------------------------------------------------

    def _grab(self) -> np.ndarray:
        shot = self.sct.grab({"left": self.x, "top": self.y, "width": self.w, "height": self.h})
        return np.frombuffer(shot.bgra, np.uint8).reshape(shot.height, shot.width, 4)[:, :, :3]

    def _tick(self):
        if not self.running:
            return
        if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            self.cancel()
            return
        t0 = time.perf_counter()
        self.stitcher.push(self._grab())
        self.frames += 1
        self.stitch_ms = (time.perf_counter() - t0) * 1000

        now = time.monotonic()
        if self.stitcher.version != self.preview_version and (
                self.preview_version < 0 or now - self.last_preview >= self.PREVIEW_MS / 1000):
            self.preview_version = self.stitcher.version
            self.last_preview = now
            self._refresh_preview()
        if self.stitcher.capped:
            self.status.configure(text="已达最大高度")
        self.root.after(max(10, self.INTERVAL_MS - int(self.stitch_ms)), self._tick)

    # 收尾 ----------------------------------------------------------

    def _result_image(self) -> Image.Image:
        arr = self.stitcher.result()
        if arr is None:
            arr = self._grab()
        return Image.fromarray(np.ascontiguousarray(arr[:, :, ::-1]))

    def _teardown(self):
        self.running = False
        for win in self.windows:
            win.destroy()
        self.windows.clear()
        self.sct.close()

    def _center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def cancel(self):
        self._teardown()
        self.app.quit()

    def confirm(self):
        img = self._result_image()
        self._teardown()
        try:
            copy_image_to_clipboard(img)
            msg = f"已复制到剪贴板（{img.width} × {img.height}）"
        except Exception as ex:
            msg = f"复制失败：{ex}"
        toast(self.root, msg, self._center(), then=self.app.quit)

    def save(self):
        self.running = False
        img = self._result_image()
        for win in self.windows:
            win.withdraw()
        owner = tk.Toplevel(self.root)  # 置顶的临时父窗口，保证保存对话框在最前
        owner.overrideredirect(True)
        owner.attributes("-topmost", True)
        owner.geometry("1x1+%d+%d" % self._center())
        owner.focus_force()
        path = filedialog.asksaveasfilename(
            parent=owner, title="保存长截图", defaultextension=".png",
            initialfile=f"长截图_{dt.datetime.now():%Y%m%d_%H%M%S}.png",
            filetypes=[("PNG 图片", "*.png"), ("JPEG 图片", "*.jpg"), ("所有文件", "*.*")])
        owner.destroy()
        if not path:  # 取消了保存对话框：回到滚动截图状态继续
            for win in self.windows:
                win.deiconify()
            self.running = True
            self.root.after(self.INTERVAL_MS, self._tick)
            return
        self._teardown()
        try:
            (img.convert("RGB") if path.lower().endswith((".jpg", ".jpeg")) else img).save(path)
        except Exception as ex:
            messagebox.showerror("保存失败", str(ex))
            self.app.quit()
            return
        toast(self.root, f"已保存：{os.path.basename(path)}", self._center(), then=self.app.quit)

    def _finish_to_file(self, path: str):
        """命令行 --save-to：到时间自动保存并退出（脚本/自测用）。"""
        if not self.running:
            return
        img = self._result_image()
        print(f"frames={self.frames} last_stitch={self.stitch_ms:.1f}ms "
              f"size={img.width}x{img.height}", flush=True)
        self._teardown()
        img.save(path)
        self.app.quit()


# ── 入口 ─────────────────────────────────────────────────────────────

class App:
    def __init__(self, args):
        self.root = tk.Tk()
        self.root.withdraw()
        self.scale = self.root.winfo_fpixels("1i") / 96.0
        with mss.MSS() as sct:
            m = sct.monitors[1]
            self.primary = (m["left"], m["top"], m["width"], m["height"])
        if args.region:
            self.root.after(100, lambda: Session(self, args.region, args.save_to, args.duration))
        else:
            self._selector = Selector(self)

    def quit(self):
        self.root.quit()

    def run(self):
        self.root.mainloop()
        self.root.destroy()


def parse_region(text: str) -> tuple[int, int, int, int]:
    x, y, w, h = (int(v) for v in text.split(","))
    return x, y, w, h


def main():
    ap = argparse.ArgumentParser(description="滚动长截图工具")
    ap.add_argument("--region", type=parse_region, metavar="X,Y,W,H",
                    help="跳过框选，直接对该屏幕区域（物理像素）开始长截图")
    ap.add_argument("--save-to", metavar="PATH", help="配合 --region：到时自动保存到该路径并退出")
    ap.add_argument("--duration", type=float, default=8.0, help="配合 --save-to：截取秒数")
    args = ap.parse_args()
    if args.save_to and not args.region:
        ap.error("--save-to 需要配合 --region 使用")
    App(args).run()


if __name__ == "__main__":
    main()
