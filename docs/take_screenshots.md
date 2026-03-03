# 截图操作指南

本文档指导你截取 README 中引用的所有截图。

---

## 前置准备

1. 确保已填写 `data/profile.md` 和至少一段经历
2. 启动 Web UI：

```bash
python3 web/server.py
# 浏览器打开 http://localhost:8765
```

3. 建议使用 **1200px 宽度**的浏览器窗口（或 Retina 屏幕下 2400px）
4. 使用**浅色模式**保持一致性

---

## 截图列表

### 01-web-home.png — Web UI 首页

1. 打开 `http://localhost:8765`
2. 确保页面完全加载
3. 截取浏览器内容区域（不含浏览器工具栏）
4. 保存为 `docs/screenshots/01-web-home.png`

### 02-web-profile.png — 个人信息填写页

1. 点击「个人信息」标签页
2. 确保表单中有示例数据（姓名、学校等已填写）
3. 截取完整表单区域
4. 保存为 `docs/screenshots/02-web-profile.png`

### 03-web-experience.png — 经历管理页

1. 点击「经历管理」标签页
2. 确保列表中至少有 2-3 段经历
3. 截取经历列表 + 操作按钮区域
4. 保存为 `docs/screenshots/03-web-experience.png`

### 04-web-generate.png — AI 简历生成页

1. 点击「AI 生成」标签页
2. 在 JD 输入框中粘贴一段示例 JD
3. 填写公司名和岗位名
4. 截取完整的生成表单（不需要点击生成）
5. 保存为 `docs/screenshots/04-web-generate.png`

### 05-web-gallery.png — 简历画廊页

1. 先通过 Claude Code 或 Web UI 生成至少 1-2 份简历
2. 点击「简历画廊」标签页
3. 截取画廊列表（含公司名、岗位名、日期）
4. 保存为 `docs/screenshots/05-web-gallery.png`

### 06-pdf-output.png — 生成的 PDF 示例

1. 打开 `output/` 目录下任意一份生成的 PDF
2. 使用 Preview（macOS）或 PDF 阅读器打开
3. 截取整页 PDF 预览（确保内容清晰可读）
4. 保存为 `docs/screenshots/06-pdf-output.png`

> 建议对敏感信息（姓名、电话、邮箱）进行模糊处理后再截图。

### 07-claude-code.png — Claude Code 命令行

1. 在终端中打开 Claude Code（进入项目目录）
2. 输入 `/resume` 并粘贴一段 JD
3. 等待系统开始处理（显示分析岗位、匹配经历等步骤）
4. 截取终端窗口
5. 保存为 `docs/screenshots/07-claude-code.png`

---

## 图片优化（可选）

如果截图文件较大，可使用以下工具压缩：

```bash
# macOS（需安装 pngquant）
brew install pngquant
pngquant --quality=65-80 docs/screenshots/*.png --ext .png --force

# 或使用 ImageOptim（macOS GUI 工具）
```

---

## 完成检查

确认以下文件均已存在：

```bash
ls -la docs/screenshots/
# 应看到：
# 01-web-home.png
# 02-web-profile.png
# 03-web-experience.png
# 04-web-generate.png
# 05-web-gallery.png
# 06-pdf-output.png
# 07-claude-code.png
```

截图完成后，README 中的图片引用将自动生效。
