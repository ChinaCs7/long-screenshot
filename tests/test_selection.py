# -*- coding: utf-8 -*-
"""框选调整逻辑单元测试（纯几何，不开窗口）。

运行：python -m pytest tests  或  python tests/test_selection.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from long_screenshot import ensure_min_size, hit_test, move_sel, resize_sel  # noqa: E402

SEL = (100, 100, 300, 400)
W, H = 1920, 1080


def test_hit_test_regions():
    assert hit_test(SEL, 100, 100, 6) == "lt"
    assert hit_test(SEL, 303, 402, 6) == "rb"
    assert hit_test(SEL, 200, 97, 6) == "t"
    assert hit_test(SEL, 98, 250, 6) == "l"
    assert hit_test(SEL, 200, 250, 6) == "move"
    assert hit_test(SEL, 50, 50, 6) is None
    assert hit_test(SEL, 310, 250, 6) is None


def test_resize_single_edge():
    assert resize_sel(SEL, "r", 500, 999, W, H) == ((100, 100, 500, 400), "r")
    assert resize_sel(SEL, "t", 0, 20, W, H) == ((100, 20, 300, 400), "t")


def test_resize_corner_crossing_flips_edges():
    sel, edges = resize_sel(SEL, "lt", 350, 450, W, H)  # 左上角拖过了右下角
    assert sel == (300, 400, 350, 450)
    assert edges == "rb"
    sel, edges = resize_sel(sel, edges, 380, 480, W, H)  # 继续拖，手柄跟手
    assert sel == (300, 400, 380, 480)


def test_resize_clamped_to_screen():
    assert resize_sel(SEL, "rb", 5000, 5000, W, H)[0] == (100, 100, W, H)


def test_move_keeps_size_and_stays_on_screen():
    assert move_sel(SEL, 50, 20, W, H) == (150, 120, 350, 420)
    assert move_sel(SEL, -500, -500, W, H) == (0, 0, 200, 300)
    assert move_sel(SEL, 5000, 5000, W, H) == (W - 200, H - 300, W, H)


def test_ensure_min_size():
    assert ensure_min_size((10, 10, 20, 15), 40, W, H) == (10, 10, 50, 50)
    assert ensure_min_size((W - 5, H - 5, W, H), 40, W, H) == (W - 40, H - 40, W, H)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
