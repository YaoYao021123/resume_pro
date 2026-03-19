# LaTeX Editor — Overleaf-like 再编辑功能设计

**日期**: 2026-03-05
**状态**: Approved

---

## 概述

在 Web UI 中新增类 Overleaf 的 LaTeX 编辑器页面，允许用户在 AI 生成简历后直接编辑 `.tex` 源码并实时编译预览 PDF。入口从画廊卡片的「编辑」按钮进入。

## 布局

```
┌────────────────────────────────────────────────┐
│ [← 返回画廊]  美团·产品经理  [Compile ▶] [下载] │  ← 顶部工具栏
├───────────────────────┬────────────────────────┤
│                       │                        │
│   CodeMirror 6        │   PDF iframe           │
│   LaTeX 编辑器         │   浏览器原生渲染         │
│                       │                        │
│   - 语法高亮           │   编译后自动刷新         │
│   - 行号              │                        │
│   - 括号匹配           │                        │
│   - Ctrl+F 搜索       │                        │
│   - Tab 缩进           │                        │
│                       │                        │
├───────────────────────┴────────────────────────┤
│ ✅ Compiled | Fill: 95.4% | 1 page      [状态栏] │
└────────────────────────────────────────────────┘
```

## 组件

| 组件 | 实现 | 说明 |
|------|------|------|
| 代码编辑器 | CodeMirror 6 via CDN (esm.sh) | LaTeX mode, 行号, 括号匹配, 搜索替换 |
| PDF 预览 | `<iframe>` + 浏览器原生 PDF 渲染 | 复用现有 `/api/gallery/pdf/` 路由 |
| 编译按钮 | 调用 `POST /api/editor/compile` | 保存 tex → xelatex → 返回状态 |
| 状态栏 | 底部固定 bar | 编译状态/错误/填充率/页数 |
| 工具栏 | 顶部固定 bar | 返回、标题、Compile、下载、快捷键提示 |

## 新增 API (server.py)

| 方法 | 路径 | 功能 | 请求/响应 |
|------|------|------|-----------|
| GET | `/api/editor/tex?dir={dir_name}` | 读取 tex 内容 | → `{content, filename, dir_name}` |
| POST | `/api/editor/save` | 保存 tex | `{dir, content}` → `{success}` |
| POST | `/api/editor/compile` | 保存 + 编译 + 检查 | `{dir, content}` → `{success, pages, fill_rate, errors, log_tail}` |

### 编译 API 实现细节

1. 将 `content` 写入 `output/{person_id}/{dir}/resume-zh_CN.tex`
2. 运行 `xelatex -interaction=nonstopmode resume-zh_CN.tex`
3. 检查退出码，成功则运行 `page_fill_check.py` 获取填充率
4. 失败则从 `.log` 文件提取最后 20 行错误信息
5. 返回 `{success, pages, fill_rate, errors, log_tail}`

### 路径安全

- `dir` 参数不得含 `..` 或 `/` 开头
- resolve 后必须在 `output/` 目录内
- 仅允许写入 `resume-zh_CN.tex` 文件

## 前端交互流程

1. 画廊卡片点击「编辑」→ `openEditor(dir_name)` → `goTo('editor')`
2. `GET /api/editor/tex` 加载内容 → 初始化 CodeMirror 实例
3. 右侧 iframe 加载当前 PDF（`/api/gallery/pdf/{dir}/resume-zh_CN.pdf`）
4. 用户编辑 tex 代码
5. 点击 Compile 或 Ctrl+Enter → 状态栏显示 "Compiling..."
6. `POST /api/editor/compile` → 后端保存+编译+检查
7. 成功: 刷新 iframe（`?t=timestamp`）+ 状态栏显示 ✅ + 填充率
8. 失败: 状态栏显示错误信息（log 关键行）

## CodeMirror 6 CDN 加载

```html
<script type="module">
import {EditorView, basicSetup} from 'https://esm.sh/@codemirror/basic-setup'
import {StreamLanguage} from 'https://esm.sh/@codemirror/language'
import {stex} from 'https://esm.sh/@codemirror/legacy-modes/mode/stex'
</script>
```

- 使用 esm.sh CDN，支持 ES modules
- LaTeX 语法高亮使用 `@codemirror/legacy-modes` 中的 `stex` mode
- 编辑器主题跟随页面暗色/亮色风格

## 画廊卡片改动

在现有卡片正面的按钮区域增加「编辑」按钮：

```
[查看] [编辑] [下载] [删除]
```

## 不做的事情（V2 考虑）

- SyncTeX 双向同步（编辑器 ↔ PDF 位置同步）
- 实时自动编译（输入时自动触发）
- 多文件编辑（resume.cls、.sty 等）
- 协同编辑
- Undo/Redo 历史面板
