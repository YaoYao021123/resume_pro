---
name: resume
description: 岗位针对性简历生成器。当用户提供 JD（岗位描述）、面经或岗位分析，或提出"帮我生成简历"、"针对这个岗位"等请求时触发。自动检查用户数据完整性，分析岗位需求，从经历库中智能筛选内容，生成单页 LaTeX 简历 PDF，自动调优至恰好一页。
license: Apache-2.0
---

# Resume Generation Skill

> **角色分工**：本文件是**执行流程**（按步骤做）；`CLAUDE.md` 是**规则百科**（详细约束），两者互补。遇到细节问题以 `CLAUDE.md` 为准。

---

## Step 0：前置检查

1. 确定活跃人员 → `tools/person_manager.get_active_person_id()`（None = legacy 模式）
2. 读取对应 `profile.md`（legacy: `data/profile.md`）
3. 读取对应 `experiences/` 目录（legacy: `data/experiences/`）

**阻断条件（任一触发则停止，提示用户）：**
- `profile.md` 含 `[YOUR_XXX]` 占位符
- `experiences/` 无有效文件（排除 `_template.md`、`README.md`、空文件）

---

## Step 1：岗位分析

从 JD / 面经中提取：

| 维度 | 提取内容 |
|------|---------|
| 公司 + 岗位 | 如「美团 · 商业分析师」 |
| 岗位类型 | 产品 / 运营 / 技术 / 金融 / 咨询 / 研究 / 其他 |
| 硬技能关键词 | 技术栈、工具、编程语言 |
| 职能关键词 | 数据分析、行业研究、策略规划、商业化… |
| 行业关键词 | 互联网、金融、AI、医疗… |
| 软技能 | 沟通、跨部门协作、自驱… |

**输出**：一段简短的岗位画像（后续所有选择都以此为锚点）。

---

## Step 2：内容匹配与选取

### 2.1 读取所有经历文件

读取 `experiences/*.md`（跳过 `_template.md`、`README.md`、空文件）。
同时检查 `work_materials/{公司名}/` 是否有非空文件 → 有则优先用作内容来源。

### 2.2 分类（严格执行）

| 分类 | 判定条件 | 放入 section |
|------|----------|-------------|
| 实习/工作 | 文件名不含 `研究_`，且标签**不同时**包含 `研究`+`学术` | `\section{实习经历}` |
| 研究/项目 | 文件名含 `研究_`，**或**标签同时含 `研究`+`学术` | `\section{研究经历}` 或 `\section{项目经历}` |

- 大学/学院课题研究 ≠ 实习，禁止放入「实习经历」

### 2.3 选取

| 类型 | 数量 | 说明 |
|------|------|------|
| 实习经历 | **2-4 段** | 核心内容 |
| 研究/项目 | **0-2 段** | JD 要求研究能力时加入 |
| **合计** | **≤ 5 段** | 目标占满整页 |

- 按 JD 关键词 vs 经历标签的**匹配度**排序选取
- **严格时间倒序**排列（最新在前）
- 每段经历**只能出现在一个 section**（去重）

### 2.4 改写 Bullet

**数量**：实习/工作经历每段 2-3 条，最多 4 条；项目/研究经历 1-2 条

**改写规则（逐条必须遵守）：**

| # | 规则 | 示例 |
|---|------|------|
| 1 | 结尾**不加句号** | ✗ `…提供数据支撑。` → ✓ `…提供数据支撑` |
| 2 | **保留量化数据**，不捏造 | ✓ `产出50页报告` ✗ `产出大量报告` |
| 3 | 动词名词**向 JD 靠拢**（不改变事实） | JD 要"商业分析" → bullet 用"商业分析"而非"行业调研" |
| 4 | **结果导向**，不是"负责了什么" | ✗ `负责数据分析` → ✓ `分析…数据，发现…，推动…` |
| 5 | **城市**从经历文件读取，不猜测 | — |
| 6 | **禁止外部过时数据**（营收/CAGR 等） | ✗ `营收294亿元` → ✓ 删除 |
| 7 | **禁止领域专有术语** | ✗ `"三大鸿沟"` `"算力券"` → ✓ 通用描述 |
| 8 | **研究 bullet 面向岗位改写** | ✗ 堆砌 `三重交互/DID/聚类稳健标准误` → ✓ 聚焦数据规模、工具、产出 |
| 9 | **中文引号**用 `"…"` | ✗ `„..."` ✗ `"..."` |
| 10 | **不添加**用户未提及的技能/成果 | — |

### 2.5 选取获奖

从 `profile.md` 的获奖部分中选取，**最多 3 条**：

**优先级**（从高到低）：
1. 与 JD 直接相关（如数学建模 → 数据分析岗）
2. 国际级 / 国家级高含金量奖项
3. 奖学金只保留**最高等级 1 条**

**必须排除**：
- 同类奖学金的低等级（有特等就不要二等）
- 低含金量（三等奖、公益类）
- 与 JD 完全无关的奖项

---

## Step 3：生成 LaTeX

### 3.1 准备输出目录

```bash
OUTPUT_DIR="$(pwd)/output/{公司}_{岗位}_{YYYYMMDD}"
mkdir -p "$OUTPUT_DIR"

# 复制模板（字体用 symlink 节省空间）
TEMPLATE_DIR="$(pwd)/latex_src/resume"
for f in "$TEMPLATE_DIR"/*; do
  [ "$(basename "$f")" = "fonts" ] && continue
  cp -r "$f" "$OUTPUT_DIR/"
done
ln -sf "$TEMPLATE_DIR/fonts" "$OUTPUT_DIR/fonts"
```

### 3.2 写入 .tex

修改 `$OUTPUT_DIR/resume-zh_CN.tex` 或 `$OUTPUT_DIR/resume-en.tex`（按 language=zh|en），各 section 内容来源：

| Section | 来源 | 注意 |
|---------|------|------|
| 头部（姓名/联系方式） | `profile.md` | — |
| 教育背景 | `profile.md` | 按岗位选 4-5 门课程 |
| 实习经历 | Step 2 选定的实习经历 | 时间倒序，按分类放 |
| 研究/项目经历 | Step 2 选定的研究经历 | 可选，0-2 段 |
| 获奖情况 | Step 2.5 选定 | **≤ 3 条** |
| 技能 | `profile.md` | 按岗位调整顺序 |

### 3.3 输出前强制验证 ✅

**写入 .tex 前必须逐项检查，全部通过才能写入：**

- [ ] 获奖 **≤ 3 条**？同类奖学金只留最高 1 条？无低含金量？
- [ ] 经历总段数 **≤ 5**？（实习 2-4 + 研究/项目 0-2）
- [ ] 每段经历**只在一个 section** 中？
- [ ] 所有 bullet **无句号结尾**？
- [ ] **无领域专有术语**？无引号内自定义概念？
- [ ] **无外部过时数据**？（营收/CAGR 等）
- [ ] 研究 bullet **已面向岗位改写**？（无统计方法堆砌）
- [ ] 城市信息**来自经历文件**？
- [ ] 中文引号格式 `"..."` 正确？

> 任一项 ✗ → 修正后重新检查，直到全部 ✓

---

## Step 4：编译

```bash
export PATH="$HOME/Library/TinyTeX/bin/universal-darwin:$PATH"
cd "$OUTPUT_DIR"
xelatex -interaction=nonstopmode resume-zh_CN.tex > /tmp/xelatex_out.txt 2>&1   # zh
# 或
xelatex -interaction=nonstopmode resume-en.tex > /tmp/xelatex_out.txt 2>&1      # en
echo "Exit: $?"
sleep 2
mdls -name kMDItemNumberOfPages resume-zh_CN.pdf
# 或
mdls -name kMDItemNumberOfPages resume-en.pdf
```

- 编译失败 → 检查 `.log`，常见：`&`→`\&`，`%`→`\%`，`_`→`\_`
- 页数 = 1 → 进入 Step 5 填充率检查
- 页数 > 1 → 进入 Step 5 单页调优

---

## Step 5：单页调优 & 填充率检查

### 5.1 超页时：按序精简（每步后重新编译确认页数）

> **注意**：代码 `_tune_overflow()` 自动执行的顺序是：排版压缩（vspace → margins → list spacing）→ 内容删减（research → project → bullets）→ 极端压缩（font size → section spacing）。手动操作时建议先尝试内容精简。

**第一轮：内容精简（手动优先，效果最大）**

| 序号 | 操作 | 说明 |
|------|------|------|
| 1 | 修复悬挂行 | bullet 末尾 1-3 字独占一行 → 删/改尾部冗余 |
| 2 | 精简获奖至 ≤ 3 条 | 删低含金量 |
| 3 | 注释研究/项目经历整块 | 效果 ~60mm |
| 4 | 注释最不相关的一段实习 | 效果 ~60mm |
| 5 | 减少 bullet：3 条 → 2 条 | — |

**第二轮：排版压缩（内容精简后仍溢出）**

| 序号 | 操作 | 修改文件 |
|------|------|---------|
| 6 | `\vspace{-6pt}→-8pt`，`-2pt→-4pt` | `.tex` |
| 7 | `top/bottom 0.5in→0.4in` | `resume.cls` |
| 8 | `itemsep/topsep 0.1em→0.05em` | `resume.cls` |
| 9 | `\titlespacing` `*1.5/*1.3→*1.0/*0.8` | `resume.cls` |
| 10 | `\LoadClass[10pt]→[9.5pt]` | `resume.cls` |

**红线**：字号 ≥ 9pt，页边距 ≥ 0.35in

**每步后必须**：编译 → `mdls -name kMDItemNumberOfPages` → 1 页则**立即停止**

### 5.2 填充率检查

```bash
python3 tools/page_fill_check.py "$OUTPUT_DIR"
```

| 填充率 | 状态 | 处理 |
|--------|------|------|
| 95% ~ 100% | 理想 | 无需调整 |
| < 95% | 偏空 | 增加经历/bullet/间距（详见 CLAUDE.md） |
| > 100% | 溢出 | 回到 5.1 继续精简 |

---

## Step 6：输出汇报

### 6.1 输出到用户

```
✅ 简历已生成
- 岗位：{公司} · {岗位名}
- 路径：output/{...}/resume-zh_CN.pdf 或 output/{...}/resume-en.pdf
- 页数：1 页 | 填充率：XX%

📋 选入经历：
1. {公司A} — 匹配关键词：xxx, xxx
2. {公司B} — 匹配关键词：xxx, xxx
...

🔧 调优记录：（如有）
- ...
```

### 6.2 写入 generation_log.md

保存到 `$OUTPUT_DIR/generation_log.md`，内容：
- 目标岗位 + JD 关键词
- 选入经历 + 选择理由
- Bullet 改写说明
- 调优记录
