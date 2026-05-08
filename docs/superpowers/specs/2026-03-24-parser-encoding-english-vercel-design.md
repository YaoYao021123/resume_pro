# 简历解析编码修复 + 英文简历支持 + Vercel 部署设计

## 1. 背景与目标

当前用户反馈“导入解析乱码”，且项目已新增英文模板 `latex_src/resume/resume-en.tex`，需要把英文模板纳入主生成与导入流程。同时目标部署平台为 Vercel，需要给出可落地的部署方式。

本次目标：

- 修复上传解析中的文本乱码（重点覆盖 UTF-16/UTF-32/BOM/常见中文编码）
- 增加显式语言模式 `language=zh|en`，贯通 CLI + Web API + import-resume 流程
- 英文模板接入并可编译输出英文 PDF
- 输出适配本项目的 Vercel 混合部署指南
- 完成必要测试与代码提交（用户要求直接提交到 `main`）

---

## 2. 范围

### In Scope

- `web/server.py` 上传文本解码增强
- `tools/generate_resume.py` 增加语言参数（默认 `zh`）
- `/api/generate`、`/api/import-resume/create-empty`、`/api/import-resume/confirm-compile` 增加 `language`
- Web 前端增加语言选择并透传
- 模板/编译目标按语言切换：
  - 中文：`resume-zh_CN.tex` / `resume-zh_CN.pdf`
  - 英文：`resume-en.tex` / `resume-en.pdf`
- 覆盖所有依赖 tex/pdf 文件名的 Web 功能路径（导入编译、编辑器读取与保存、版本快照、gallery 元数据与下载）
- 新增/更新 unittest
- README 增补英文使用与部署文档

### Out of Scope

- 自动语言检测（按用户确认，采用显式参数）
- 英文语义重写算法重构（本期先做模板与链路协同）
- 在 Vercel Serverless 里直接跑 XeLaTeX（不可作为主路径）

---

## 3. 方案对比与结论

### A. 现有链路增量扩展（推荐）
- 在现有生成与导入链路上新增 `language` 参数与模板分支
- 增强解码逻辑，最小侵入修复乱码
- 优点：改动集中、风险可控、可回归
- 缺点：中文/英文渲染函数共存，后续仍需抽象

### B. 新建独立英文生成子系统
- 优点：中英隔离彻底
- 缺点：重复代码大、维护成本高、交付慢

### C. 仅模板切换，不修解析与导入
- 优点：最快
- 缺点：不能解决乱码核心痛点，且导入流程不完整

结论：采用 A。

---

## 4. 详细设计

## 4.1 编码修复设计（导入解析）

### 现状问题
- `extract_text_from_upload()` 对 `.txt/.md` 仅尝试 `utf-8/utf-8-sig/gb18030/gbk`，回退 `latin-1`，容易出现“可解码但内容乱码”。

### 设计
- 增加更稳健的解码策略函数（例如 `_decode_text_bytes_best_effort`）：
  1. 优先按 BOM 识别：UTF-8-SIG、UTF-16 LE/BE、UTF-32 LE/BE
  2. 无 BOM 时按候选编码尝试：`utf-8`、`utf-16`、`utf-32`、`gb18030`、`gbk`
  3. 对每个结果做质量评分，选择最高分文本（确定性规则）：
     - `replacement_ratio = count("�") / max(len(text), 1)`
     - `printable_ratio = count(ch.isprintable() or ch in "\\n\\r\\t") / max(len(text), 1)`
     - `score = printable_ratio - 2.0 * replacement_ratio`
     - 过滤阈值：`printable_ratio < 0.85` 的候选直接丢弃
     - 若多个候选得分相同，按编码优先级决策：`utf-8 > utf-16 > utf-32 > gb18030 > gbk`
  4. 所有策略失败时，最后回退 `latin-1` + `errors='replace'`
- 不引入外部依赖，保持 stdlib。

### 验收标准
- UTF-16/UTF-32 文本简历上传后中文字段不再乱码
- 现有 UTF-8/GBK 文件解析行为不回退

## 4.2 语言参数贯通设计（主生成）

### 参数契约
- 新增 `language`：只允许 `zh` 或 `en`，默认 `zh`
- 非法值返回明确错误（不 silent fallback）：
  - HTTP API：`400`, `{"error":"invalid language: <val>; allowed: zh,en"}`
  - CLI：打印同样语义错误并 `exit code != 0`

### 统一校验入口
- 新增统一函数（示例：`_normalize_language(value: str|None) -> str`）
  - `None/'' -> 'zh'`
  - 仅接受 `zh/en`
  - 任何入口（CLI、`/api/generate`、import API）必须复用同一函数，保证行为一致

### 语言单一真源（Single Source of Truth）
- 主生成：以请求参数 `language` 为真源（缺省为 `zh`）
- 导入草稿：以草稿目录 `generation_context.json.language` 为真源
  - `create-empty` 写入该值
  - `confirm-compile` 若请求未传 language，则使用草稿语言
  - 若请求显式传 language 且与草稿语言不一致 -> 400

### CLI
- `tools/generate_resume.py` 增加 `--language {zh,en}`（短别名可选 `-l`）
- 默认 `zh`，保证向后兼容

### 生成引擎
- `generate_resume(...)` 签名增加 `language='zh'`
- 模板/输出文件名由语言映射：
  - `zh -> resume-zh_CN.tex/pdf`
  - `en -> resume-en.tex/pdf`
- `generation_context.json` 增加 `language`

## 4.3 语言参数贯通设计（导入流程）

- `/api/import-resume/create-empty` 接收 `language`
  - 草稿目录创建时复制对应模板并在目录写入 `generation_context.json.language`
- `/api/import-resume/confirm-compile` 接收 `language`
  - 写入对应 tex 文件并编译对应目标
  - 返回 `pdf_path` 跟随语言
  - 若传入 `language` 与草稿目录已有 `generation_context.json.language` 不一致，返回 400（防止草稿语言漂移）
- `render_imported_resume_tex(structured, language='zh')`
  - `zh` 使用现有中文 section label
  - `en` 使用英文 section label（Education/Experience/Skills/Honors）
  - 保持事实内容不变，仅做模板/文案层适配

### 旧草稿/手工目录兼容规则
- `generation_context.json` 不存在或无 `language` 字段时：
  1. 若目录存在 `resume-en.tex` 则推断 `en`
  2. 否则若存在 `resume-zh_CN.tex` 则推断 `zh`
  3. 两者都不存在时默认 `zh`
- `confirm-compile` 若请求未传 language，按以上规则推断并落盘回写 `generation_context.json.language`
- 若请求显式传 language 且与推断/上下文语言不一致，返回 400（提示“请新建对应语言草稿目录”）

## 4.4 Web 前端设计

- 在“生成”和“导入简历”区域增加语言选择（zh/en）
- 发起 `/api/generate`、`/api/import-resume/*` 请求时透传 `language`
- UI 默认中文，避免破坏现有用户习惯

## 4.5 Vercel 部署设计（混合架构）

### 约束
- Vercel Serverless/Edge 不适合稳定执行 XeLaTeX 编译，且冷启动/文件系统限制明显。

### 目标架构
- Vercel：托管前端静态资源 + 轻量 API 网关
- 独立容器服务（Render/Fly.io/Railway）：运行 `web/server.py` 和 XeLaTeX 编译
- 可选独立认证计费后端：`backend/auth_billing_service`

### 数据流
- 浏览器 -> Vercel 前端/API -> Resume 编译容器服务
- 编译容器服务本地执行 XeLaTeX，返回 PDF 下载路径

### 网关与编译服务契约
- Vercel API（BFF）转发：
  - `POST /api/generate`（含 `language`）-> 编译服务同名接口
  - `POST /api/import-resume/*`（含 `language`）-> 编译服务同名接口
- 响应要求：
  - `success`, `output_dir`, `pdf_path`, `error`, `log_tail`（失败时）
- 鉴权：
  - 生产环境建议在 Vercel -> 编译服务间增加静态服务密钥（`X-Service-Token`）
  - 编译服务拒绝无 token 或 token 不匹配请求（401/403）

### 两种部署路径（择一）
- 路径 A（推荐）：Vercel BFF 转发
  - 浏览器只访问 Vercel 域名
  - Vercel API Route 转发到编译服务，并注入 `X-Service-Token`
  - 编译服务仅允许来自 Vercel 的 token 请求
- 路径 B：浏览器直连编译服务
  - 不使用服务间 token 注入
  - 依赖用户态鉴权（JWT/session）+ 严格 CORS + 限流
  - 仅在你可接受跨域与鉴权暴露时使用

### PDF 存储策略（MVP）
- 第一阶段：编译服务本地磁盘短期保存（与当前 `output/` 兼容）
- 对外下载：
  - 通过编译服务受控下载接口访问，不直接暴露文件系统路径
  - 保留策略由部署层实现（例如按天清理）
- 后续可演进到对象存储（S3/R2），但不在本期实现范围

### 可执行部署细节（MVP）
- `vercel.json`（路径 A）：
  - 保留前端静态托管
  - 由 `api/*.ts` 执行转发（而非纯 rewrite），以便注入 `X-Service-Token`
- CORS：
  - 编译服务仅允许 Vercel 站点域名
- 上传限制：
  - 大文件上传直接走编译服务端点，避免 Vercel 函数体积/超时限制
- 环境变量（命名约定）：
  - `PUBLIC_API_BASE_URL`：前端请求基地址（Vercel 注入）
  - `RESUME_BACKEND_BASE_URL`：BFF 转发目标
  - `RESUME_BACKEND_SERVICE_TOKEN`：Vercel -> 编译服务鉴权 token
  - 编译服务侧：`EXPECTED_SERVICE_TOKEN` 校验上述 token

### 环境变量与配置
- 前端侧：后端 API base URL
- 编译服务侧：`PATH` 包含 xelatex、模型配置、Auth/Billing 对接配置

## 4.6 文件名解析矩阵（必须覆盖）

| 模块/接口 | 输入 | 解析规则 | 产物 |
|---|---|---|---|
| CLI `generate_resume.py` | `--language` | `_normalize_language` + `_resolve_resume_filenames` | tex/pdf 按语言 |
| `/api/generate` | `language` | 同上 | tex/pdf 按语言 |
| `/api/import-resume/create-empty` | `language` | 同上，写 `generation_context.language` | 草稿模板按语言 |
| `/api/import-resume/confirm-compile` | `language?` | 先读 context，再按兼容规则推断 | 编译/返回 pdf 按语言 |
| `/api/editor/regenerate` | dir + context | 目录语言解析后选 tex 文件 | 更新对应 tex |
| `/api/editor/compile` | dir + context | 同上 | 生成对应 pdf |
| `/api/editor/synctex` | dir + context | 同上 | 读取对应 synctex |
| gallery 列表/下载 | dir + context | 同上 | 正确展示 tex/pdf 名称 |
| 页面填充率检查 | `tex_filename` | 显式传入或由 resolver 给出 | 正确检查 zh/en tex |

---

## 5. 错误处理

- `language` 非法：400 + `invalid language`（含允许值）
- 模板文件缺失：500 + 明确提示缺哪个模板
- 编译失败：返回 `log_tail`，不吞错
- 上传文件不可解析：400 + `文件内容为空或无法解析`
- editor/gallery 目录缺失目标语言 tex：404 + 明确缺失文件名

---

## 6. 测试设计

- `tests/test_import_resume_parser.py`
  - 新增 UTF-16/UTF-32 文本解析用例（中文字段断言）
  - 新增导入渲染/创建草稿在 `language=en` 时使用英文模板与输出名
- `tests` 下 Web 相关测试（新增或扩展）：
  - `language=en` 时主生成输出 `resume-en.tex/pdf`
  - 非法 `language` 返回 400
  - `confirm-compile` 与草稿语言不一致时返回 400
  - editor 读取/保存接口按语言解析正确 tex 文件名
  - gallery 列表、下载、版本快照中的 tex/pdf 文件名按语言正确
  - 页面填充率检查入口对英文 tex 路径不报错（至少完成 smoke case）
  - legacy 目录（无 context.language）回退规则测试
  - import 旧草稿 language 推断 + 冲突报错测试

### 测试执行策略
- 单元测试优先覆盖：
  - `_normalize_language`、`_resolve_resume_filenames`
  - `_decode_text_bytes_best_effort`
- API 测试需 mock：
  - `xelatex` 子进程（`subprocess.run`）
  - `mdls` 页数查询
  - `tools/page_fill_check.py` 调用
- 目标：不依赖本机 TeX 环境即可稳定运行大部分新增测试

回归测试：
- `python3 -m py_compile tools/*.py web/server.py`
- `python3 -m unittest discover -s tests -p 'test_*.py' -v`

---

## 7. 向后兼容与发布策略

- 默认 `language=zh`，不影响现有调用
- 旧前端未传 language 时后端自动走中文模板
- 英文功能通过显式参数启用，可渐进发布

---

## 8. 交付物

- 代码改动：`web/server.py`、`tools/generate_resume.py`、`web/index.html`、测试文件
- 文档改动：`README.md`（英文能力 + Vercel 混合部署指南）
- Git 提交：按用户要求提交到当前 `main`

### 语言文件名契约表（必须统一）

| language | tex 文件 | pdf 文件 |
|---|---|---|
| zh | `resume-zh_CN.tex` | `resume-zh_CN.pdf` |
| en | `resume-en.tex` | `resume-en.pdf` |

所有端点和函数必须通过统一 resolver（示例：`_resolve_resume_filenames(language)`）获取文件名，禁止散落硬编码。

### 强制影响点枚举（本期必须改）
- `web/server.py`：
  - import 编译与返回路径
  - editor `regenerate/compile/synctex`
  - gallery 与下载路径
  - 任何 `resume-zh_CN.*` 硬编码处
- `tools/page_fill_check.py`：
  - 增加 `tex_filename`（或 `language`）参数，默认兼容 `resume-zh_CN.tex`

---

## 9. 拆分执行策略（避免范围耦合）

- Workstream A（代码）：编码修复 + 语言参数贯通 + 测试 + README 功能说明
- Workstream B（部署文档）：Vercel 混合部署指南（不阻塞代码发布）

两条 workstream 可在同一迭代交付，但实现上独立，避免部署细节阻塞功能修复。

---

## 附录 A（后续能力，非本期）

- 认证计费后端（`backend/auth_billing_service`）联动属于既有能力，本次语言与编码修复不新增其范围。部署文档只描述“可选接入点”，不扩展其实现。
