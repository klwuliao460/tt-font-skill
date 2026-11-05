---
name: tt-game-bitmap-font-localization
description: 汉化 TT Fusion / Traveller's Tales 系游戏（乐高漫威超级英雄2、乐高星战、乐高蝙蝠侠3、侏罗纪世界等）的位图字体与文本。覆盖三条独立工作流：FT2(TNFN+DDS DXT5/BC3) 字体图集的字形替换汉化、TEXT.CSV 多语言表改写与繁简转换、中英双语字幕分流。当出现"字大小不一/粗细不均/缺字/繁体残留/改字体后闪退/进游戏字没了/字幕被切断/原字残影/字腔糊成黑块"，或需要修改 .FT2 字体图集、TEXT.CSV 时使用。普通文本翻译、非 TT 游戏、位图字体之外的字体替换不走本 skill。
---

# TT 游戏位图字体汉化

## 概述

TT 系游戏的中文"汉化"不是换字体文件，而是**在二进制图集里原地重画字形** + **改 TEXT.CSV 文本**。
两条路线的可行边界已被实测钉死：只能做**原地等长修改**，任何"加槽位/加记录/小号字"路线都会闪退或"字没了"。
本 skill 提供：结构速查、度量对齐协议、四个可跑脚本、以及禁止重试清单。

**先读 `references/dead_ends.md`，再动手。** 大纲里已经付过学费的方向不要重新推导。

## 1. 何时使用

用：

- 要改 TT 游戏 `GUI/FONT/FONT_XX_NXG.FT2` 的字形（繁转简、补字、统一粗细/大小）
- 要改 `STUFF/TEXT/TEXT.CSV` 的中文字段、做双语字幕
- 症状：字大小不一、粗细不均、缺字、繁体残留、改完闪退、进游戏"字没了"、字幕被从中间切断、原字残影

不用（走普通做法）：

- 纯文本翻译、不涉及 FT2/TEXT.CSV
- 非 TT 引擎的字体（Unity/UE 的 TTF、BMFont fnt 等）
- 只改字号/行距的 UI 缩放问题

## 2. 开工前必做

1. **定位三件套**（先确认存在，别猜）：
   - 字体：`<游戏目录>/GUI/FONT/FONT_XX_NXG.FT2`（`CH_NXG` = 中文）
   - 文本：`<游戏目录>/STUFF/TEXT/TEXT.CSV`
   - 渲染源 TTF：默认 `C:\Windows\Fonts\NotoSansSC-VF.ttf`
2. **建立备份链**（命名规范，一轮一个，别覆盖）：
   - `X.FT2.bak` = **原版**（永远不动）
   - 失败版本按原因命名：`.v1broken.bak` / `.v2flat.bak` / `.v2bold.bak`
   - 正式基线：`.v2w500.bak`；上一轮：`.prev.bak`
   - CSV 同理：`TEXT.CSV.prebi.bak`（纯中文原版）、`.bi.bak`、`.seq.bak`
3. **记录 md5**（`md5sum` 或 `certutil -hashfile`），把**回滚命令先写在回复里**再改文件：
   `cp -p "<备份>" "<目标>"`
4. **量原版**（决定所有目标数字，跨游戏不能沿用 LMSH2 常量）：
   ```
   python scripts/font_probe.py FONT_XX_NXG.FT2.bak
   ```
   记下 `core_h / core_bot / alpha_h / alpha_bot / 描边环 / 竖笔众数` 与结构偏移。

## 3. 工作流① 字体图集汉化（原地重画字形）

```
font_probe 量原版 → 定目标数字 → font_generate 生成 → 自动自解析验证
→ 目检预览图 → 部署(备份+md5) → 用户进游戏截图
```

1. **定替换对**：从**部署版** TEXT.CSV 取实际出现过的繁体字，`OpenCC('t2s')` 逐字转，
   只保留 `CJK 且 结果为单字且与原字不同 且 码位在字体字符表` 的条目。
   文本码位不动，只把繁体槽位画成简体字形 → 零闪退风险。
   ```
   python scripts/font_generate.py --ft2-in <原版.bak> --text-csv <部署TEXT.CSV> \
          --out FONT_new.FT2 --target-core-h 27 --core-bottom 39 --weight 500 \
          --preview preview.png
   ```
2. **看脚本的自动验证**（表区未变 / 未触碰块差异 0 / 旧字度量不变 / 新字 core_h、core_bot 达标）。
   任何 `FAIL` 先修，**不要部署**。
3. **目检 `preview.png`**：新字与旧字并排，看基线齐不齐、粗细、字腔有没有被描边糊掉。
4. **部署**：加 `--deploy "<游戏目录>/GUI/FONT"`（自动 `.prev.bak` + md5 + 打印回滚命令）。
5. **请用户进游戏截图**。度量与截图冲突时**以截图为准**重新测量，不要争论数字。

关键约束（详见 `references/ft2_format.md`、`references/glyph_model.md`）：

- 只做原地等长修改；插入/追加记录、加高图集、改计数 = 禁止
- 清理区必须覆盖自身 rect，且清理块必须进 BC3 重编码集合（否则原字残影）
- 核心掩膜用 `a2>90` 实心 + 只填 ≤2px 振铃细缝（填全部洞 = 字腔豆腐块）
- 校准基准是**核心墨高**，不是含描边的 alpha 墨高

## 4. 工作流② TEXT.CSV 改写

1. **TYPE 圈定**：字幕 = `TYPE ∈ {Subtitle, Dialogue}`（LMSH2 实测 10544 行）；
   面板类（`Game/Char/Hint/Framework`）支持多行同时，**不要**当字幕处理。
2. **两层转换**（`references/csv_workflow.md` §3）：
   - 逐字层走**字体**（工作流①），文本不动
   - 上下文相关字（著/瞭/藉/乾）走**文本**：
     ```
     python scripts/csv_trad_fix.py --csv TEXT.CSV --out TEXT_final.CSV --font FONT_XX_NXG.FT2
     ```
     用 `tw2sp` 短语判定，只改白名单字；著名/顯著/瞭望/慰藉 自动保留；
     目标简体字**必须**在字体字符表里（脚本会 FAIL）
3. **校验**：行数不变 / 每行列数不变 / 除目标列外零差异 / 无 BOM / LF / 抽样打印。
   `~7…~~` 颜色标记与 `¬` 占位符必须原样。
4. **部署**：先 `cp -p` 备份 + md5，回滚命令先写出来。

## 5. 工作流③ 双语字幕

按 `references/csv_workflow.md` §4 的决策树选形态，**只有第 3 条可行**：

```
python scripts/csv_bilingual.py --csv TEXT.CSV.prebi.bak --out TEXT_bi.CSV --check   # 先看规则命中
python scripts/csv_bilingual.py --csv TEXT.CSV.prebi.bak --out TEXT_bi.CSV --deploy "<...>/STUFF/TEXT"
```

- 宽度预算：`36*len(全角) + 22*len(半角) <= 1180` → 同行 `中文␣␣英文`（同时可见）
- 否则 → `中文\n英文` 干净轮播；中文基底自带 `\n` → 强制轮播
- 英文取 `ENGLISH` 列原始大小写；脚本幂等，可反复重跑（会先剥旧尾巴）
- 体检：`--check` 对已有成品报"与规则不符 N 行"，差异集中在预算边界带属正常
- 游戏里若出现"英文被从中间分页"→ 收紧 `--budget` 重跑；短字幕被无谓拆开 → 放宽

## 6. 操作纪律（不可协商）

1. 任何部署前：`cp -p` 备份并**打印 md5**；**回滚命令先写在回复里**。
2. 闪退/异常：**单变量隔离**（只换文本 或 只换字体，再启动一次）；LMSH2 等游戏本身有间歇问题，
   **先重开一次**排除，再决定回滚。
3. 每轮生成后先**自解析验证**（用同一套解析代码读回成品）再部署 —— 自解析自洽**不代表**游戏认，
   所以还要求表区字节零改动。
4. **用户进游戏截图是唯一真值**；度量数字与截图冲突时以截图为准重新测量。
5. 只有用户明说"改"才动文件；评审类问题（"还有什么要优化"）先给**带数字的优先级清单**，他自己改。

## 7. 故障速查表

| 现象 | 最可能根因 | 先查 | 详见 |
|---|---|---|---|
| 进游戏"字没了" | 表区被动 / 字符表不再 cp 升序 / 加了记录 | 表区字节比对、`font_probe` 升序检查 | ft2_format §7 |
| 启动期闪退 | 文本用了私用区/生僻码位（`U+FF61`、`U+9FE0`） | 单变量隔离：只换文本再启动 | dead_ends §2 |
| 字形位置错、张冠李戴 | 按图集阅读序推位置 | 用坐标表 `0x4F+28*(gi-1)` | dead_ends §6 |
| 字旁边有原字残影 | 清理区没覆盖自身 rect，或清理块没进 BC3 重编码 | `font_probe --compare` 看未触碰差异 | glyph_model §4-3 |
| 口/日/囗 字腔糊成黑块 | 填洞填了全部洞 / halo 被复刻 | 填洞只填 ≤2px 细缝；描边环 ≤3px 空白重开 | glyph_model §4-1,2 |
| 粗细不均 / 与原版不搭 | weight 轴没生效（875/700 过粗）、按 alpha 墨高校准 | `--weight 500`，校准**核心墨高** | glyph_model §3 |
| 字上下跳、基线不齐 | `core_bot` 没对齐 `rect_y+39` | `font_probe` 看 `core_bot` 众数 | ft2_format §6 |
| 黑块 / 脏底 | 非字形像素 RGB 写了 255 | 只写 `comp_a>0` 像素 | glyph_model §4-4 |
| 繁体残留 | 该字不在替换对（t2s 保守）或码位缺槽 | `csv_trad_fix --dry-run` 看白名单命中 | csv_workflow §3 |
| 简体字变方框/缺字 | 目标简体字不在字体字符表 | `csv_trad_fix --font` 会 FAIL | csv_workflow §3 |
| 字幕英文被从中间分页 | 同行超长，引擎按单行分页 | 收紧 `--budget` 重跑分流 | csv_workflow §4 |
| 中英不同屏（轮播） | 用了 `\n` 求同时双行 | 只有同行拼接能同时可见 | dead_ends §3 |
| CSV 游戏里没变化 | 改了交接包副本 / 编码或换行不符 | 以**部署版**文件为准，查 BOM/CRLF | csv_workflow §1 |

## 8. 常量表（LMSH2 实测，换游戏先重量）

`WEIGHT=500`、`TARGET_CORE_H=27`、`CORE_BOTTOM=39`、`OUTLINE_ALPHA=255`、`MAX_CORE_H=36`、
`CAP_TARGET=13`（半号字，路线已废弃）、`BUDGET=1180`、`CJK_PX=36`、`ASCII_PX=22`、
行带周期 57、字形区 48、墨迹 `y+11..y+40`、字符数 3108、坐标表 `@0x4F`、字符表 `@0x1542B`、
DDS `@0x184CF`（fourcc 在 `+84`，像素从 `+128`）。

## Resources

- `references/ft2_format.md` — FT2 二进制结构、偏移公式、**安全边界**（能不能改的判断依据）
- `references/glyph_model.md` — 三层字形模型、度量协议与阈值、四个必踩坑、验证清单
- `references/csv_workflow.md` — TEXT.CSV 格式、两层繁简转换、双语字幕决策树、校验清单
- `references/dead_ends.md` — 禁止重试清单（每条带实测日期与现象）
- `scripts/font_probe.py` — 解析 FT2：结构体检 / 度量总览 / 逐字明细 / 两版对比 / 槽位拼图
- `scripts/font_generate.py` — 字形替换生成器（原地等长 + 块级 BC3 重编码 + 自验证 + 部署备份）
- `scripts/csv_bilingual.py` — 双语字幕宽度预算分流（`--check` 体检 / `--deploy` 部署）
- `scripts/csv_trad_fix.py` — 上下文相关繁体字定点修正（白名单 + 字体槽位存在性校验）

脚本依赖：`python 3.12`、`numpy`、`pillow`、`opencc-python-reimplemented`（或 pyopencc）。
所有脚本：stdout 为 LLM 友好摘要，退出码 0=通过 / 1=有 FAIL / 2=参数或文件错误。
