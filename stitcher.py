# -*- coding: utf-8 -*-
"""
滚动长截图的拼接引擎（纯 numpy，与界面无关，可单独测试）。

思路
----
选区内连续抓帧，相邻两帧做比较：

1. 行签名：每行横向切成 16 段，取每段平均亮度 → 一帧变成 (高度, 16) 的小矩阵。
   用签名比对比逐像素比对快几十倍，而且对抗锯齿、细微色差不敏感。
2. 固定区域：第一次检测到滚动时，从上往下、从下往上数「两帧完全没变」的行，
   作为吸顶标题栏 / 底部输入框（上限各 1/4 帧高），只让中间的「滚动带」参与拼接。
3. 求偏移：在滚动带里枚举位移 dy（正 = 往下滚，负 = 往上滚），
   打分 = 错位后「对得上的有内容行数」− 2×「对不上的行数」，最高分即本次滚动距离。
   - 按行计数而不是平均误差：鼠标悬停高亮只影响少数几行，不会拖垮判断。
   - 只数有内容的行：大片空白对哪个位移都「对得上」，不能当证据。
   - 多个位移分数接近（整块空白、重复图案）时判为歧义，本帧跳过，等内容更有特征再对齐。
4. 游标：记录滚动带顶部在长图中的位置，往下滚超出末尾长图就变长，
   往上滚超出开头就在前面补行，来回滚动不会重复拼接。
5. 最新帧覆盖：每帧都把整条滚动带写回它在长图中的位置，而不是只追加新露出的行。
   很多窗口是「先滚动、后补画」（图片延迟加载、底部最后一行晚一帧才画），
   只追加会把补画前的半成品永久留在长图里。
6. 固定竖条：滚动条不随内容移动，第 5 条会把每帧的滑块都「盖章」进长图。
   对齐后仍反复对不上的窄竖条就是滚动条，输出时用左侧相邻列补平。
"""
from __future__ import annotations

import numpy as np


class Stitcher:
    BUCKETS = 16          # 每行横向切几段
    SAME_TOL = 2.0        # 行签名差异 < 此值视为这一行没变
    ROW_TOL = 6.0         # 对齐时，行签名差异 > 此值记为「对不上的行」
    MAX_BAD_RATIO = 0.25  # 最佳位移下对不上的行数占重叠区的上限
    AMBIG_MARGIN = 4      # 最佳位移的得分须领先其它位移这么多行，否则视为歧义
    FIXED_COL_MAX_W = 24  # 固定竖条（滚动条）最宽多少像素，更宽的不当滚动条处理

    def __init__(self, max_height: int = 30000):
        self.max_height = max_height
        self.prev: np.ndarray | None = None   # 上一次成功处理的帧 (H, W, 3)
        self.prev_sig: np.ndarray | None = None
        self.top: int | None = None           # 吸顶固定区行数
        self.bottom: int | None = None        # 底部固定区行数
        self.header: np.ndarray | None = None
        self.footer: np.ndarray | None = None
        self.buf: np.ndarray | None = None    # 滚动内容缓冲区，前 content_len 行有效
        self.content_len = 0
        self.cursor = 0                       # 滚动带顶部在内容中的行号
        self.capped = False
        self.version = 0                      # 长图内容每变化一次 +1，供界面判断是否刷新预览
        self.col_hits: np.ndarray | None = None  # 每列「内容已对齐、这一列却对不上」的帧数
        self.fixed_runs: list[tuple[int, int]] = []  # 识别出的固定竖条 [起, 止) 列

    # ── 对外接口 ─────────────────────────────────────────────

    def push(self, frame: np.ndarray) -> str:
        """送入一帧（H×W×3，uint8）。返回处理结果：
        init / same / moved / grow / nomatch"""
        sig = self.signature(frame)
        if self.prev is None:
            self.prev, self.prev_sig = frame, sig
            return "init"

        changed = np.abs(sig - self.prev_sig).max(axis=1) >= self.SAME_TOL
        if not changed.any():
            return "same"

        h = frame.shape[0]
        if self.top is None:
            cap = h // 4
            top = min(int(np.argmax(changed)), cap)
            bottom = min(int(np.argmax(changed[::-1])), cap)
        else:
            top, bottom = self.top, self.bottom

        dy = self._match(self.prev_sig[top:h - bottom], sig[top:h - bottom])
        relocated = False
        if dy is None and self.top is not None:
            # 和上一帧对不上（滚太快、上一帧恰好是空白）：拿新画面去和已拼好的
            # 长图对位。否则上一帧一直不更新，后面的帧离它越来越远，再也接不上
            band_h = h - top - bottom
            lo = max(0, self.cursor - band_h)
            hi = min(self.content_len, self.cursor + 2 * band_h)
            o = self._match(self.signature(self.buf[lo:hi]), sig[top:h - bottom])
            if o is not None:
                dy, relocated = lo + o - self.cursor, True
        if dy is None:
            return "nomatch"

        if self.top is None:
            if dy == 0:
                # 还没滚动过，只是画面原地变化（悬停、延迟加载）：记住最新画面，
                # 但不能据此锁定固定区——原地变化的行不代表「会滚动」
                self.prev, self.prev_sig = frame, sig
                return "moved"
            # 第一次检测到滚动：锁定固定区，并用上一帧的滚动带初始化长图
            self.top, self.bottom = top, bottom
            self.header = self.prev[:top].copy()
            self.footer = self.prev[h - bottom:].copy()
            first_band = self.prev[top:h - bottom]
            self.content_len = len(first_band)
            self.buf = np.empty((max(self.content_len * 4, 2048), *first_band.shape[1:]), np.uint8)
            self.buf[:self.content_len] = first_band
            self.cursor = 0

        band = frame[top:h - bottom]
        band_h = len(band)
        status = "moved"
        if dy != 0 and not relocated:
            self._learn_fixed_columns(self.prev[top:h - bottom], band, dy)
        self.cursor += dy

        if self.cursor < 0:  # 滚到了起点之上：在前面补行
            if not self.capped:
                extra = -self.cursor
                self.buf = np.concatenate([np.empty((extra, *self.buf.shape[1:]), np.uint8), self.buf])
                self.content_len += extra
                status = "grow"
            self.cursor = 0

        end = self.cursor + band_h
        if end > self.content_len:  # 滚过了末尾：长图变长
            if not self.capped:
                self._reserve(end)
                self.content_len = end
                status = "grow"
            else:
                self.cursor = self.content_len - band_h

        # 最新一帧覆盖它所在的整段：目标窗口经常「先滚动、后补画」（延迟加载的
        # 头像图片、文本框补画最后一行），只追加新行会把补画前的半成品永久留下
        self.buf[self.cursor:self.cursor + band_h] = band
        if self.cursor == 0:
            self.header = frame[:top].copy()
        if self.cursor + band_h == self.content_len:
            self.footer = frame[h - bottom:].copy()

        if self.total_height() >= self.max_height:
            self.capped = True

        self.prev, self.prev_sig = frame, sig
        self.version += 1
        return status

    def total_height(self) -> int:
        if self.top is None:
            return 0 if self.prev is None else self.prev.shape[0]
        return self.top + self.content_len + self.bottom

    def parts(self) -> list[np.ndarray]:
        if self.prev is None:
            return []
        if self.top is None:
            return [self.prev]
        return [self.header, self.buf[:self.content_len], self.footer]

    def result(self) -> np.ndarray | None:
        """完整长图（BGR，H×W×3）。"""
        return self._assemble(1)

    def preview(self, step: int) -> np.ndarray | None:
        """按 step 抽行抽列的缩略长图，避免每次都拼一张完整大图。"""
        return self._assemble(step)

    def _assemble(self, step: int) -> np.ndarray | None:
        parts = self.parts()
        if not parts:
            return None
        parts = [p[::step, ::step] for p in parts]
        if len(parts) == 3 and self.fixed_runs:
            parts[1] = self._erase_fixed_columns(parts[1].copy(), step)
        parts = [p for p in parts if len(p)]
        return np.vstack(parts)

    # ── 固定竖条（滚动条）──────────────────────────────────────
    #
    # 滚动条夹在滚动区域里、却不随内容移动。每帧都整段覆盖写回长图，
    # 滑块就会在每帧移出画面的那段里留下一个「印章」，长图右侧出现一串滑块。
    # 识别办法：内容已经按 dy 对齐了，只有某几列窄条还对不上，且反复出现。
    # 处理办法：输出时用竖条左侧紧挨着的那一列像素把它补平。

    def _learn_fixed_columns(self, pb: np.ndarray, nb: np.ndarray, dy: int) -> None:
        band_h, w = pb.shape[:2]
        if dy >= 0:
            a, b = pb[dy:], nb[:band_h - dy]
        else:
            a, b = pb[:band_h + dy], nb[-dy:]
        diff = np.abs(a.astype(np.int16) - b).max(axis=2) > 24
        frac = diff.mean(axis=1)
        local = (frac > 0) & (frac < 0.15)  # 只看「局部」对不上的行，整行变化的是悬停高亮
        if not local.any():
            return
        hit = diff[local].sum(axis=0) >= 8
        if self.col_hits is None:
            self.col_hits = np.zeros(w, np.int32)
        self.col_hits += hit

        mask = self.col_hits >= 2
        # 向两侧各扩 2 列，吃掉滑块的抗锯齿边缘
        grown = mask.copy()
        for k in (1, 2):
            grown[k:] |= mask[:-k]
            grown[:-k] |= mask[k:]
        runs = []
        x = 0
        while x < w:
            if grown[x]:
                x1 = x
                while x1 < w and grown[x1]:
                    x1 += 1
                if x1 - x <= self.FIXED_COL_MAX_W + 4:
                    runs.append((x, x1))
                x = x1
            else:
                x += 1
        self.fixed_runs = runs

    def _erase_fixed_columns(self, arr: np.ndarray, step: int) -> np.ndarray:
        w = arr.shape[1]
        for c0, c1 in self.fixed_runs:
            s0, s1 = c0 // step, min(w, -(-c1 // step))
            if s0 > 0:
                arr[:, s0:s1] = arr[:, s0 - 1:s0]
            elif s1 < w:
                arr[:, s0:s1] = arr[:, s1:s1 + 1]
        return arr

    def _reserve(self, rows: int) -> None:
        if rows > len(self.buf):
            grown = np.empty((max(rows, len(self.buf) * 2), *self.buf.shape[1:]), np.uint8)
            grown[:self.content_len] = self.buf[:self.content_len]
            self.buf = grown

    # ── 内部 ─────────────────────────────────────────────────

    @classmethod
    def signature(cls, frame: np.ndarray) -> np.ndarray:
        gray = frame.astype(np.float32).mean(axis=2)
        h, w = gray.shape
        bw = max(1, w // cls.BUCKETS)
        n = min(cls.BUCKETS, w // bw)
        return gray[:, : bw * n].reshape(h, n, bw).mean(axis=2)

    def _row_diff(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """两组行签名逐行的差异：取各段差异中第二大的，忽略最大的那一段。
        滚动条滑块、小红点这类窄元素只会让一段对不上；真正错位会牵动好几段。"""
        d = np.abs(a - b)
        return np.partition(d, -2, axis=1)[:, -2] if d.shape[1] >= 2 else d[:, 0]

    def _match(self, ref: np.ndarray, ns: np.ndarray) -> int | None:
        """求 ns 相对 ref 的位移 o：ns[i] ≈ ref[i + o]。两者长度可以不同。"""
        r_len, n_len = len(ref), len(ns)
        min_overlap = max(12, min(r_len, n_len) // 5)
        if min(r_len, n_len) < min_overlap + 2:
            return None

        # 「有内容」的行：行内有明暗变化，或与上一行不同。纯空白行对齐不提供信息。
        info = (ref.max(axis=1) - ref.min(axis=1) > self.SAME_TOL)
        info[1:] |= np.abs(ref[1:] - ref[:-1]).max(axis=1) > self.SAME_TOL

        offsets = np.arange(-(n_len - min_overlap), r_len - min_overlap + 1)
        score = np.full(len(offsets), -np.inf, np.float32)
        for k, o in enumerate(offsets):
            i0, i1 = max(0, -o), min(n_len, r_len - o)
            row = self._row_diff(ref[i0 + o:i1 + o], ns[i0:i1])
            ok = row <= self.ROW_TOL
            bad = len(row) - np.count_nonzero(ok)
            if bad > len(row) * self.MAX_BAD_RATIO:
                continue
            # 打分 = 对上的有内容行 − 2×对不上的行；小位移优先作同分决胜
            score[k] = np.count_nonzero(ok & info[i0 + o:i1 + o]) - 2 * bad - abs(o) * 1e-4

        best = int(np.argmax(score))
        if not np.isfinite(score[best]) or score[best] < self.AMBIG_MARGIN:
            return None
        o_best = int(offsets[best])
        far = np.abs(offsets - o_best) > 2
        if far.any() and score[far].max() > score[best] - self.AMBIG_MARGIN:
            return None  # 空白 / 重复内容：多个位移都说得通
        return o_best
