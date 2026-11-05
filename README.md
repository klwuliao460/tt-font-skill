# tt-game-bitmap-font-localization

一个 Qoder / Claude 风格的 Agent Skill：汉化 TT Fusion / Traveller's Tales 系游戏（乐高漫威超级英雄2、乐高星战、乐高蝙蝠侠3、侏罗纪世界等）的**位图字体图集 `.FT2`** 与 **`TEXT.CSV` 文本表**。

所有结论来自 2026-10-31《乐高漫威超级英雄2》实战验证，带实测数字与可复现基线，不是推测：三层字形模型（白色核心 + 1px 近黑描边环 + 软光晕）、原地等长修改的安全边界、插入槽位/私用区码位导致"字没了"与启动闪退、字幕 `\n` 只能轮播、双语同行要靠宽度预算分流。

## 目录

```
skills/tt-game-bitmap-font-localization/
├── SKILL.md
├── references/
│   ├── ft2_format.md      FT2(TNFN+DDS DXT5/BC3) 二进制结构、偏移公式、安全边界
│   ├── glyph_model.md     字形三层模型、度量协议与阈值、四个必踩坑、验证清单
│   ├── csv_workflow.md    TEXT.CSV 格式、两层繁简转换、双语字幕决策树
│   └── dead_ends.md       已证伪路线清单（每条带实测日期与现象，禁止重试）
└── scripts/
    ├── font_probe.py      解析 FT2：结构体检 / 度量总览 / 逐字明细 / 两版对比 / 槽位拼图
    ├── font_generate.py   字形替换生成器（原地等长 + 块级 BC3 重编码 + 自验证 + 部署备份）
    ├── csv_bilingual.py   双语字幕宽度预算分流（--check 体检 / --deploy 部署）
    └── csv_trad_fix.py    上下文相关繁体字定点修正（白名单 + 字体槽位存在性校验）
```

## 安装

把 `skills/tt-game-bitmap-font-localization/` 整个目录放进 Qoder 的用户级 skill 目录（Windows: `%USERPROFILE%\.qoder-cn\skills\`，macOS/Linux: `~/.qoder-cn/skills/`），重启会话或 `/skills reload`，用 `/tt-game-bitmap-font-localization` 触发。

## 依赖

Python 3.12+，`numpy`，`pillow`（需带 DDS/DXT5 解码），`opencc-python-reimplemented`（或 pyopencc）。渲染源默认 `C:\Windows\Fonts\NotoSansSC-VF.ttf`（可变字体，weight 轴设 500），可用 `--ttf` 换。

## 三条独立工作流

1. **字体图集汉化**：`font_probe` 量原版 → `font_generate` 把繁体码位槽原地重画成简体字形（表区零改动，只重编码被触碰的 4×4 BC3 块）→ 自解析验证 → 部署 → 进游戏截图确认。
2. **TEXT.CSV 改写**：按 `TYPE` 圈定可改行 → 两层繁简转换（逐字层走字体、上下文相关字著/瞭/藉/乾 走文本）→ 行数/列数/非目标列/BOM/LF 校验。
3. **双语字幕**：宽度预算分流 `36×全角 + 22×半角 ≤ 1180` → 同行 `中文  英文`（同时可见），否则 `中文\n英文` 干净轮播。

## 已验证到什么程度

- `font_probe.py` 对原版字体输出 `core_h=27 / alpha_h=29 / core_bot=39 / alpha_bot=40 / 描边环 1/1 / 竖笔众数 2`
- `font_generate.py` 生成的字体与 2026-10-31 那版**进游戏验证过的**成品 md5 逐字节相同：`3cab92576377b344e6ccb0040567fd8d`
- `csv_bilingual.py --check` 对部署成品命中 10469/10544 行（99.3%），差异全部落在预算边界带

## 注意

- 只做**原地等长**修改。加记录、加高图集、改计数、私用区码位（`U+FF61`、`U+9FE0`）都已实测证伪，见 `references/dead_ends.md`。
- 脚本会写文件。`--deploy` 前会自动 `cp -p` 备份并打印 md5 与回滚命令；即便如此，**先备份再动**。
- 本仓库只有解析/生成代码与文档，不含任何游戏文件（`.FT2`、`TEXT.CSV` 请从你自己的安装目录取）。
