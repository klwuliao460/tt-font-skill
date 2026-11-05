#!/usr/bin/env python3
"""TT 位图字体字形替换生成器（原地等长 · 块级 BC3 重编码）。

只做一件事: 把"要换字形的槽位"（如繁体码位槽）用指定 TTF 的简体字形重画, 其余字节原样保留。
表区(头/坐标表/字符表)一律不动 —— 插入/追加记录、加高图集、改计数都是禁止路线(见 references/dead_ends.md)。

用法:
  # 1) 从部署 TEXT.CSV 推繁体->简体替换对, 生成新字体并自解析验证
  python font_generate.py --ft2-in FONT_ORIG.FT2 --text-csv TEXT.CSV --out FONT_new.FT2

  # 2) 复用已有 pairs.json(码位->显示字形)
  python font_generate.py --ft2-in FONT_ORIG.FT2 --pairs pairs.json --out FONT_new.FT2

  # 3) 生成 + 验证通过后部署(自动 cp -p 备份 + md5 + 打印回滚命令)
  python font_generate.py ... --deploy "D:/SteamLibrary/.../GUI/FONT"

度量目标默认取自 LMSH2 实测(见 references/glyph_model.md), 换游戏时用 font_probe.py 先量原版再传参:
  --target-core-h / --core-bottom / --weight / --max-core-h / --outline-alpha

退出码: 0=生成且验证全通过; 1=有 FAIL; 2=参数/文件错误。
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import struct
import sys
import time
from collections import Counter

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from font_probe import TTBitmapFont, GLYPH_H, INK_TH, WHITE_TH   # noqa: E402

E_BIG = 192                  # 大字号渲染基准
GLYPH_ROW = 57               # 行带周期(px)
CAL_CHARS = '國東鐵們說話漢樹極歐戰壓縮鏡驗艦醜'   # 结构多样(横/竖/包围/撇捺)的校准字
TTF_DEFAULT = r'C:\Windows\Fonts\NotoSansSC-VF.ttf'


def log_to(lines):
    def log(s=''):
        lines.append(str(s))
        print(s, flush=True)
    return log


def dilate8(m):
    """8 邻域 1px 膨胀(向量化)。"""
    d = m.copy()
    d[1:, :] |= m[:-1, :]
    d[:-1, :] |= m[1:, :]
    d[:, 1:] |= m[:, :-1]
    d[:, :-1] |= m[:, 1:]
    d[1:, 1:] |= m[:-1, :-1]
    d[1:, :-1] |= m[:-1, 1:]
    d[:-1, 1:] |= m[1:, :-1]
    d[:-1, :-1] |= m[1:, 1:]
    return d


def fill_holes(m, max_thin=2):
    """只填 LANCZOS 缩小的振铃细缝(洞包围盒最小边<=max_thin), 保留真实字腔(口/日/囗内部)。"""
    H, W = m.shape
    openm = ~m
    seen = np.zeros_like(openm)
    holes = []
    for y0 in range(H):
        for x0 in range(W):
            if not openm[y0, x0] or seen[y0, x0]:
                continue
            comp = [(y0, x0)]
            seen[y0, x0] = True
            i = 0
            touches_border = False
            while i < len(comp):
                y, x = comp[i]
                i += 1
                if y in (0, H - 1) or x in (0, W - 1):
                    touches_border = True
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < H and 0 <= nx < W and openm[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        comp.append((ny, nx))
            if not touches_border:
                holes.append(comp)
    out = m.copy()
    for comp in holes:
        ys = [p[0] for p in comp]
        xs = [p[1] for p in comp]
        if min(max(ys) - min(ys) + 1, max(xs) - min(xs) + 1) <= max_thin:
            for y, x in comp:
                out[y, x] = True
    return out


def reopen_tiny_counters(core, ring, max_area=3):
    """描边环封死的窄字腔(连通域<=max_area)去掉邻接描边重新打开; 大字腔保留描边。"""
    openm = ~(core | ring)
    H, W = openm.shape
    seen = np.zeros_like(openm)
    nb = ((1, 0), (-1, 0), (0, 1), (0, -1))
    for y in range(H):
        for x in range(W):
            if not openm[y, x] or seen[y, x]:
                continue
            stack = [(y, x)]
            seen[y, x] = True
            comp = []
            while stack:
                cy, cx = stack.pop()
                comp.append((cy, cx))
                for dy, dx in nb:
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < H and 0 <= nx < W and openm[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if len(comp) <= max_area:
                for cy, cx in comp:
                    for dy, dx in nb:
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < H and 0 <= nx < W:
                            ring[ny, nx] = False
    return ring


def bc3_encode_batch(blocks):
    """(N,4,4,4) uint8 RGBA -> (N,16) BC3 字节, 全向量化(约 14 万块 / 7 秒)。"""
    N = blocks.shape[0]
    a = blocks[:, :, :, 3].reshape(N, 16).astype(np.int32)
    a0 = a.max(axis=1)
    a1 = a.min(axis=1)
    near_pure = (a0 - a1) < 8
    cand = np.stack([a0, a1] + [(7 * a0 - i * (a0 - a1)) // 7 for i in range(1, 7)], axis=1)
    d = np.abs(a[:, :, None] - cand[:, None, :])
    idx = d.argmin(axis=2).astype(np.uint8)
    idx[near_pure] = 0
    a_idx = np.zeros(N, np.uint64)
    for i in range(16):
        a_idx |= idx[:, i].astype(np.uint64) << (3 * i)
    rgb = blocks[:, :, :, :3].reshape(N, 4, 4, 3).astype(np.int32)
    mx = rgb.max(axis=(1, 2))
    mn = rgb.min(axis=(1, 2))
    pure_rgb = (mx - mn).max(axis=1) < 8
    c0 = ((mx[:, 0] >> 3) << 11) | ((mx[:, 1] >> 2) << 5) | (mx[:, 2] >> 3)
    c1 = ((mn[:, 0] >> 3) << 11) | ((mn[:, 1] >> 2) << 5) | (mn[:, 2] >> 3)
    swap = c0 < c1                      # 保证 c0>=c1, 避开 3 色模式
    c0, c1 = np.where(swap, c1, c0), np.where(swap, c0, c1)

    def unpack(v):
        r = (v >> 11) & 0x1F
        g = (v >> 5) & 0x3F
        b = v & 0x1F
        return np.stack([r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2], axis=1)

    cand4 = np.stack([unpack(c0), unpack(c1),
                      (2 * unpack(c0) + unpack(c1)) / 3,
                      (unpack(c0) + 2 * unpack(c1)) / 3], axis=1).astype(np.float32)
    px = rgb.reshape(N, 16, 3)
    d2 = ((px[:, :, None, :] - cand4[:, None, :, :]) ** 2).sum(axis=3)
    idx2 = d2.argmin(axis=2).astype(np.uint8)
    idx2[pure_rgb] = 0
    c_idx = np.zeros(N, np.uint64)
    for i in range(16):
        c_idx |= idx2[:, i].astype(np.uint64) << (2 * i)
    out = np.zeros((N, 16), np.uint8)
    out[:, 0] = a0
    out[:, 1] = a1
    for i in range(6):
        out[:, 2 + i] = (a_idx >> (8 * i)).astype(np.uint8)
    out[:, 8] = (c0 >> 8) & 0xFF
    out[:, 9] = c0 & 0xFF
    out[:, 10] = (c1 >> 8) & 0xFF
    out[:, 11] = c1 & 0xFF
    for i in range(4):
        out[:, 12 + i] = (c_idx >> (8 * i)).astype(np.uint8)
    return out


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def build_pairs_from_csv(csv_path, column, cp2gi, log):
    """部署文本里实际出现的繁体字 -> 简体显示字形(槽位原地换字形, 文本码位不动)。"""
    from opencc import OpenCC
    cc = OpenCC('t2s')
    import csv as _csv
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        rows = list(_csv.reader(f))
    hdr = rows[0]
    if column not in hdr:
        log(f'FAIL 列名 "{column}" 不在表头 (可选: {hdr})')
        return None
    ti = hdr.index(column)
    used = set()
    for r in rows[1:]:
        if len(r) > ti and r[ti]:
            used.update(r[ti])
    han = [c for c in used if 0x4E00 <= ord(c) <= 0x9FFF]
    pairs, missing = {}, []
    for ch in han:
        cp = ord(ch)
        x = cc.convert(ch)
        if len(x) != 1 or x == ch:
            continue
        if cp not in cp2gi:
            missing.append(ch)
            continue
        pairs[cp] = x
    log(f'部署文本汉字 {len(han)} 个 / 需换字形槽位 {len(pairs)} 个 / 字体缺字 {len(missing)}: '
        f'{" ".join(missing[:30])}')
    if missing:
        log('  (缺字=该繁体码位不在字体字符表, 只能改文本或接受缺字, 不能加槽位)')
    return pairs


def main():
    ap = argparse.ArgumentParser(description='TT FT2 字形替换生成器')
    ap.add_argument('--ft2-in', required=True, help='原版(或上一版)FT2, 必须是表区正确的基准')
    ap.add_argument('--out', required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--text-csv', help='部署 TEXT.CSV, 用 t2s 推导替换对')
    g.add_argument('--pairs', help='已有 pairs.json {"码位": "显示字形"}')
    ap.add_argument('--column', default='CHINESE TRADITIONAL')
    ap.add_argument('--ttf', default=TTF_DEFAULT)
    ap.add_argument('--weight', type=float, default=500, help='VF weight 轴(500=LMSH2 实测吻合)')
    ap.add_argument('--target-core-h', type=int, default=27, help='核心墨高校准目标(用 font_probe 量原版)')
    ap.add_argument('--core-bottom', type=int, default=39, help='核心墨底相对 rect_y(=alpha 墨底-1)')
    ap.add_argument('--max-core-h', type=int, default=36, help='带内核心墨高上限')
    ap.add_argument('--outline-alpha', type=int, default=255, help='描边环 alpha(原版实测 255)')
    ap.add_argument('--preview', default='', help='把结果槽位与相邻旧字拼成 PNG 的路径')
    ap.add_argument('--deploy', help='部署到该目录(自动 .prev.bak + md5 + 回滚命令)')
    ap.add_argument('--no-backup-suffix', action='store_true', help='部署时不覆盖 .prev.bak(已有自管备份链时用)')
    a = ap.parse_args()

    lines = []
    log = log_to(lines)
    t0 = time.time()

    if not os.path.exists(a.ttf):
        log(f'FAIL 渲染源 TTF 不存在: {a.ttf}')
        return 2
    font = TTBitmapFont(a.ft2_in)
    info, fails = font.structure_report()
    for s in info:
        log('  ' + s)
    for s in fails:
        log(s)
    if fails:
        log('FAIL 输入字体结构体检未过, 先修输入再生成')
        return 1

    if a.pairs:
        raw = json.load(open(a.pairs, encoding='utf-8'))
        pairs = {int(k): v for k, v in (raw.get('pairs', raw)).items()}
        log(f'从 {a.pairs} 读取替换对 {len(pairs)} 个')
    else:
        pairs = build_pairs_from_csv(a.text_csv, a.column, font.cp2gi, log)
        if not pairs:
            return 1

    arr = font.arr.copy()
    H, W = font.H, font.W
    dds_bytes = bytearray(font.data[font.dds_off:])
    pre_dds_len = font.dds_off

    # ---- 渲染校准: 全局 em 缩放使核心墨高 = target ----
    fnt = ImageFont.truetype(a.ttf, E_BIG)
    try:
        axes = fnt.get_variation_axes() if hasattr(fnt, 'get_variation_axes') else []
    except Exception:
        axes = []
    if axes and a.weight is not None:
        try:
            fnt.set_variation_by_axes([a.weight])
            log(f'渲染源 {os.path.basename(a.ttf)} 可变字体轴={[ax["name"] for ax in axes]} -> weight={a.weight:g}')
        except Exception as e:
            log(f'weight 轴设置失败(按默认字重渲染): {type(e).__name__}: {e}')
    else:
        log(f'渲染源 {os.path.basename(a.ttf)} 无可变轴, 忽略 weight')

    def render_big(ch):
        im = Image.new('L', (E_BIG * 2, E_BIG * 2), 0)
        ImageDraw.Draw(im).text((E_BIG // 2, E_BIG // 4), ch, font=fnt, fill=255)
        a2 = np.array(im)
        ys, xs = np.where(a2 > 60)
        if len(ys) == 0:
            return None
        return a2[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    hs = []
    for c in CAL_CHARS:
        r = render_big(c)
        if r is not None:
            hs.append(r.shape[0])
    if not hs:
        log('FAIL 校准字全部渲染失败, 检查 TTF 是否含 CJK')
        return 2
    H_med = float(np.median(hs))
    scale = a.target_core_h / H_med
    log(f'校准: E_BIG={E_BIG} 渲染墨高中位={H_med:.1f} -> scale={scale:.4f} (目标核心墨高 {a.target_core_h}px)')

    # ---- 行内邻居(清理边界保护) ----
    row_slots = {}
    for gi, (x, y, w) in font.rects.items():
        row_slots.setdefault(y // GLYPH_ROW, []).append((x, x + w, gi))
    for r in row_slots:
        row_slots[r].sort()

    def clear_bounds(x, y, w):
        sl = row_slots.get(y // GLYPH_ROW, [])
        x_lo, x_hi = x - 2, x + w + 2
        for sx, se, _ in sl:
            if se < x and sx > x_lo - 6:
                x_lo = max(x_lo, sx)      # 不越过前邻起点
            if sx > x + w and sx < x_hi + 6:
                x_hi = min(x_hi, se)      # 不越过后邻终点
        return x_lo, x_hi

    touched = set()
    cleared = np.zeros((H, W), bool)
    stats = Counter()
    for ycp, xch in sorted(pairs.items()):
        gi = font.cp2gi.get(ycp)
        if gi is None or gi not in font.rects:
            stats['skip_no_slot'] += 1
            continue
        rx, ry, rw = font.rects[gi]
        if rx + rw > W or ry + GLYPH_H > H:
            stats['skip_oob'] += 1
            continue
        x_lo, x_hi = clear_bounds(rx, ry, rw)
        arr[ry + 1:ry + 50, x_lo:x_hi + 1] = 0        # 清理必须覆盖自身 rect
        cleared[ry + 1:ry + 50, x_lo:x_hi + 1] = True
        big = render_big(xch)
        if big is None:
            stats['skip_norender'] += 1
            continue
        nw = max(1, int(round(big.shape[1] * scale)))
        nh = max(1, int(round(big.shape[0] * scale)))
        a2 = np.array(Image.fromarray(big).resize((nw, nh), Image.LANCZOS))
        nw, nh = a2.shape[1], a2.shape[0]
        if nw > rw + 1:                                # 宽度约束(引擎按 rect 宽裁)
            f2 = (rw + 1) / nw
            a2 = np.array(Image.fromarray(a2).resize((max(1, int(round(nw * f2))),
                                                      max(1, int(round(nh * f2)))), Image.LANCZOS))
            nw, nh = a2.shape[1], a2.shape[0]
            stats['w_clamp'] += 1
        if nh > a.max_core_h:
            f2 = a.max_core_h / nh
            a2 = np.array(Image.fromarray(a2).resize((max(1, int(round(nw * f2))), a.max_core_h),
                                                     Image.LANCZOS))
            nw, nh = a2.shape[1], a2.shape[0]
            stats['h_clamp'] += 1
        pad = 2
        solid = np.zeros((nh + 2 * pad, nw + 2 * pad), bool)
        solid[pad:pad + nh, pad:pad + nw] = a2 > 90    # 实心掩膜(避开振铃缝)
        apad = np.pad(a2, pad)
        filled = fill_holes(solid)
        Hc, Wc = filled.shape
        comp_a = np.zeros((Hc, Wc), np.uint8)
        comp_rgb = np.zeros((Hc, Wc, 3), np.uint8)
        comp_a[filled] = np.where(apad[filled] > 0, apad[filled], 255)
        comp_rgb[filled] = 255                          # 白色核心
        ring = dilate8(solid) & ~filled                 # 1px 近黑描边环
        ring = reopen_tiny_counters(filled, ring)
        comp_a[ring] = a.outline_alpha
        y_bot = ry + a.core_bottom
        y_top = y_bot - (pad + nh) + 1
        px = rx + (rw - nw) // 2 - pad
        px = max(x_lo, min(px, x_hi - Wc + 1))
        if y_top < 0 or y_top + Hc > H or px < 0 or px + Wc > W:
            stats['skip_oob'] += 1
            continue
        region = arr[y_top:y_top + Hc, px:px + Wc]
        m = comp_a > 0                                  # 只写有墨像素, 其余 RGB 留 0
        region[:, :, :3][m] = comp_rgb[m]
        region[:, :, 3][m] = comp_a[m]
        for by in range((ry + 1) // 4, min((ry + 49) // 4, H // 4 - 1) + 1):
            for bx in range(x_lo // 4, x_hi // 4 + 1):
                touched.add((bx, by))
        stats['done'] += 1

    # ---- 只重编码被触碰的 4x4 块 ----
    tb = sorted(touched)
    bx = np.array([b[0] for b in tb], np.int64)
    by = np.array([b[1] for b in tb], np.int64)
    blocks = np.stack([arr[yy * 4:yy * 4 + 4, xx * 4:xx * 4 + 4] for xx, yy in tb])
    enc = bc3_encode_batch(blocks)
    pos = 128 + (by * (W // 4) + bx) * 16
    for i in range(len(tb)):
        dds_bytes[pos[i]:pos[i] + 16] = enc[i].tobytes()
    log(f'替换 {stats["done"]} 槽 / 宽缩 {stats["w_clamp"]} / 高缩 {stats["h_clamp"]} / 跳过 {sum(v for k, v in stats.items() if k.startswith("skip"))}'
        f' / 重编码块 {len(tb)} / 耗时 {time.time() - t0:.0f}s')

    out_bytes = font.data[:pre_dds_len] + bytes(dds_bytes)
    d = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(d, exist_ok=True)
    with open(a.out, 'wb') as f:
        f.write(out_bytes)
    log(f'输出 {a.out} ({len(out_bytes)} 字节, 输入 {len(font.data)})')

    # ---- 自解析验证(用同一套解析代码读回成品) ----
    fails = []
    if len(out_bytes) != len(font.data):
        fails.append(f'FAIL 长度改变 {len(font.data)} -> {len(out_bytes)}')
    if out_bytes[:pre_dds_len] != font.data[:pre_dds_len]:
        fails.append('FAIL 表区(头+坐标表+字符表)字节被改动 —— 必闪退/字没了')
    new = TTBitmapFont(a.out)
    cps = [cp for cp, _ in new.chars]
    if not all(cps[i] <= cps[i + 1] for i in range(len(cps) - 1)):
        fails.append('FAIL 字符表不再 cp 升序')
    new_arr = new.arr
    mask = np.ones((H, W), bool)
    for xx, yy in tb:
        mask[yy * 4:yy * 4 + 4, xx * 4:xx * 4 + 4] = False
    diff = int(((new_arr != arr).any(axis=2) & mask).sum())
    log(f'验证: 未触碰块像素差异={diff} (应=0)')
    if diff:
        fails.append('FAIL 未触碰块发生变化(编码器越界)')
    # 旧字(非替换槽)度量不变
    keep = [cp for cp in font.cjk_cps() if cp not in pairs][:400]
    moved = 0
    for cp in keep:
        m1, m2 = font.metrics(cp), new.metrics(cp)
        if (m1 is None) != (m2 is None) or (m1 and m2 and (m1['core_h'] != m2['core_h']
                                                           or m1['core_bot'] != m2['core_bot'])):
            moved += 1
    log(f'验证: 抽样旧字 {len(keep)} 个, 度量变化 {moved} 个 (应=0; >0 = 清理区夹到邻格)')
    if moved:
        fails.append('FAIL 旧字被夹伤')
    # 新字度量
    ms = [new.metrics(cp) for cp in pairs]
    ms = [m for m in ms if m]
    if ms:
        ch_ = Counter(m['core_h'] for m in ms).most_common(1)[0]
        cb_ = Counter(m['core_bot'] for m in ms).most_common(1)[0]
        ah_ = Counter(m['alpha_h'] for m in ms).most_common(1)[0]
        log(f'验证: 新字 core_h 众数={ch_[0]}(目标 {a.target_core_h}) core_bot 众数={cb_[0]}(目标 {a.core_bottom}) '
            f'alpha_h 众数={ah_[0]} 描边环={Counter((m["ring_above"], m["ring_below"]) for m in ms).most_common(1)[0][0]}')
        if abs(ch_[0] - a.target_core_h) > 1:
            fails.append(f'FAIL 新字核心墨高 {ch_[0]} 偏离目标 {a.target_core_h} —— 重跑前用 font_probe 复核原版度量')
        if cb_[0] != a.core_bottom:
            fails.append(f'FAIL 新字核心墨底 {cb_[0]} != {a.core_bottom} —— 基线不齐, 游戏里会上下跳')
    else:
        fails.append('FAIL 新字无可测度量')

    if a.preview:
        new_sample = list(pairs.keys())[:10]
        old_sample = [cp for cp in font.cjk_cps() if cp not in pairs][:6]
        montage(new, new_sample + old_sample, a.preview)
        log(f'槽位拼图 {a.preview} ({len(new_sample)} 新字 + {len(old_sample)} 旧字并排, NEAREST x6) '
            f'—— 目检基线齐不平齐 / 粗细 / 字腔有没有被描边糊掉')

    for s in fails:
        log(s)

    if a.deploy:
        if fails:
            log('SKIP 部署: 验证未全通过')
        else:
            tgt = os.path.join(a.deploy, os.path.basename(a.ft2_in))
            if os.path.exists(tgt):
                if a.no_backup_suffix:
                    stamp = time.strftime('%Y%m%d-%H%M%S')
                    bak = f'{tgt}.{stamp}.bak'
                else:
                    bak = tgt + '.prev.bak'
                shutil.copy2(tgt, bak)
                log(f'备份 {bak} md5={md5(bak)}')
                log(f'回滚命令: cp -p "{bak}" "{tgt}"')
            shutil.copy2(a.out, tgt)
            log(f'部署 {tgt} md5={md5(tgt)} (源 {md5(a.out)})')
            log('下一步: 进游戏截图确认; 度量与截图冲突时以截图为准重新测量')

    report = os.path.splitext(a.out)[0] + '_report.txt'
    open(report, 'w', encoding='utf-8').write('\n'.join(lines))
    print('RESULT: ' + ('PASS' if not fails else f'FAIL({len(fails)})'))
    return 0 if not fails else 1


def montage(font, cps, out_path, scale=6):
    try:
        lab = ImageFont.truetype(r'C:\Windows\Fonts\msyh.ttc', 24)
    except Exception:
        lab = ImageFont.load_default()
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
        return
    canvas = Image.new('RGBA', (sum(t[1].shape[1] for t in tiles) * scale + 20 * len(tiles),
                                (GLYPH_H + 12) * scale), (35, 35, 35, 255))
    d = ImageDraw.Draw(canvas)
    x = 10
    for ch, crop in tiles:
        im = Image.fromarray(crop).resize((crop.shape[1] * scale, crop.shape[0] * scale), Image.NEAREST)
        canvas.paste(im, (x, 10 * scale), im)
        d.text((x, 2 * scale), ch, font=lab, fill=(255, 220, 80, 255))
        x += crop.shape[1] * scale + 20 * scale
    canvas.save(out_path)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f'ERROR {type(e).__name__}: {e}')
        sys.exit(2)
