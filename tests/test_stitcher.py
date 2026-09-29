# -*- coding: utf-8 -*-
"""拼接引擎单元测试：用合成的「长页面」模拟滚动，校验拼出的长图与原页面逐像素一致。

运行：python -m pytest tests  或  python tests/test_stitcher.py
"""
import os
import random
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stitcher import Stitcher  # noqa: E402

W = 420
HEADER_H = 48
FOOTER_H = 60
VIEW_H = 600
BAND_H = VIEW_H - HEADER_H - FOOTER_H


def make_page(height=3200, seed=1):
    """白底、随机长度的「文字行」，中间夹一段 260px 的空白区。"""
    rnd = random.Random(seed)
    img = Image.new("RGB", (W, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    y = 10
    while y < height - 30:
        if 1400 <= y < 1660:  # 空白段
            y += 20
            continue
        x = 16
        # 头像 + 若干「字块」，模拟聊天列表
        d.rectangle([x, y, x + 36, y + 36], fill=(rnd.randrange(60, 200),) * 3)
        tx = x + 48
        for _ in range(rnd.randrange(2, 7)):
            w = rnd.randrange(18, 60)
            d.rectangle([tx, y + 6, tx + w, y + 16], fill=(40, 40, 40))
            tx += w + 6
        d.text((x + 48, y + 22), f"line {y}", fill=(120, 120, 120))
        y += rnd.choice([44, 52, 60])
    return np.asarray(img)[:, :, ::-1].copy()  # 转 BGR，与抓帧格式一致


def make_chrome(h, color, label):
    img = Image.new("RGB", (W, h), color)
    ImageDraw.Draw(img).text((12, h // 3), label, fill=(0, 0, 0))
    return np.asarray(img)[:, :, ::-1].copy()


HEADER = make_chrome(HEADER_H, (230, 245, 236), "Sticky header")
FOOTER = make_chrome(FOOTER_H, (245, 245, 245), "Input box")


def frame_at(page, pos, hover_row=None):
    band = page[pos:pos + BAND_H].copy()
    if hover_row is not None:  # 鼠标悬停高亮：选区内某块区域轻微变灰
        band[hover_row:hover_row + 50] = (band[hover_row:hover_row + 50] * 0.93).astype(np.uint8)
    return np.vstack([HEADER, band, FOOTER])


def run(positions, page, hover=False):
    st = Stitcher()
    rnd = random.Random(7)
    for p in positions:
        hv = rnd.randrange(0, BAND_H - 60) if hover and rnd.random() < 0.5 else None
        st.push(frame_at(page, p, hv))
    return st


def expected(page, max_pos):
    return np.vstack([HEADER, page[:max_pos + BAND_H], FOOTER])


def test_scroll_down_with_uneven_steps():
    page = make_page()
    pos, positions = 0, [0, 0]
    rnd = random.Random(3)
    while pos < len(page) - BAND_H:
        pos = min(pos + rnd.choice([0, 30, 57, 96, 120, 180]), len(page) - BAND_H)
        positions.append(pos)
    st = run(positions, page)
    np.testing.assert_array_equal(st.result(), expected(page, positions[-1]))


def test_scroll_back_up_does_not_duplicate():
    page = make_page()
    positions = [0, 80, 200, 330, 250, 120, 240, 400, 520, 470, 700, 900]
    st = run(positions, page)
    np.testing.assert_array_equal(st.result(), expected(page, max(positions)))


def test_blank_gap_is_crossed():
    """滚过 260px 空白段：空白处歧义帧会被跳过，但最终不应丢行或错位。"""
    page = make_page()
    positions = list(range(1000, 1900, 40))
    st = Stitcher()
    for p in positions:
        st.push(frame_at(page, p))
    got = st.result()
    want = np.vstack([HEADER, page[1000:positions[-1] + BAND_H], FOOTER])
    np.testing.assert_array_equal(got, want)


def test_hover_highlight_tolerated():
    page = make_page()
    positions = [0] + list(range(0, 1200, 60))
    st = run(positions, page, hover=True)
    got = st.result()
    assert got.shape == expected(page, positions[-1]).shape
    # 悬停只改了亮度，结构应一致：固定区与首尾内容逐像素相同
    np.testing.assert_array_equal(got[:HEADER_H], HEADER)
    np.testing.assert_array_equal(got[-FOOTER_H:], FOOTER)


def test_delayed_repaint_is_overwritten():
    """目标窗口先滚动、后补画：滚动后的第一帧底部 30 行还是空白，下一帧才画上。"""
    page = make_page()
    st = Stitcher()
    for p in [0, 64, 128, 192, 256]:
        unpainted = frame_at(page, p)
        unpainted[HEADER_H + BAND_H - 30:HEADER_H + BAND_H] = 255
        st.push(unpainted)
        st.push(frame_at(page, p))  # 补画完成
    np.testing.assert_array_equal(st.result(), expected(page, 256))


def test_in_place_change_before_scroll_does_not_lock_margins():
    """还没滚动时中间某块原地变化（悬停），不能被误判成固定区。"""
    page = make_page()
    st = Stitcher()
    st.push(frame_at(page, 0))
    st.push(frame_at(page, 0, hover_row=200))
    st.push(frame_at(page, 0))
    for p in range(60, 900, 60):
        st.push(frame_at(page, p))
    # 固定区至少包含真实的标题栏/输入框（页面首尾自带的空白行也可能被算进去，不影响结果）
    assert st.top >= HEADER_H and st.bottom >= FOOTER_H and st.top < 100
    np.testing.assert_array_equal(st.result(), expected(page, 840))


def test_scrollbar_thumb_is_not_stamped():
    """复现：长列表的滚动条滑块停在画面上半部分、缓慢下移，一直向下滚。
    修复前长图右侧会留下一串滑块。"""
    page = make_page(height=6000)
    thumb_color = (150, 150, 150)
    x0, x1, thumb_h = W - 10, W - 2, 60

    def with_thumb(p):
        f = frame_at(page, p)
        ty = HEADER_H + round(p / (len(page) - BAND_H) * (BAND_H - thumb_h))
        f[ty:ty + thumb_h, x0:x1] = thumb_color
        return f

    st = Stitcher()
    positions = list(range(0, 2400, 110))
    for p in positions:
        st.push(with_thumb(p))
    got = st.result()
    want = expected(page, positions[-1])
    assert got.shape == want.shape
    content = got[HEADER_H:-FOOTER_H]
    thumb_px = np.all(content[:, x0 - 2:x1 + 2] == thumb_color, axis=2).sum()
    assert thumb_px == 0, f"长图里还残留 {thumb_px} 个滑块像素"
    np.testing.assert_array_equal(got[:, :x0 - 4], want[:, :x0 - 4])


def test_no_scroll_returns_single_frame():
    page = make_page()
    st = run([0, 0, 0], page)
    np.testing.assert_array_equal(st.result(), frame_at(page, 0))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
