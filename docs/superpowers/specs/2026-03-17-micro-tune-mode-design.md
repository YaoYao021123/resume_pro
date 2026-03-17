# 历史简历微调模式设计（生成页入口）

## 1. 背景与目标

当前系统支持：
- 新建生成：`/api/generate`
- 编辑页重生成：`/api/editor/regenerate`（基于原 JD 上下文 + 用户反馈）

新需求：当用户填写完**新的 JD/面经**后，可进入“微调模式”，选择一份历史简历作为 seed，预览“历史简历 + 历史JD + 历史面经”，再由 AI 基于**当前新JD**进行最小改动微调。

核心目标：
- 默认保留历史简历结构与大部分措辞
- 仅在必要时替换 1-2 段经历
- 结果输出到**新目录**，不覆盖历史目录

---

## 2. 范围

### In Scope
- 生成页新增“微调模式”开关与历史简历选择入口
- 历史候选列表（全部可见，按相似度排序并标注）
- 历史包预览（PDF + 历史JD + 历史面经，均只读）
- 新接口：获取微调候选、执行微调生成
- 微调结果新目录产出并进入画廊

### Out of Scope
- 直接对历史 `.tex` 做 patch 式编辑
- 覆盖历史目录
- 复杂向量检索/Embedding 服务

---

## 3. 用户流程（生成页）

1. 用户填写新 JD / 新面经
2. 打开“微调模式”
3. 点击“选择历史简历”
4. 在弹窗查看候选卡片（相似度标签 + 时间 + 公司/岗位）
5. 选中卡片后看到只读预览：
   - 历史 PDF
   - 历史 JD（`generation_context.json.jd_text`）
   - 历史面经（优先 `interview_notes`，其次 `interview_text`）
6. 点击“基于该历史简历微调”
7. 生成完成后返回结果卡，打开新目录的编辑页或 PDF

---

## 4. 架构与组件

## 前端（`web/index.html`）
- 新状态：
  - `microTuneEnabled: boolean`
  - `selectedSeedDir: string | null`
  - `microTuneCandidates: Candidate[]`
- 新 UI：
  - 生成页“微调模式”开关
  - “选择历史简历”按钮
  - 候选弹窗 + 预览面板
- 新 API 调用：
  - `POST /api/micro-tune/candidates/query`
  - `POST /api/generate/micro-tune`

## 后端（`web/server.py`）
- 新路由：
  - `POST /api/micro-tune/candidates/query` → `_query_micro_tune_candidates`
  - `POST /api/generate/micro-tune` → `_generate_micro_tuned_resume`
- 复用现有：
  - `_get_gallery()` 的目录遍历能力
  - `tools.generate_resume.generate_resume(...)` 主流程

## 生成引擎（`tools/generate_resume.py`）
- 增加可选 seed 参数：
  - `seed_context`（历史公司/岗位/JD/面经/生成引擎）
  - `seed_resume_summary`（历史简历结构摘要）
  - `micro_tune_constraints`（最小改动、可替换段数上限）
- 在 AI prompt 中注入“微调约束段落”

---

## 5. 数据与相似度

## 候选数据来源
- 遍历 `output/{person_id}/` 下目录
- 读取：
  - `generation_context.json`
  - `resume-zh_CN.pdf`（存在性）
  - `resume-zh_CN.tex`（可选用于摘要）

## 相似度（轻量规则）
- 输入：新 JD + 新面经
- 候选：历史 `jd_text + interview_text + company + role`
- 打分组成：
  - 关键词重叠（技术/领域/职能）60%
  - 岗位词重叠（role/company token）30%
  - 时间新近度 10%
- 输出：`similarity_score`（0-100）与标签（高/中/低）

---

## 6. 微调生成逻辑（推荐路线 B）

1. 读取 seed 目录上下文与历史 tex 摘要
2. 构造微调约束：
   - 优先保留历史结构、段落顺序、标题风格
   - 默认不替换经历；仅当与新 JD 不匹配时最多替换 1-2 段
   - 保留可验证数字与技术细节，不允许泛化降级
3. 调用 `generate_resume(new_jd, new_interview, company, role, prefer_ai=True, ...)`
4. 通过新增参数将 seed 信息注入 `_build_ai_prompt`
5. 完成后写入新目录，保留原目录

## 6.1 “可替换 1-2 段”的可执行判定

- 对 seed 中每段经历计算 `retain_score`（0-100）：
  - 新JD核心诉求覆盖（`core_demands` 命中）50%
  - 关键技术/对象保留度 30%
  - 可量化结果相关性 20%
- 判定规则：
  - `retain_score >= 65`：必须保留
  - `45 <= retain_score < 65`：可保留或替换（由AI选择，但需在 `relevance_reason` 说明）
  - `< 45`：可替换优先
- 替换上限：最多 2 段；若需替换超过 2 段，降级为“非微调场景”，前端提示建议使用普通生成
- 降级契约：返回 `409 MICRO_TUNE_SCOPE_EXCEEDED`，携带建议动作（`suggestion: "use_full_generate"`）

## 6.2 seed 参数契约（后端 -> 生成引擎）

```json
{
  "seed_context": {
    "dir": "历史目录名",
    "company": "历史公司",
    "role": "历史岗位",
    "jd_text": "历史JD（最多3000字）",
    "interview_text": "历史面经（最多2000字）",
    "engine": "ai|heuristic"
  },
  "seed_resume_summary": {
    "sections": [
      {
        "name": "实习经历",
        "entries": [
          {
            "company": "百度智能云",
            "role": "ToB售前AI产品实习生",
            "bullet_count": 3,
            "bullet_samples": ["...","..."]
          }
        ]
      }
    ],
    "total_experiences": 3
  },
  "micro_tune_constraints": {
    "mode": "minimal_change",
    "max_replacements": 2,
    "preserve_style": true,
    "preserve_structure": true
  }
}
```

- 长度边界：
  - `seed_context.jd_text` ≤ 3000 字
  - `seed_context.interview_text` ≤ 2000 字
  - `seed_resume_summary` 每段最多保留 2 条 bullet sample、每条 ≤ 120 字
- 数据来源：
  - `seed_context` 来自 `generation_context.json`
  - `seed_resume_summary` 来自历史 tex 解析结果（缺失时允许为空对象）

---

## 7. API 草案

## 统一错误响应结构（两接口共用）

```json
{
  "error": {
    "code": "MICRO_TUNE_SCOPE_EXCEEDED",
    "message": "微调范围超出限制，建议使用普通生成",
    "details": {
      "required_replacements": 3,
      "max_replacements": 2,
      "suggestion": "use_full_generate"
    }
  }
}
```

对外错误码固定；底层 provider 原始报错仅放 `error.details.cause`，不直接作为顶层错误码返回。

## `POST /api/micro-tune/candidates/query`

Request:
```json
{
  "jd": "新的JD",
  "interview": "新的面经"
}
```

Response:
```json
{
  "candidates": [
    {
      "dir": "百度智能云_ToB售前AI产品实习生_20260310",
      "company": "百度智能云",
      "role": "ToB售前AI产品实习生",
      "generated_at": "2026-03-10T21:10:30",
      "similarity_score": 87,
      "similarity_label": "高",
      "pdf_path": "百度智能云_ToB售前AI产品实习生_20260310/resume-zh_CN.pdf",
      "jd_text": "...",
      "interview_text": "...",
      "interview_notes": "..."
    }
  ]
}
```

校验与错误：
- 非法 JSON：`INVALID_JSON` (400)
- `jd` 缺失或空：`JD_REQUIRED` (400)
- `interview` 不是字符串：`INVALID_INTERVIEW` (400)
- 内部读取失败：`CANDIDATES_QUERY_FAILED` (500)

## `POST /api/generate/micro-tune`

Request:
```json
{
  "jd": "新的JD",
  "interview": "新的面经",
  "seed_dir": "历史目录名",
  "company": "",
  "role": "",
  "prefer_ai": true
}
```

`company/role` 回填优先级：
1. 请求体显式值（非空）
2. `seed_context.company/role`
3. `未知公司` / `未知岗位`

Response 复用 `/api/generate` 成功结构。

## 请求校验与错误码约定

| 校验项 | 条件 | 错误码 | HTTP |
|---|---|---|---|
| JSON 体 | 非法 JSON | `INVALID_JSON` | 400 |
| `jd` | 缺失或空字符串 | `JD_REQUIRED` | 400 |
| `seed_dir` | 缺失或空字符串 | `SEED_DIR_REQUIRED` | 400 |
| `seed_dir` | 含 `..`、`/`、前导`.` | `INVALID_SEED_DIR` | 403 |
| `seed_dir` | realpath 不在 `output/{person_id}` 下（含 symlink escape） | `INVALID_SEED_DIR` | 403 |
| `seed_dir` | 目录不存在 | `SEED_NOT_FOUND` | 404 |
| `prefer_ai` | 非布尔值 | `INVALID_PREFER_AI` | 400 |
| seed context | 缺失 `generation_context.json` 或缺失 `jd_text` | `SEED_CONTEXT_MISSING` | 400 |
| 微调范围 | 需要替换经历 > 2 段 | `MICRO_TUNE_SCOPE_EXCEEDED` | 409 |
| 生成失败 | AI/编译失败 | `MICRO_TUNE_FAILED` | 500 |

---

## 8. 错误处理

- `seed_dir` 非法或越权：403
- 历史目录不存在：404
- 缺失 `generation_context.json`：候选阶段允许展示但标记“不完整”；执行阶段直接报错（400）
- AI 调用失败：统一返回 `MICRO_TUNE_FAILED`，并在 `details.cause` 附 provider 错因
- 编译失败：返回日志摘要 + 失败状态

---

## 9. 兼容性与回退

- 若 seed 缺失历史 JD/面经：候选可展示但执行不可用（需 `generation_context.json` + `jd_text`）
- 若 seed 缺失 tex：使用 context 文本作为最小 seed
- 微调失败时不影响已有历史目录
- 新目录命名唯一性：`{公司}_{岗位}_{YYYYMMDD_HHMMSS}`；若仍冲突，追加 `_v2`、`_v3` 递增后缀，原子检查后落盘

## 9.1 替换计数闭环（可审计）

- 先定义 seed 经历稳定标识 `entry_id`：
  - 规则：`sha1(section + "|" + company + "|" + role + "|" + time_start + "|" + first_bullet_prefix)`
  - 由 seed 摘要阶段统一生成并传入 prompt；AI 只能引用已提供 `entry_id`
- AI 规划输出新增元数据：
  - `seed_retained_entries`: 保留的 seed 经历标识列表
  - `seed_replaced_entries`: 被替换的 seed 经历标识列表
  - `replacement_count`: 替换段数
- 后端在应用计划前做机器校验：
  - 若 `replacement_count != len(seed_replaced_entries)` -> 视为无效计划，返回 `MICRO_TUNE_FAILED`
  - 若任一 `entry_id` 不在 seed 摘要字典中 -> 视为无效计划，返回 `MICRO_TUNE_FAILED`
  - 若 `replacement_count > 2` -> 返回 `MICRO_TUNE_SCOPE_EXCEEDED` (409)
- 校验结果写入新目录 `generation_context.json`：
  - `micro_tune.seed_dir`
  - `micro_tune.replacement_count`
  - `micro_tune.seed_replaced_entries`
  - `micro_tune.seed_retained_entries`

---

## 10. 测试与验收

## 功能验收
- 可在生成页开启微调模式并选择历史简历
- 能看到“简历+JD+面经”只读预览
- 微调结果输出到新目录，原目录不变
- 默认保留历史结构，仅必要时替换 ≤2 段经历

## 接口测试
- 候选接口排序正确、字段完整
- 微调接口异常路径覆盖（非法路径、缺文件、AI失败）

## 质量测试
- 输出 bullet 继续满足 `tools/generate_resume.py` 中 `STRICT_AI_RULES` Rule 6（标题+成果、无句号）
- 填充率/单页策略保持可用

---

## 11. 实施拆分（供后续计划）

1. 后端候选接口 + 相似度排序
2. 前端微调模式 UI + 预览弹窗
3. 微调生成接口 + seed 参数透传
4. 生成引擎 prompt 注入与约束执行
5. 联调与回归测试
