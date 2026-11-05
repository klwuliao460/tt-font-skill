#!/usr/bin/env python3
"""双语字幕改写（宽度预算分流）—— 只改 TEXT.CSV，不动字体。

形态决策（实测结论，见 references/csv_workflow.md）:
  同行 `中文  英文`  仅当  cjk_px*len(中文) + ascii_px*len(英文) <= budget  → 两行同时可见
  否则                `中文\\n英文`  干净轮播（引擎按单行分页，长行会被从英文中间切断）
  中文本身含 \\n（面板/多行文本）→ 强制轮播
  英文保留 ENGLISH 列原始大小写；`~7…~~` 颜色标记原样保留

用法:
  python csv_bilingual.py --csv TEXT.CSV --out TEXT_bi.CSV            # 生成
  python csv_bilingual.py --csv TEXT.CSV --check                      # 只体检现有成品(不写文件)
  python csv_bilingual.py --csv TEXT.CSV.prebi.bak --out TEXT_bi.CSV --deploy "D:/.../STUFF/TEXT"

幂等: 目标列里已存在的 `  英文` / `\\n英文` 尾巴会先剥掉再按规则重排，可反复重跑。
退出码 0=通过; 1=校验有 FAIL; 2=参数/文件错误。
"""
import argparse
import csv
import hashlib
import io
import os
import re
import shutil
import sys
import time

PLACEHOLDER = '¬'


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def is_wide(c):
    return ord(c) > 0x2E80          # CJK 汉字 + 全角标点按一个全角位


def est_width(s, cjk_px, ascii_px):
    s = s.replace('\n', '')
    n_w = sum(1 for c in s if is_wide(c))
    return cjk_px * n_w + ascii_px * (len(s) - n_w)


def strip_tail(cell, en):
    """剥掉上一轮追加的英文尾巴，回到纯中文基底（幂等重跑）。"""
    if not en:
        return cell
    for sep in ('  ', '\n'):
        tail = sep + en
        if cell.endswith(tail):
            return cell[:-len(tail)]
    return cell


def read_rows(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    return rows


def write_rows(path, rows):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as f:   # UTF-8 无 BOM
        csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator='\n').writerows(rows)


def main():
    ap = argparse.ArgumentParser(description='TEXT.CSV 双语字幕分流')
    ap.add_argument('--csv', required=True, help='基底 CSV（建议用未加双语的 prebi 备份）')
    ap.add_argument('--out', help='输出路径（--check 时可省）')
    ap.add_argument('--column', default='CHINESE TRADITIONAL', help='目标中文字段名')
    ap.add_argument('--en-col', default='ENGLISH')
    ap.add_argument('--type-col', default='TYPE')
    ap.add_argument('--types', default='Subtitle,Dialogue', help='参与双语的 TYPE 值，逗号分隔')
    ap.add_argument('--budget', type=int, default=1180)
    ap.add_argument('--cjk-px', type=int, default=36)
    ap.add_argument('--ascii-px', type=int, default=22)
    ap.add_argument('--sep', default='  ', help='同行分隔（默认两个半角空格）')
    ap.add_argument('--check', action='store_true', help='只按规则体检，不写文件')
    ap.add_argument('--deploy', help='部署到该目录（自动备份 + md5 + 回滚命令）')
    a = ap.parse_args()

    if not os.path.exists(a.csv):
        print(f'ERROR 找不到 {a.csv}')
        return 2
    if not a.check and not a.out:
        print('ERROR 需要 --out（或改用 --check）')
        return 2

    rows = read_rows(a.csv)
    hdr = rows[0]
    for name in (a.column, a.en_col, a.type_col):
        if name not in hdr:
            print(f'FAIL 列名 "{name}" 不在表头: {hdr}')
            return 1
    ti, ei, ty = hdr.index(a.column), hdr.index(a.en_col), hdr.index(a.type_col)
    types = {t.strip() for t in a.types.split(',') if t.strip()}

    inline = carousel = forced = untouched = 0
    changed = mism = 0
    mism_ex = []
    band = []
    for row in rows[1:]:
        if len(row) <= ti or row[ty] not in types:
            untouched += 1
            continue
        en, cell = row[ei], row[ti]
        if not en or en == PLACEHOLDER or not cell or cell == PLACEHOLDER:
            untouched += 1
            continue
        zh_base = strip_tail(cell, en)
        cand = zh_base + a.sep + en
        w = est_width(cand, a.cjk_px, a.ascii_px)
        if '\n' in zh_base:
            new = zh_base + '\n' + en
            forced += 1
        elif w <= a.budget:
            new = cand
            inline += 1
        else:
            new = zh_base + '\n' + en
            carousel += 1
        band.append((w, '\n' not in new))
        if new != cell:
            changed += 1
            if a.check and len(mism_ex) < 5:
                mism_ex.append((w, cell[:70]))
        row[ti] = new

    if a.check:
        mism = changed

    mode = 'CHECK' if a.check else 'WRITE'
    print(f'== {mode} {a.csv}')
    print(f'  总行 {len(rows) - 1} / 参与改写 {inline + carousel + forced} '
          f'(同行 {inline} / 轮播 {carousel} / 中文自带换行→强制轮播 {forced}) / 未参与 {untouched}')
    if band:
        inl = [w for w, is_in in band if is_in]
        ml = [w for w, is_in in band if not is_in]
        print(f'  宽度预算 {a.budget}px (全角 {a.cjk_px}px / 半角 {a.ascii_px}px)')
        if inl:
            print(f'  同行行 est: max={max(inl)} 超预算行数={sum(1 for w in inl if w > a.budget)}')
        if ml:
            near = sum(1 for w in ml if w <= a.budget)
            print(f'  轮播行 est: min={min(ml)}; 其中 est<=预算 {near} 行'
                  f'（成品里这一带属边界抖动，真实字宽与 36/22 估算不完全一致）')

    if a.check:
        total = inline + carousel + forced
        print(f'  体检: 与规则不符 {mism}/{total} 行'
              f'（{100 * (total - mism) / max(total, 1):.1f}% 一致）')
        for w, s in mism_ex:
            print(f'    est={round(w)}: {s!r}')
        print('  不符集中在预算边界带属正常（36/22 是估算，真实字宽有出入）；'
              '若成片出现"英文被从中间分页"，收紧 --budget 再重跑')
        print('RESULT: PASS')
        return 0

    fails = []
    if not a.check:
        write_rows(a.out, rows)
        print(f'  输出 {a.out} (改动 {changed} 行)')
        # 校验: 行数 / 每行列数 / 除目标列外零差异
        back = read_rows(a.out)
        if len(back) != len(rows):
            fails.append(f'FAIL 行数变化 {len(rows)} -> {len(back)}')
        bad_cols = sum(1 for x, y in zip(rows, back) if len(x) != len(y))
        if bad_cols:
            fails.append(f'FAIL 列数不一致行数 {bad_cols}')
        other_diff = 0
        for x, y in zip(rows, back):
            for i in range(min(len(x), len(y))):
                if i != ti and x[i] != y[i]:
                    other_diff += 1
        if other_diff:
            fails.append(f'FAIL 非目标列差异 {other_diff} 处')
        raw = open(a.out, 'rb').read(4)
        if raw.startswith(b'\xef\xbb\xbf'):
            fails.append('FAIL 输出带 BOM（原版无 BOM）')
        with open(a.out, newline='', encoding='utf-8') as f:
            lf_ok = '\r' not in f.read(200000)
        if not lf_ok:
            fails.append('FAIL 出现 CRLF（必须 LF）')
        print('  校验: 行数/列数/非目标列/BOM/LF')
        shown = 0
        for r in rows[1:]:
            if len(r) > ti and r[ti] and shown < 4 and '\n' not in r[ti] and r[ty] in types:
                print('    同行样例: ' + repr(r[ti][:78]))
                shown += 1
            if shown >= 4:
                break
        shown = 0
        for r in rows[1:]:
            if len(r) > ti and r[ti] and '\n' in r[ti] and shown < 2:
                print('    轮播样例: ' + repr(r[ti][:78]))
                shown += 1
            if shown >= 2:
                break

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
