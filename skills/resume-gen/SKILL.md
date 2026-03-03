---
name: resume
description: 岗位针对性简历生成器。当用户提供 JD（岗位描述）、面经或岗位分析，或提出"帮我生成简历"、"针对这个岗位"等请求时触发。自动检查用户数据完整性，分析岗位需求，从经历库中智能筛选内容，生成单页 LaTeX 简历 PDF，自动调优至恰好一页。
license: Apache-2.0
---

# Resume Generation Skill

## Step 0：前置检查

在生成之前，先读取 `data/profile.md` 和 `data/experiences/`：

- 如果 `profile.md` 仍含 `[YOUR_XXX]` 占位符 → **停止，提示用户先填写 profile.md**
- 如果 `experiences/` 没有有效文件 → **停止，提示用户先添加经历文件**

提示格式：
```
⚠️  数据未完善，无法生成简历。
请参考 SETUP.md 完成设置后重新发送 JD。
```

## Step 1：岗位分析

提取 JD 核心信息：
- 公司 + 岗位名称
- 岗位类型（产品 / 运营 / 技术 / 金融 / 咨询 / 研究 / 其他）
- 关键词：技术栈、职能、行业背景、软技能

## Step 2：内容匹配

读取 `data/experiences/*.md`（跳过 `_template.md`、`README.md`、空文件）

**选取规则：**
- 选 **3-5 段**，目标填满整页
- 按 JD 关键词与经历标签的匹配度排序
- **严格按时间倒序排列**

**内容来源优先级：**
`data/work_materials/{公司名}/` 非空文件 > `data/experiences/{公司名}.md`

**Bullet Point 改写原则：**
- 每段 2-3 条（最多 4 条）
- 保留量化数据，不捏造
- 动词名词向 JD 关键词靠拢
- 结果导向，不是"负责了什么"

## Step 3：生成 LaTeX

```bash
OUTPUT_DIR="$(pwd)/output/{公司}_{岗位}_{YYYYMMDD}"
mkdir -p "$OUTPUT_DIR"
cp -r "$(pwd)/latex_src/resume/"* "$OUTPUT_DIR/"
```

修改 `$OUTPUT_DIR/resume-zh_CN.tex`：
- 头部：从 `data/profile.md` 读取姓名、邮箱、电话
- 教育背景：从 `data/profile.md` 读取，按岗位选 4-5 门课程
- 经历：选定经历按时间倒序，不选的用 `%` 注释
- 获奖 + 技能：从 `data/profile.md` 读取，按岗位调整顺序

## Step 4：编译

```bash
export PATH="$HOME/Library/TinyTeX/bin/universal-darwin:$PATH"
# Linux: export PATH="$HOME/.TinyTeX/bin/x86_64-linux:$PATH"

cd "$OUTPUT_DIR"
xelatex -interaction=nonstopmode resume-zh_CN.tex > /tmp/xelatex_out.txt 2>&1
echo "Exit: $?"
mdls -name kMDItemNumberOfPages resume-zh_CN.pdf
```

编译出错：检查 `.log`，常见为特殊字符未转义（`&`→`\&`，`%`→`\%`，`_`→`\_`）

## Step 5：单页调优

超页时按序执行（每步后重新编译）：

1. **修复悬挂行**：bullet 末尾 1-3 字独占一行 → 删除/改写尾部冗余短语
2. `top/bottom 0.5in → 0.4in`（resume.cls）
3. `itemsep/topsep → 0.05em`（resume.cls）
4. 注释研究经历整块
5. 注释最不相关的经历段落
6. `\vspace{-6pt} → -8pt`，`\vspace{-2pt} → -4pt`
7. `\LoadClass[10pt] → [9.5pt]`（resume.cls）
8. `\titlespacing*{\section}{0cm}{*1.5}{*1.3} → {0cm}{*1.0}{*0.8}`（resume.cls）

**红线：字号 ≥ 9pt，上下页边距 ≥ 0.35in**

## Step 6：输出汇报

生成完成后输出：
- 目标岗位 + 文件路径
- 选入经历及理由（与 JD 哪些关键词对齐）
- 调优记录

同时写入 `generation_log.md`。
