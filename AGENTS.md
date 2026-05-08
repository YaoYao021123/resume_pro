# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Build, Test, and Lint Commands

```bash
# Start Web UI (stdlib HTTP server, zero dependencies)
python3 web/server.py                          # default port 8765
python3 web/server.py --port 9000              # custom port

# Generate resume from CLI
python3 tools/generate_resume.py --person default 'JD text here'
python3 tools/generate_resume.py 'JD text here'  # uses active person

# Page fill check on generated output
python3 tools/page_fill_check.py output/<person_id>/<company>_<role>_<YYYYMMDD>

# Lint (no formal linter configured; use py_compile as syntax check)
python3 -m py_compile tools/*.py web/server.py

# Tests (stdlib unittest)
python3 -m unittest discover -s tests -p 'test_*.py' -v           # full suite
python3 tests/test_import_resume_parser.py                         # single file
python3 tests/test_import_resume_parser.py ImportResumeParserTests.test_parse_resume_text_extracts_basic_fields -v  # single test

# Backend tests (FastAPI auth/billing, requires fastapi+uvicorn)
python3 -m unittest discover -s backend/auth_billing_service/tests -p 'test_*.py' -v

# Start auth/billing backend (optional)
python3 -m uvicorn backend.auth_billing_service.main:app --host 0.0.0.0 --port 8080

# LaTeX compilation (must use xelatex, not pdflatex)
export PATH="$HOME/Library/TinyTeX/bin/universal-darwin:$PATH"  # macOS
xelatex -interaction=nonstopmode resume-zh_CN.tex
```

## Architecture

Four-layer system: **Data → Generation → Web/API → Layout/Quality**.

**Data layer** (`tools/person_manager.py` + `data/persons.json`): Multi-person registry. Each person gets isolated `data/{id}/` (profile.md, experiences/, work_materials/) and `output/{id}/`. Legacy single-person mode (no `persons.json`) auto-migrates via `tools/migrate_to_multi_person.py`.

**Generation engine** (`tools/generate_resume.py`, ~2600 lines): Core pipeline — profile/experience loading → JD keyword extraction → relevance matching → bullet rewriting → LaTeX rendering → XeLaTeX compile → single-page tuning. Supports 10 AI model providers (OpenAI, Gemini, Anthropic, GLM, Kimi, MiniMax, Grok, Qwen, Doubao, other) configured via `tools/model_config.py` and `.env.local`. Falls back to local rule engine when AI is disabled.

**Web/API layer** (`web/server.py` ~3700 lines + `web/index.html` ~7000 lines): Python stdlib `http.server` — no Flask/Django. Single-file SPA frontend with no build step. Exposes REST APIs for person/profile/experience CRUD, resume generation, gallery, LaTeX editor with compile/versioning, and resume import/parse flow. Optional auth/billing enforcement via `AUTH_BILLING_ENFORCE=1` + FastAPI backend at `backend/auth_billing_service/`.

**Layout/Quality layer** (`latex_src/resume/` + `tools/page_fill_check.py`): LaTeX templates (`.cls` + `.tex`) copied to output per run. `page_fill_check.py` injects measurement code into `.tex`, compiles, reads `.aux` for fill ratio, advises on tuning, then cleans up.

**Chrome extension** (`extension/`): MV3 extension for auto-filling job application forms (Workday, Greenhouse, Lever, SmartRecruiters, LinkedIn) using resume data. Platform-specific adapters in `extension/adapters/`. Communicates with web server `/api/ext/*` endpoints. SQLite backend via `tools/ext_db.py`.

### Key conventions

- Zero core Python dependencies — web server and all tools run on stdlib only. Backend requires `fastapi` + `uvicorn`.
- XeLaTeX required (not pdflatex) for Chinese font support via TinyTeX.
- AI config sources must stay consistent: `AGENTS.md`, `skills/resume-gen/SKILL.md`, `.Codex/agents/resume-generator.md`, and `tools/generate_resume.py` business rules. Update all together when behavior changes.
- Experience classification is strict: intern/work vs research experiences go in separate LaTeX sections, never mixed. Each experience appears in exactly one section.
- Selection caps: total experiences ≤ 5, awards ≤ 3. Bullets: 2-3 per experience (max 4), no trailing punctuation, no fabricated data.

---

# Resume Generator Pro — 配置说明

你是一个岗位针对性简历生成 Agent。根据用户提供的 JD（招聘描述）或面经，从用户的个人经历库中智能筛选最相关内容，生成**精准匹配、单页 LaTeX 排版**的简历 PDF。

---

## 核心工作流

当用户发来 JD 或面经时，**立即执行 `/resume` skill**，无需额外确认。

**六步流程：**

1. **前置检查** — 确认活跃人员的 `profile.md` 和 `experiences/` 已填写，否则引导用户先完成设置
2. **分析岗位** — 提取 JD 核心关键词（技术栈、职能、行业、软技能）
3. **匹配内容** — 从经历库中选出最相关的经历，决定展示顺序
4. **生成 LaTeX** — 基于模板生成 `.tex` 文件，改写 bullet 对齐 JD
5. **编译 & 调优** — 运行 xelatex 编译，确保输出在一页内
6. **填充率检查** — 运行 `tools/page_fill_check.py` 检测页面填充率，根据建议自动调优

---

## 文件路径索引

| 资源 | 路径 |
|------|------|
| 人员注册表 | `data/persons.json` |
| 共享模板 | `data/_shared/experiences/` |
| 个人基本信息 | `data/{person_id}/profile.md` |
| 经历详情文件 | `data/{person_id}/experiences/` |
| 原始工作材料 | `data/{person_id}/work_materials/` |
| 人员管理模块 | `tools/person_manager.py` |
| 迁移脚本 | `tools/migrate_to_multi_person.py` |
| LaTeX 模板 | `latex_src/resume/` |
| 输出目录 | `output/{person_id}/` |
| 填充率检查工具 | `tools/page_fill_check.py` |
| Web 数据管理 UI | `web/server.py` + `web/index.html` |
| Skill | `skills/resume-gen/SKILL.md` |

> **Legacy 模式**：如果 `data/persons.json` 不存在，仍兼容旧路径 `data/profile.md` + `data/experiences/`。首次运行时自动迁移。

---

## 前置检查规则

在生成简历之前，必须检查用户数据是否完整：

### 检查 profile.md
读取活跃人员的 `profile.md`（路径通过 `tools/person_manager` 获取，legacy 模式为 `data/profile.md`），确认以下字段已替换（不再含 `[YOUR_XXX]` 占位符）：
- 姓名、邮箱、电话
- 至少一段教育背景
- 至少一项技能

**如果未填写**，停止生成流程，输出：
```
⚠️ 请先完成个人信息设置：
1. 打开 data/profile.md
2. 将所有 [YOUR_XXX] 占位符替换为你的真实信息
3. 完成后重新发送 JD

详细说明请参考 SETUP.md
```

### 检查 experiences/
读取活跃人员的 `experiences/` 目录，排除 `_template.md` 和 `README.md`，检查是否存在至少一个有效经历文件。

**如果为空**，停止生成流程，输出：
```
⚠️ 请先添加至少一段经历：
1. 复制 data/experiences/_template.md
2. 重命名为 01_公司名.md 并填写
3. 完成后重新发送 JD

详细说明请参考 SETUP.md
```

---

## 内容读取规则

### 个人信息
从活跃人员的 `profile.md` 读取姓名、邮箱、电话、教育背景、技能、获奖情况、项目经历、论文发表。

### 经历内容（优先级）
1. **原始工作材料**（`data/{person_id}/work_materials/{公司名}/` 下的非空文件）— 最优先
2. **经历详情文件**（`data/{person_id}/experiences/{公司名}.md`）— 次优先

读取时：
- 跳过空文件
- 不要修改原始文件内容
- 只在生成的 `.tex` 中改写 bullet

---

## 候选人画像（Candidate Portrait）

AI 模型在选择经历之前，需先构建候选人画像（STRICT_AI_RULES Rule 0）：

1. **`candidate_portrait`**：2-3 句话描述「这个岗位在找什么样的人」（能力特质 + 背景偏好 + 产出期望）
2. **`core_demands`**：3-5 条核心诉求（完整句子，而非单词清单）

基于画像整体判断哪些经历最能证明候选人匹配，而不是逐词匹配 JD 关键词。画像写入 `generation_log.md` 并用于补充关键词集合。

---

## 经历匹配规则

### 经历分类（严格执行）

根据经历文件的**文件名前缀**和**标签**将经历分入不同 LaTeX section，**禁止混放**：

| 分类 | 判定条件 | 放入 section |
|------|----------|-------------|
| 实习/工作 | 文件名不含 `研究_`，且标签中**不同时**包含 `研究` + `学术` | `\section{实习经历}` |
| 研究 | 文件名含 `研究_`，**或**标签同时包含 `研究` + `学术` | `\section{研究经历}` 或 `\section{项目经历}` |
| 项目 | profile.md 中的项目经历 | `\section{项目经历}` |

**关键规则：**
- 大学/学院内的课题研究 **不是实习**，必须放入「研究经历」或「项目经历」
- 同一段经历**只能出现在一个 section**，严禁重复（如已在实习经历中出现，不得再出现在项目经历中）

### 展示数量
- 实习经历选择 **2-4 段**（核心内容）
- 研究/项目经历选择 **0-2 段**（JD 要求研究能力时加入）
- **合计不超过 5 段经历**，代码会自动补足至最少 3 段，根据内容填充程度决定，目标占满整页
- **严格按时间倒序排列**（最新经历在最前）

### 去重规则（强制）
生成 `.tex` 前必须检查：如果一段经历已放入「实习经历」section，则**不得**在「项目经历」或「研究经历」中再次出现，反之亦然。每段经历只出现一次。

### 相关性排序
根据 JD 提取的关键词与经历文件中的**标签**做匹配：
- 高度匹配：标签与 JD 关键词重叠 3 个以上 → 优先展示
- 部分匹配：标签与 JD 行业/职能一致 → 考虑展示
- 低匹配：与 JD 完全无关 → 排后或不展示

### 通用匹配思路
- **技术/互联网/AI 岗位**：优先有技术工具、数据分析、产品运营标签的经历
- **金融/咨询/研究岗位**：优先有研究、分析、建模、财务标签的经历
- **销售/商务/运营岗位**：优先有客户管理、增长、指标达成标签的经历
- **研究经历**：JD 明确要求研究/学术能力时加入，超页时优先删除

---

## Bullet 改写原则

- **实习/工作经历 2-3 条** bullet，最多 4 条；**项目/研究经历 1-2 条**
- **保留量化数据**（百分比、数量、时间、金额）— 不捏造
- **动词和名词向 JD 关键词靠拢**（但不改变事实）
- **结果导向**：描述你做了什么并产生了什么结果，而不是"负责了什么"
- **不添加**用户未在经历文件中提及的技能或成果
- **Bullet 结尾不加句号**（中文句号 `。` 和英文句号 `.` 均不加）
- **城市信息必须从经历文件中读取**，不得自行猜测或默认
- **禁止出现过时/过细的外部数据**：如被研究公司的具体营收额、净利润、行业 CAGR 等第三方数据。这类数据会过时且面试时易被追问。只保留**用户自身工作产出的量化数据**（如产出报告数量、效率提升百分比、覆盖范围等）
- **禁止领域专有术语/特定定义**：不使用仅在特定项目或行业内部通用的术语（如"三大鸿沟"、"算力券"等）。简历内容应使用通用、易理解的表述，避免面试时被追问术语含义。如原文含此类术语，改写为通用描述
- **中文引号使用规范**：使用正确的中文左右引号 `"..."` （Unicode U+201C / U+201D），不使用全角下引号 `„..."`（U+201E）或其他错误引号形式
- **研究/项目经历同样适用以上所有规则**：研究项目的 bullet 需**面向目标岗位改写**，避免堆砌统计方法名称和学术术语（如"三重交互"、"聚类稳健标准误"、"DID方法"等），聚焦可迁移能力（数据处理规模、使用的工具/语言、量化研究产出）。面试官看的是你的能力，不是你的研究课题细节

---

## 获奖情况规则

- **最多展示 3 条**获奖记录，严格筛选
- **筛选优先级**（从高到低）：
  1. 与 JD 岗位直接相关的奖项（如数学建模之于数据分析岗）
  2. 高含金量 / 国家级 / 国际级奖项
  3. 奖学金中只保留最高等级的 1 条
- **不展示**重复性质的奖项（如同类奖学金多年获得，只保留最高等级 1 条）
- **不展示**与 JD 完全无关的低含金量奖项（如公益奖学金、三等奖等）

---

## LaTeX 模板规范

### 文件结构
```
latex_src/resume/
├── resume-zh_CN.tex   ← 中文模板
├── resume-en.tex      ← 英文模板
├── resume.cls         ← 文档类（定义排版命令）
├── zh_CN-Adobefonts_external.sty
├── linespacing_fix.sty
└── fonts/
```

### 关键排版参数（默认值）
在 `resume.cls` 中：
- 页边距：`left=0.65in, right=0.65in, top=0.5in, bottom=0.5in`
- 主字号：`\LoadClass[10pt]{article}`
- section 间距：`\titlespacing*{\section}{0cm}{*1.5}{*1.3}`
- itemize 间距：`topsep=0.1em, itemsep=0.1em`

在 `.tex` 中：
- `\vspace{-8pt}` — 头部间距
- `\vspace{-6pt}` — 每段工作开头
- `\vspace{-2pt}` — 每段工作结尾

### 编译命令
```bash
# 需设置 xelatex 路径（TinyTeX 示例）
export PATH="$HOME/Library/TinyTeX/bin/universal-darwin:$PATH"
# 或 Linux/WSL：
# export PATH="$HOME/.TinyTeX/bin/x86_64-linux:$PATH"

cd /path/to/output/dir/
# 中文
xelatex -interaction=nonstopmode resume-zh_CN.tex
# 英文
xelatex -interaction=nonstopmode resume-en.tex
```

---

## 单页调优策略

超出一页时，代码 `_tune_overflow()` **按以下优先级顺序自动调整**，每次改一项后重新编译检查填充率，一旦 ≤ 100% 即停止：

**第一轮：排版压缩（快速微调，通常足够）**

1. **缩小 `\vspace`**：`-6pt → -8pt`，`-2pt → -4pt`
2. **缩小页边距**：`top=0.5in → 0.4in`，`bottom=0.5in → 0.4in`（`resume.cls`）
3. **缩小列表间距**：`itemsep/topsep → 更小值`（`resume.cls`）

**第二轮：内容删减（排版压缩不够时）**

4. **删除研究经历 section**：注释掉整个研究经历
5. **删除项目经历 section**：注释掉整个项目经历
6. **减少 bullet 数量**：每段经历从 3 条减到 2 条

**第三轮：极端压缩（前两轮仍溢出时）**

7. **缩小字号**：`10pt → 9.5pt → 9pt`（`resume.cls`）
8. **缩小 section 间距**：`*1.5/*1.3 → *1.0/*0.8`

**手动辅助（代码未自动执行，需人工介入时参考）：**
- 修复悬挂行（Widow Line）：找出 bullet 末尾仅有 1-3 个字独占一行的情况，改写使文字收紧
- 精简获奖至 ≤3 条（生成阶段已执行 `_filter_awards`，通常不需要额外操作）
- 删减最不相关的一段实习经历

**强制检查：每步操作后必须重新编译并检查填充率。若已 ≤ 100% 则停止调优。**

> **红线**：字号不低于 9pt，上下页边距不低于 0.35in

---

## 页面填充率检查

编译后必须运行填充率检查，确保简历页面内容饱满：

### 使用方式
```bash
python3 tools/page_fill_check.py <output_dir> [xelatex_path]
```

### 检查原理
在 `.tex` 的 `\begin{document}` 后注入 `\pagetotal` / `\pagegoal` 测量代码，编译后从 `.aux` 文件读取内容高度和可用高度，计算填充率。检查完成后自动清理注入代码并重新编译还原干净 PDF。

### 填充率阈值
| 填充率 | 状态 | 处理 |
|--------|------|------|
| 99% ~ 100% | 排版饱满 | 无需调整 |
| 95% ~ 99% | 理想范围 | 无需调整 |
| < 95% | 偏空 | 需补充内容或扩大间距 |
| > 100% | 溢出 | 按「单页调优策略」缩减 |

### 偏空时的充实策略（按优先级）
1. 增加一段经历（实习/项目/研究），每段 ~60mm
2. 加入项目经历或论文发表 section
3. 给现有经历增加 bullet（每段最多 4 条）
4. 展开 bullet 描述，补充量化数据
5. 增大列表间距 / section 间距 / 页边距

---

## 生成最终检查清单（强制）

在输出 `.tex` 文件前，**必须逐项核对**以下条件。任何一项不通过，必须先修正再输出：

- [ ] **获奖 ≤ 3 条**，同类奖学金仅保留最高等级 1 条，无低含金量奖项
- [ ] **经历总段数 ≤ 5**（实习 2-4 段 + 研究/项目 0-2 段）
- [ ] **每段经历仅出现在一个 section**（无跨 section 重复）
- [ ] **所有 bullet 无句号结尾**（`。` 和 `.` 均不加）
- [ ] **无领域专有术语**或引号内自定义概念（如"三大鸿沟"、"高房价城市轮化效应"等）
- [ ] **无外部过时数据**（营收额、净利润、CAGR 等第三方数据）
- [ ] **研究项目 bullet 已面向目标岗位改写**（无堆砌统计方法名称）
- [ ] **城市信息均来自经历文件**
- [ ] **中文引号格式正确** `"..."`

---

## 输出规范

每次生成简历，输出到（多人模式下按人员隔离）：
```
output/{person_id}/{公司名}_{岗位名}_{YYYYMMDD}/
├── resume-zh_CN.tex / resume-en.tex
├── resume-zh_CN.pdf / resume-en.pdf
└── generation_log.md
```

> Legacy 单人模式输出到 `output/{公司名}_{岗位名}_{YYYYMMDD}/`

`generation_log.md` 内容：
- 目标岗位与 JD 关键词提取结果
- 选入的经历列表及选择理由
- 每段经历选用了哪些 bullet 及改写说明
- 调优记录（是否调优、具体操作）

---

## 注意事项

1. **不捏造**数据或经历，只改写和筛选用户已提供的内容
2. JD 要求的技能若用户确实不具备，**不添加**，可弱化相关描述
3. LaTeX 特殊字符必须转义：`&` → `\&`，`%` → `\%`，`_` → `\_`
4. 编译失败时检查 `.log` 文件中的错误信息
5. 必须使用 `xelatex`，不能用 `pdflatex`（中文字体支持）
