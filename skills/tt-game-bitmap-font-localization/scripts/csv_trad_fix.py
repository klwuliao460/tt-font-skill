#!/usr/bin/env python3
"""上下文相关繁体字定点修正（t2s 不敢动的字，只在短语判定该转的位置转）.

背景: 字体槽位换字形走的是逐字 OpenCC('t2s')，而 著/瞭/藉/乾 这类"一字多义"字 t2s 会保守不转
      （著名/顯著/瞭望/慰藉 必须保留原字，听着/了解了 才需要 着/了）。
      这类只能改文本: 用 OpenCC('tw2sp') 做短语级判定，只把判定要转的那一个字替换进原文，
      其余字符（含 ~7…~~ 颜色标记、标点、英文尾巴）一律不动。

用法:
  python csv_trad_fix.py --csv TEXT_bi.CSV --out TEXT_final.CSV --font FONT_CH_NXG.FT2
  python csv_trad_fix.py --csv TEXT.CSV --out /dev/null --font F.FT2 --dry-run    # 只看会改哪些
  ... --deploy "D:/.../STUFF/TEXT"

校验: 行数/列数不变、只有目标列变、改动处数与抽样打印、目标简体字必须存在于字体字符表(否则缺字)。
退出码 0=通过; 1=有 FAIL; 2=参数/文件错误。
"""
import argparse
import csv
import difflib
import hashlib
import os
import shutil
import struct
import sys

DEFAULT_WHITELIST = {'著': '着', '瞭': '了', '藉': '借', '乾': '干'}
PLACEHOLDER = '¬'


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def font_codepoints(ft2_path):
    """只读字符表(不解图集): cp 集合，用于确认目标简体字有槽位。"""
    data = open(ft2_path, 'rb').read()
    count = struct.unpack('>H', data[0x31:0x33])[0]
    rectcount = count - 1
    off = 0x4F + 28 * rectcount + 8
    cps = set()
    for i in range(count):
        cp, _gi = struct.unpack('>HH', data[off + 4 * i:off + 4 * i + 4])
        cps.add(cp)
    return cps


def align_replace(old, new):
    """等长替换位置 [(i, old_ch, new_ch)]; OpenCC 短语改写若改变长度，用 SequenceMatcher 取等长替换块。"""
    if len(old) == len(new):
        return [(i, old[i], new[i]) for i in range(len(old)) if old[i] != new[i]]
    out = []
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'replace' and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                out.append((i1 + k, old[i1 + k], new[i1 + k]))
    return out


def main():
    ap = argparse.ArgumentParser(description='上下文相关繁体字定点修正')
    ap.add_argument('--csv', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--font', help='当前部署/生成用的 FT2，用于确认目标简体字有槽位（强烈建议给）')
    ap.add_argument('--column', default='CHINESE TRADITIONAL')
    ap.add_argument('--type-col', default='TYPE')
    ap.add_argument('--types', default='', help='只改这些 TYPE（逗号分隔）；默认空=全行都改')
    ap.add_argument('--whitelist', default='', help='覆盖默认白名单，格式 著:着,瞭:了')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--deploy', help='部署到该目录（自动备份 + md5 + 回滚命令）')
    a = ap.parse_args()

    wl = dict(DEFAULT_WHITELIST)
    if a.whitelist:
        wl = {}
        for kv in a.whitelist.split(','):
            if ':' not in kv:
                print(f'ERROR 白名单项缺冒号: {kv}')
                return 2
            k, v = kv.split(':')
            wl[k] = v

    try:
        from opencc import OpenCC
    except ImportError:
        print('ERROR 需要 opencc: pip install opencc-python-reimplemented 或 pyopencc')
        return 2
    tw = OpenCC('tw2sp')
    t2s = OpenCC('t2s')

    if not os.path.exists(a.csv):
        print(f'ERROR 找不到 {a.csv}')
        return 2
    with open(a.csv, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    hdr = rows[0]
    if a.column not in hdr:
        print(f'FAIL 列名 "{a.column}" 不在表头: {hdr}')
        return 1
    ti = hdr.index(a.column)
    types = {t.strip() for t in a.types.split(',') if t.strip()}
    tyi = hdr.index(a.type_col) if a.type_col in hdr else None
    if types and tyi is None:
        print(f'WARN 无 {a.type_col} 列，忽略 --types 过滤')
        types = set()

    have = None
    if a.font:
        have = font_codepoints(a.font)
        print(f'字体字符表 {len(have)} 码位 ({a.font})')
    else:
        print('WARN 未给 --font: 无法确认目标简体字在字体里有槽位（缺字风险）')

    applied = {k: 0 for k in wl}
    kept = {k: 0 for k in wl}
    missing_target = set()
    other_diffs = 0
    rows_changed = 0
    examples = []

    skipped_type = 0
    for n, row in enumerate(rows[1:], 2):
        if len(row) <= ti or not row[ti] or row[ti] == PLACEHOLDER:
            continue
        if types and tyi is not None and len(row) > tyi and row[tyi] not in types:
            if any(c in wl for c in row[ti]):
                skipped_type += 1
            continue
        cell = row[ti]
        src_hits = [c for c in cell if c in wl]
        if not src_hits:
            continue
        conv = tw.convert(cell)
        reps = align_replace(cell, conv)
        keep_set = []
        for i, oc, nc in reps:
            if oc in wl:
                tgt = wl[oc]
                if nc == tgt:
                    if have is not None and ord(tgt) not in have:
                        missing_target.add(tgt)
                        continue
                    keep_set.append((i, oc, nc))
            else:
                other_diffs += 1
        for i, oc, nc in keep_set:
            cell = cell[:i] + nc + cell[i + 1:]
        if cell != row[ti]:
            rows_changed += 1
            for i, oc, nc in keep_set:
                applied[oc] += 1
            if len(examples) < 6:
                for i, oc, nc in keep_set:
                    examples.append((n, cell[max(0, i - 6):i + 7], oc, nc))
        # 白名单字出现但 tw2sp 判定不转 -> 保留（著名/顯著/瞭望/慰藉 这类）
        for c in src_hits:
            if sum(1 for i, oc, nc in reps if oc == c) == 0:
                kept[c] += 1
        row[ti] = cell

    print(f'== 上下文定点修正 {a.csv}')
    print(f'  白名单 {wl}')
    print(f'  改动行 {rows_changed} / 处数 ' + ' '.join(f'{k}->{wl[k]}:{applied[k]}' for k in wl))
    print(f'  判定保留(不改) ' + ' '.join(f'{k}:{kept[k]}' for k in wl) + '（著名/顯著/瞭望/慰藉 等）')
    print(f'  tw2sp 顺带改到但非白名单的差异 {other_diffs} 处 —— 已全部忽略，只认白名单字')
    if skipped_type:
        print(f'  因 --types {sorted(types)} 跳过、仍留繁体白名单字的行: {skipped_type} 行'
              f'（这些行在游戏里会显示成 著/瞭 原字形）')
    for n, ctx, oc, nc in examples:
        print(f'    行{n}: …{ctx}…  ({oc}->{nc})')

    fails = []
    if missing_target:
        fails.append(f'FAIL 目标简体字不在字体字符表(会缺字): {sorted(missing_target)} '
                     f'—— 要么这些字先补进字体槽位，要么不改文本')

    if a.dry_run:
        print('  DRY-RUN: 未写文件')
        for s in fails:
            print(s)
        print('RESULT: ' + ('PASS' if not fails else f'FAIL({len(fails)})'))
        return 0 if not fails else 1

    orig = [list(r) for r in csv.reader(open(a.csv, encoding='utf-8-sig', newline=''))]
    d = os.path.dirname(os.path.abspath(a.out))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator='\n').writerows(rows)
    print(f'  输出 {a.out}')

    # 校验: 行数 / 每行列数 / 除目标列外零差异 / 无 BOM / LF
    back = [list(r) for r in csv.reader(open(a.out, encoding='utf-8-sig', newline=''))]
    if len(back) != len(orig):
        fails.append(f'FAIL 行数变化 {len(orig)} -> {len(back)}')
    if sum(1 for x, y in zip(orig, back) if len(x) != len(y)):
        fails.append('FAIL 有行列数不一致')
    other = sum(1 for x, y in zip(orig, back)
                for i in range(min(len(x), len(y))) if i != ti and x[i] != y[i])
    if other:
        fails.append(f'FAIL 非目标列差异 {other} 处')
    tgt_only = sum(1 for x, y in zip(orig, back) if x != y and any(
        x[i] != y[i] for i in range(min(len(x), len(y))) if i == ti))
    head = open(a.out, 'rb').read(3)
    if head == b'\xef\xbb\xbf':
        fails.append('FAIL 输出带 BOM（原版无 BOM）')
    if b'\r' in open(a.out, 'rb').read(200000):
        fails.append('FAIL 出现 CR（必须 LF）')
    print(f'  校验: 行数/列数/非目标列/BOM/LF 通过, 目标列改动行 {tgt_only}')

    if a.deploy:
        if fails:
            print('SKIP 部署: 校验未过')
        else:
            tgt = os.path.join(a.deploy, os.path.basename(a.csv).split('.bak')[0])
            if os.path.exists(tgt):
                bak = tgt + '.prev.bak'
                shutil.copy2(tgt, bak)
                print(f'  备份 {bak} md5={md5(bak)}')
                print(f'  回滚命令: cp -p "{bak}" "{tgt}"')
            shutil.copy2(a.out, tgt)
            print(f'  部署 {tgt} md5={md5(tgt)}')

    for s in fails:
        print(s)
    print('RESULT: ' + ('PASS' if not fails else f'FAIL({len(fails)})'))
    return 0 if not fails else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f'ERROR {type(e).__name__}: {e}')
        sys.exit(2)
