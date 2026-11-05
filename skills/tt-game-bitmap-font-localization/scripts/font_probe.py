#!/usr/bin/env python3
"""TT 系游戏位图字体（.FT2 / TNFN + DDS DXT5）解析与度量探针。

用法:
  python font_probe.py FONT.FT2                      # 结构体检 + CJK 度量总览
  python font_probe.py FONT.FT2 --chars 東國漢漢       # 逐字度量明细
  python font_probe.py FONT.FT2 --json m.json        # 全量度量落盘（供生成器/复核用）
  python font_probe.py A.FT2 --compare B.FT2         # 两份字体自解析对比（旧字是否完好）
  python font_probe.py FONT.FT2 --preview 東國漢 --out slots.png   # 槽位放大拼图（目检）

退出码: 0=通过, 1=结构检查不过(打印 FAIL 行), 2=文件/参数错误。traceback 已抑制。
"""
import argparse
import io
import json
import struct
import sys
from collections import Counter

import numpy as np
from PIL import Image

BAND = 57          # 行带周期(px)
GLYPH_H = 48       # 行带内字形区高度: y .. y+47
INK_TOP = 11       # 原版墨迹顶相对 rect_y
INK_BOT = 40       # 原版 alpha 墨底相对 rect_y
INK_TH = 128       # alpha 墨迹阈值(排除 alpha20-128 的软光晕)
WHITE_TH = 128     # RGB 判"白色核心"阈值: min(RGB)>128; 用 200 会因 BC1 量化把核心底行算进描边(核心少 1px)


def fbe(data, off):
    return struct.unpack('>f', data[off:off + 4])[0]


def u16(data, off):
    return struct.unpack('>H', data[off:off + 2])[0]


def u32(data, off):
    return struct.unpack('>I', data[off:off + 4])[0]


class TTBitmapFont:
    """原地等长读取: 头 64B 大端 / 坐标表 @0x4F / 字符表 @0x1542B(随字体长度变) / DDS。"""

    def __init__(self, path):
        self.path = path
        self.data = open(path, 'rb').read()
        self.magic = self.data[0x08:0x0C]
        self.header_dds_off = u32(self.data, 0x00) + 4      # 0x00 = DDS 偏移-4
        self.dds_off = self.data.find(b'DDS ')              # 权威定位
        self.count = u16(self.data, 0x31)                    # 0x30 u32 = 字符数<<8
        self.rectcount = self.count - 1
        self.rect_off = 0x4F
        self.char_off = self.rect_off + 28 * self.rectcount + 8
        self.rects = {}          # gi -> (x, y, w)
        o = self.rect_off
        for gi in range(1, self.rectcount + 1):
            self.rects[gi] = (int(round(fbe(self.data, o))),
                              int(round(fbe(self.data, o + 4))),
                              int(round(fbe(self.data, o + 8))))
            o += 28
        self.chars = []          # [(cp, gi)] 原表顺序
        o = self.char_off
        while o < self.char_off + 4 * self.count:
            self.chars.append(struct.unpack('>HH', self.data[o:o + 4]))
            o += 4
        self.cp2gi = dict(self.chars)
        img = Image.open(io.BytesIO(self.data[self.dds_off:])).convert('RGBA')
        self.arr = np.array(img)
        self.H, self.W = self.arr.shape[:2]
        self.alpha = self.arr[:, :, 3]
        self.rgb = self.arr[:, :, :3]

    # ---- 结构检查 ----
    def structure_report(self):
        fails, info = [], []
        info.append(f'magic={self.magic.decode(errors="replace")} '
                    f'header_dds_off=0x{self.header_dds_off:X} dds_off=0x{self.dds_off:X}')
        if self.header_dds_off != self.dds_off:
            fails.append('FAIL 头声明的 DDS 偏移与 b"DDS " 实测不一致')
        info.append(f'字符数={self.count} (u32@0x30=0x{u32(self.data, 0x30):X}) '
                    f'坐标表条数={self.rectcount} @{self.rect_off:X} 字符表@0x{self.char_off:X}')
        cps = [cp for cp, _ in self.chars]
        asc = all(cps[i] <= cps[i + 1] for i in range(len(cps) - 1))
        info.append(f'字符表升序: {asc}（游戏二分查找依赖，重排必须保持升序）')
        if not asc:
            fails.append('FAIL 字符表非 cp 升序 → 游戏查不到字（"字没了"）')
        gis = [g for _, g in self.chars]
        dup = {g: n for g, n in Counter(gis).items() if n > 1}
        info.append(f'gi min={min(gis)} max={max(gis)} 共享 gi 的槽位数={len(gis) - len(set(gis))}'
                    f'（原版本身就有共享 gi，去重基线非 0）')
        if max(gis) > self.rectcount:
            fails.append(f'FAIL gi 越界: max={max(gis)} > rectcount={self.rectcount}')
        info.append(f'图集 {self.W}x{self.H} 行带周期={BAND} 块={self.W // 4}x{self.H // 4}')
        # TT 的 DDS 头是 128B 变体: 标准偏移被平移, fourcc 实测在 dds_off+84('NVTT' 标签后)
        head = self.data[self.dds_off:self.dds_off + 128]
        fourcc = b''
        for k in (b'DXT5', b'DXT3', b'DXT1', b'BC5 ', b'BC4 ', b'BC3 ', b'ATI1', b'ATI2'):
            j = head.find(k)
            if j >= 0:
                fourcc = k
                break
        info.append(f'DDS 头 {len(head)}B fourcc={fourcc.decode(errors="replace").strip()} @+{head.find(fourcc) if fourcc else -1} '
                    f'像素数据从 +128 起 (每块 16B = BC4 alpha 8B + BC1 rgb 8B, 行优先)')
        if fourcc != b'DXT5':
            fails.append(f'FAIL 压缩格式非 DXT5/BC3: {fourcc!r} — BC3 重编码器不可直接复用')
        return info, fails

    # ---- 单字度量 ----
    def region(self, cp, pad_x=1):
        gi = self.cp2gi.get(cp)
        if gi is None or gi not in self.rects:
            return None
        rx, ry, rw = self.rects[gi]
        x0 = max(0, rx - pad_x)
        x1 = min(self.W, rx + rw + pad_x)
        y0 = ry
        y1 = min(self.H, ry + GLYPH_H)
        if x1 - x0 <= 0 or y1 - y0 <= 1:
            return None
        return self.alpha[y0:y1, x0:x1], self.rgb[y0:y1, x0:x1], (rx, ry, rw), y0

    def metrics(self, cp):
        r = self.region(cp)
        if r is None:
            return None
        a, rgb, (rx, ry, rw), y0 = r
        ink = a > INK_TH
        if not ink.any():
            return None
        core = ink & (rgb.min(axis=2) > WHITE_TH)
        if not core.any():
            core = ink
        ys, xs = np.where(ink)
        cys, cxs = np.where(core)
        m = {
            'cp': cp, 'gi': self.cp2gi[cp], 'rect': [rx, ry, rw],
            'alpha_top': int(ys.min()), 'alpha_bot': int(ys.max()),
            'alpha_h': int(ys.max() - ys.min() + 1),
            'alpha_w': int(xs.max() - xs.min() + 1),
            'core_top': int(cys.min()), 'core_bot': int(cys.max()),
            'core_h': int(cys.max() - cys.min() + 1),
            'core_w': int(cxs.max() - cxs.min() + 1),
            'ring_above': int(cys.min() - ys.min()),
            'ring_below': int(ys.max() - cys.max()),
            'alpha_max': int(a.max()), 'core_min_alpha': int(a[core].min()) if core.any() else 0,
            'ink_pixels': int(ink.sum()),
        }
        m['core_bot_abs'] = ry + m['core_bot']
        m['alpha_bot_abs'] = ry + m['alpha_bot']
        m['run_mode'] = stroke_run_mode(core)
        return m

    def cjk_cps(self, lo=0x4E00, hi=0x9FFF):
        return sorted(cp for cp in self.cp2gi if lo <= cp <= hi)


def stroke_run_mode(core_mask, max_run=20):
    """核心掩膜水平游程众数（排除 <=max_run 以外的长横/整行）。"""
    runs = Counter()
    for row in core_mask:
        run = 0
        for v in row:
            if v:
                run += 1
            elif run:
                if run <= max_run:
                    runs[run] += 1
                run = 0
        if run and run <= max_run:
            runs[run] += 1
    return runs.most_common(1)[0][0] if runs else 0


def pct(vals, q):
    if not vals:
        return 0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


def overview(font, cps, sample=1200):
    cps = cps[:sample]
    ms = [m for m in (font.metrics(cp) for cp in cps) if m]
    if not ms:
        return None, ['FAIL 目标字符集无可用度量（码位不在字符表 / rect 无效）']
    def mode(key):
        c = Counter(m[key] for m in ms)
        return c.most_common(3)
    def stat(key):
        v = [m[key] for m in ms]
        return f'min={min(v)} p25={pct(v,.25)} 中位={pct(v,.5)} p75={pct(v,.75)} max={max(v)} 众数={mode(key)[0]}'
    rows = [
        f'样本 {len(ms)}/{len(cps)} 字',
        f'core_h (白色核心墨高, 校准基准): {stat("core_h")}',
        f'alpha_h (含描边环墨高):        {stat("alpha_h")}',
        f'core_bot (核心墨底 vs rect_y): {stat("core_bot")}',
        f'alpha_bot (alpha 墨底 vs rect_y): {stat("alpha_bot")}',
        f'ring_above / ring_below (描边环厚): 中位 {pct([m["ring_above"] for m in ms],.5)} / {pct([m["ring_below"] for m in ms],.5)}',
        f'alpha_max (墨底不透明度): 众数 {mode("alpha_max")[0]}',
        f'核心水平游程众数（竖笔宽）: {Counter(m["run_mode"] for m in ms).most_common(5)}',
        f'core_w: {stat("core_w")}',
    ]
    fails = []
    ch = pct([m['core_h'] for m in ms], .5)
    if ch == 0:
        fails.append('FAIL core_h 中位为 0 — 检查 INK_TH/WHITE_TH 阈值或图集解码')
    return rows, fails


def montage(font, cps, out_path, scale=6, label_font=26):
    tiles = []
    for cp in cps:
        gi = font.cp2gi.get(cp)
        if gi not in font.rects:
            continue
        rx, ry, rw = font.rects[gi]
        crop = font.arr[ry:ry + GLYPH_H, max(0, rx - 2):rx + rw + 2]
        if crop.size:
            tiles.append((chr(cp), crop))
    if not tiles:
        return 0
    lab = ImageFont_path(label_font)
    tw = [t[1].shape[1] * scale for t in tiles]
    canvas = Image.new('RGBA', (int(sum(tw)) + 20 * len(tiles), (GLYPH_H + 14) * scale), (35, 35, 35, 255))
    x = 10
    for ch, crop in tiles:
        im = Image.fromarray(crop).resize((crop.shape[1] * scale, crop.shape[0] * scale), Image.NEAREST)
        canvas.paste(im, (x, 12 * scale), im)
        d = ImageDraw_draw(canvas)
        d.text((x, 2 * scale), ch, font=lab, fill=(255, 220, 80, 255))
        x += crop.shape[1] * scale + 20 * scale
    canvas.save(out_path)
    return len(tiles)


def ImageFont_path(size):
    from PIL import ImageFont
    for p in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\arial.ttf'):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def ImageDraw_draw(canvas):
    from PIL import ImageDraw
    return ImageDraw.Draw(canvas)


def main():
    ap = argparse.ArgumentParser(description='TT .FT2 位图字体探针')
    ap.add_argument('ft2')
    ap.add_argument('--chars', default='', help='逐字明细（字符串）')
    ap.add_argument('--compare', help='与另一份 FT2 对比逐字度量')
    ap.add_argument('--json', help='全量 CJK 度量落盘路径')
    ap.add_argument('--preview', default='', help='槽位拼图字符集')
    ap.add_argument('--out', default='font_preview.png')
    ap.add_argument('--range', default='4E00-9FFF', help='码位区间 hex，如 0020-FFFF')
    ap.add_argument('--sample', type=int, default=1200)
    a = ap.parse_args()

    try:
        font = TTBitmapFont(a.ft2)
    except Exception as e:
        print(f'ERROR 打开失败: {type(e).__name__}: {e}')
        return 2

    print(f'== {a.ft2}')
    info, fails = font.structure_report()
    for s in info:
        print('  ' + s)

    lo, hi = (int(x, 16) for x in a.range.split('-'))
    cps = [cp for cp in font.cjk_cps(lo, hi)] if (lo, hi) == (0x4E00, 0x9FFF) else \
          sorted(cp for cp in font.cp2gi if lo <= cp <= hi)

    if a.chars:
        print('-- 逐字明细')
        for ch in a.chars:
            m = font.metrics(ord(ch))
            if m is None:
                print(f'  {ch} U+{ord(ch):04X}: 无字形/无墨/不在字符表')
                continue
            print(f'  {ch} U+{ord(ch):04X} gi={m["gi"]} rect={m["rect"]} '
                  f'core_h={m["core_h"]} core_bot={m["core_bot"]} '
                  f'alpha_h={m["alpha_h"]} alpha_bot={m["alpha_bot"]} '
                  f'ring={m["ring_above"]}/{m["ring_below"]} 竖笔={m["run_mode"]}')

    print('-- CJK 度量总览')
    rows, f2 = overview(font, cps, a.sample)
    if rows:
        for s in rows:
            print('  ' + s)
    fails += f2 or []

    if a.compare:
        print(f'-- 对比 {a.compare}')
        try:
            other = TTBitmapFont(a.compare)
        except Exception as e:
            print(f'  ERROR 打开失败: {type(e).__name__}: {e}')
            return 2
        common = sorted(set(font.cp2gi) & set(other.cp2gi))
        changed = []
        for cp in common:
            m1, m2 = font.metrics(cp), other.metrics(cp)
            if m1 is None or m2 is None:
                if (m1 is None) != (m2 is None):
                    changed.append((cp, 'one-side-empty'))
                continue
            if m1['rect'] != m2['rect'] or abs(m1['core_h'] - m2['core_h']) > 1 \
                    or abs(m1['core_bot'] - m2['core_bot']) > 1:
                changed.append((cp, f'{m1["core_h"]}/{m1["core_bot"]} -> {m2["core_h"]}/{m2["core_bot"]}'))
        print(f'  共同码位 {len(common)}, 度量变化 {len(changed)} 字'
              f'（未改动的旧字应只来自替换槽位；范围外出现变化=清理区越界夹到邻格）')
        for cp, why in changed[:20]:
            print(f'    U+{cp:04X} {chr(cp)}: {why}')
        if len(changed) > 20:
            print(f'    ... 其余 {len(changed) - 20} 字（--json 或提高上限查看）')
        head_same = font.data[:font.dds_off] == other.data[:other.dds_off]
        print(f'  表区(头+坐标表+字符表)字节完全一致: {head_same}')

    if a.json:
        out = {}
        for cp in cps:
            m = font.metrics(cp)
            if m:
                out[str(cp)] = m
        json.dump(out, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False)
        print(f'-- 度量已落盘 {a.json} ({len(out)} 字)')

    if a.preview:
        n = montage(font, list(a.preview), a.out)
        print(f'-- 槽位拼图 {a.out} ({n} 格, NEAREST x6)')

    for s in fails:
        print(s)
    return 1 if fails else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f'ERROR {type(e).__name__}: {e}')
        sys.exit(2)
