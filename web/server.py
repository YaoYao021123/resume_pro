#!/usr/bin/env python3
"""Resume Generator Pro — 本地 Web 数据管理服务器

启动方式：python3 web/server.py [--port 8765]
自动打开浏览器 → http://localhost:8765
"""

import argparse
import cgi
import io
import json
import os
import re
import shutil
import urllib.parse
import webbrowser
import zipfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / 'data'
PROFILE_PATH = DATA_DIR / 'profile.md'
EXPERIENCES_DIR = DATA_DIR / 'experiences'
WORK_MATERIALS_DIR = DATA_DIR / 'work_materials'
OUTPUT_DIR = PROJECT_ROOT / 'output'
WEB_DIR = PROJECT_ROOT / 'web'

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB


# ─── Profile.md 解析 ───────────────────────────────────────────

def parse_profile() -> dict:
    """将 profile.md 解析为 JSON 结构"""
    if not PROFILE_PATH.exists():
        return {}

    text = PROFILE_PATH.read_text(encoding='utf-8')
    lines = text.split('\n')

    data = {
        'basic': {
            'name_zh': '', 'name_en': '', 'email': '', 'phone': '',
            'linkedin': '', 'github': '', 'website': ''
        },
        'education': [],
        'awards': [],
        'skills': {'tech': '', 'software': '', 'languages': ''},
        'projects': [],
        'publications': [],
        'directions': {'primary': '', 'secondary': ''}
    }

    section = ''
    subsection = ''
    in_code_block = False
    current_edu = None
    code_lines = []

    for line in lines:
        stripped = line.strip()

        # 检测 section
        if stripped.startswith('## ') and not stripped.startswith('### '):
            section = stripped[3:].strip()
            subsection = ''
            in_code_block = False
            continue

        # 检测 subsection
        if stripped.startswith('### '):
            subsection = stripped[4:].strip()
            in_code_block = False

            # 新的教育条目
            if section == '教育背景' and '学历' in subsection:
                if current_edu:
                    data['education'].append(current_edu)
                current_edu = {
                    'school': '', 'degree': '', 'major': '', 'department': '',
                    'time_start': '', 'time_end': '', 'gpa': '', 'rank': '', 'courses': ''
                }

            if section == '项目经历' and '项目' in subsection:
                # Each project is parsed from its code block
                pass
            continue

        # 代码块边界
        if stripped.startswith('```'):
            if in_code_block:
                # 代码块结束，处理收集的行
                _process_code_block(data, section, subsection, current_edu, code_lines)
                code_lines = []
            in_code_block = not in_code_block
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        # 课程行（在教育背景中，不在代码块中的特殊处理）
        # 课程通常在代码块中，已在 _process_code_block 处理

    # 收尾最后一个教育条目
    if current_edu:
        data['education'].append(current_edu)

    # 确保至少有一个教育条目
    if not data['education']:
        data['education'].append({
            'school': '', 'degree': '', 'major': '', 'department': '',
            'time_start': '', 'time_end': '', 'gpa': '', 'rank': '', 'courses': ''
        })

    return data


def _process_code_block(data, section, subsection, current_edu, code_lines):
    """处理一个代码块中的内容"""
    block_text = '\n'.join(code_lines)

    if section == '基本信息':
        kv = _parse_kv_block(code_lines)
        # 第一个代码块：必填信息
        if '姓名（中文）' in kv or '姓名(中文)' in kv:
            data['basic']['name_zh'] = kv.get('姓名（中文）', kv.get('姓名(中文)', ''))
            data['basic']['name_en'] = kv.get('姓名（英文）', kv.get('姓名(英文)', ''))
            data['basic']['email'] = kv.get('邮箱', '')
            data['basic']['phone'] = kv.get('电话', '')
        # 第二个代码块：可选信息
        if 'LinkedIn（可选）' in kv or 'LinkedIn(可选)' in kv or 'LinkedIn' in kv:
            data['basic']['linkedin'] = kv.get('LinkedIn（可选）', kv.get('LinkedIn(可选)', kv.get('LinkedIn', '')))
            data['basic']['github'] = kv.get('GitHub（可选）', kv.get('GitHub(可选)', kv.get('GitHub', '')))
            data['basic']['website'] = kv.get('个人网站（可选）', kv.get('个人网站(可选)', kv.get('个人网站', '')))

    elif section == '教育背景' and current_edu is not None:
        kv = _parse_kv_block(code_lines)
        if '学校' in kv:
            current_edu['school'] = kv.get('学校', '')
            current_edu['degree'] = kv.get('学历', '')
            current_edu['major'] = kv.get('专业', '')
            current_edu['department'] = kv.get('学院', '')
            time_str = kv.get('时间', '')
            if '--' in time_str:
                parts = time_str.split('--')
                current_edu['time_start'] = parts[0].strip()
                current_edu['time_end'] = parts[1].strip()
            current_edu['gpa'] = kv.get('GPA', '')
            current_edu['rank'] = kv.get('排名', '')
        else:
            # 课程代码块
            courses = block_text.strip()
            if courses and not courses.startswith('['):
                current_edu['courses'] = courses

    elif section == '获奖情况':
        for line in code_lines:
            line = line.strip()
            if not line or line.startswith('['):
                continue
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 3:
                data['awards'].append({
                    'name': parts[0], 'issuer': parts[1], 'date': parts[2]
                })
            elif len(parts) == 2:
                data['awards'].append({
                    'name': parts[0], 'issuer': parts[1], 'date': ''
                })

    elif section == '技能':
        content = block_text.strip()
        if content.startswith('['):
            content = ''
        if '编程' in subsection or '技术' in subsection:
            data['skills']['tech'] = content
        elif '软件' in subsection:
            data['skills']['software'] = content
        elif '外语' in subsection or '语言' in subsection:
            data['skills']['languages'] = content

    elif section == '求职方向标签':
        kv = _parse_kv_block(code_lines)
        data['directions']['primary'] = kv.get('主要方向', '')
        data['directions']['secondary'] = kv.get('兴趣方向（可选）', kv.get('兴趣方向', ''))

    elif section == '项目经历':
        kv = _parse_kv_block(code_lines)
        if '项目名称' in kv:
            data['projects'].append({
                'name': kv.get('项目名称', ''),
                'role': kv.get('角色', ''),
                'time_start': '',
                'time_end': '',
                'desc': kv.get('描述', ''),
                'tags': kv.get('标签', ''),
            })
            time_str = kv.get('时间', '')
            if '--' in time_str:
                parts = time_str.split('--')
                data['projects'][-1]['time_start'] = parts[0].strip()
                data['projects'][-1]['time_end'] = parts[1].strip()

    elif section == '论文发表':
        for line in code_lines:
            line = line.strip()
            if not line or line.startswith('['):
                continue
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 5:
                data['publications'].append({
                    'title': parts[0], 'authors': parts[1], 'venue': parts[2], 'year': parts[3], 'description': parts[4]
                })
            elif len(parts) >= 4:
                data['publications'].append({
                    'title': parts[0], 'authors': parts[1], 'venue': parts[2], 'year': parts[3], 'description': ''
                })
            elif len(parts) == 3:
                data['publications'].append({
                    'title': parts[0], 'authors': parts[1], 'venue': parts[2], 'year': '', 'description': ''
                })


def _parse_kv_block(lines):
    """解析 key：value 格式的代码块"""
    result = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 支持中文冒号和英文冒号
        for sep in ['：', ':']:
            if sep in line:
                key, value = line.split(sep, 1)
                key = key.strip()
                value = value.strip()
                # 去除占位符：[YOUR_XXX]、[X.X/4.0]（可选）、前 X% 等
                if re.match(r'^\[YOUR_.*\]', value):
                    value = ''
                elif re.match(r'^\[.*\]', value):
                    value = ''
                elif re.match(r'^前 X%', value):
                    value = ''
                elif re.match(r'^\[硕士', value):
                    value = ''
                result[key] = value
                break
    return result


# ─── Profile.md 写入 ───────────────────────────────────────────

def render_profile(data: dict) -> str:
    """将 JSON 数据渲染为 profile.md 格式"""
    b = data.get('basic', {})
    edu_list = data.get('education', [])
    awards = data.get('awards', [])
    skills = data.get('skills', {})
    dirs = data.get('directions', {})

    sections = []

    # 头部
    sections.append("""# 个人信息档案

> **填写说明**
> - 用 `[YOUR_XXX]` 标记的字段必须替换为你的真实信息
> - 用 `<!-- 注释 -->` 包裹的是说明文字，填完后可删除
> - 课程、技能等列表：尽量多填，生成简历时会按岗位自动筛选

---""")

    # 基本信息
    sections.append(f"""## 基本信息

```
姓名（中文）：{b.get('name_zh', '')}
姓名（英文）：{b.get('name_en', '')}
邮箱：{b.get('email', '')}
电话：{b.get('phone', '')}
```

<!-- 可选：LinkedIn、GitHub、个人网站 -->
```
LinkedIn（可选）：{b.get('linkedin', '')}
GitHub（可选）：{b.get('github', '')}
个人网站（可选）：{b.get('website', '')}
```

---""")

    # 教育背景
    edu_sections = ['## 教育背景\n\n<!-- 按时间倒序填写，最新在前。可添加多段。 -->']

    for i, edu in enumerate(edu_list):
        label = '最新' if i == 0 else ''
        label_suffix = f'（{label}）' if label else ''
        time_str = f"{edu.get('time_start', '')} -- {edu.get('time_end', '')}"

        edu_sections.append(f"""### 学历 {i + 1}{label_suffix}

```
学校：{edu.get('school', '')}
学历：{edu.get('degree', '')}
专业：{edu.get('major', '')}
学院：{edu.get('department', '')}
时间：{time_str}
GPA：{edu.get('gpa', '')}
排名：{edu.get('rank', '')}
```

**主修课程{('（尽量多填，生成时按岗位筛选 4-5 门）' if i == 0 else '')}：**

```
{edu.get('courses', '')}
```""")

    sections.append('\n\n'.join(edu_sections) + '\n\n---')

    # 获奖情况
    award_lines = []
    for a in awards:
        if a.get('name'):
            parts = [a.get('name', ''), a.get('issuer', ''), a.get('date', '')]
            award_lines.append(' | '.join(parts))

    award_text = '\n'.join(award_lines) if award_lines else ''
    sections.append(f"""## 获奖情况

<!-- 每行一条，格式：奖项名称 | 颁发机构 | 时间 -->
<!-- 示例：全国大学生数学建模竞赛一等奖 | 教育部 | 2023/11 -->

```
{award_text}
```

---""")

    # 技能
    sections.append(f"""## 技能

### 编程与技术

<!-- 列举你掌握的编程语言、框架、工具 -->
<!-- 示例：Python (Pandas, NumPy, Sklearn), SQL, R, Stata, Excel VBA -->

```
{skills.get('tech', '')}
```

### 软件工具

<!-- 列举常用软件 -->
<!-- 示例：Tableau, Power BI, Figma, Notion, Salesforce -->

```
{skills.get('software', '')}
```

### 外语水平

<!-- 格式：语言 - 证书/考试成绩（水平描述） -->
<!-- 示例：英语 - IELTS 7.5（流利）；日语 - JLPT N2 -->

```
{skills.get('languages', '')}
```

---""")

    # 项目经历
    projects = data.get('projects', [])
    if projects:
        proj_sections = ['## 项目经历\n\n<!-- 按时间倒序填写 -->']
        for i, p in enumerate(projects):
            time_str = f"{p.get('time_start', '')} -- {p.get('time_end', '')}"
            proj_sections.append(f"""### 项目 {i + 1}

```
项目名称：{p.get('name', '')}
角色：{p.get('role', '')}
时间：{time_str}
描述：{p.get('desc', '')}
标签：{p.get('tags', '')}
```""")
        sections.append('\n\n'.join(proj_sections) + '\n\n---')
    else:
        sections.append("""## 项目经历

<!-- 按时间倒序填写 -->
<!-- 格式：项目名称 | 角色 | 时间 | 描述 | 标签 -->

---""")

    # 论文发表
    pubs = data.get('publications', [])
    pub_lines = []
    for pub in pubs:
        if pub.get('title'):
            parts = [pub.get('title',''), pub.get('authors',''), pub.get('venue',''), pub.get('year',''), pub.get('description','')]
            pub_lines.append(' | '.join(parts))
    pub_text = '\n'.join(pub_lines) if pub_lines else ''
    sections.append(f"""## 论文发表

<!-- 每行一条，格式：论文标题 | 作者 | 期刊/会议 | 年份 | 内容描述 -->

```
{pub_text}
```

---""")

    # 求职方向
    sections.append(f"""## 求职方向标签

<!-- 填写你的目标岗位类型，用于辅助匹配判断 -->
<!-- 可多选，以逗号分隔 -->
<!-- 常见方向：产品经理、运营、数据分析、技术开发、金融分析、咨询、投资、市场营销、人力资源 -->

```
主要方向：{dirs.get('primary', '')}
兴趣方向（可选）：{dirs.get('secondary', '')}
```
""")

    return '\n\n'.join(sections)


def parse_experience_file(filepath: Path) -> dict:
    """解析单个经历 .md 文件为 JSON 结构"""
    if not filepath.exists():
        return {}

    text = filepath.read_text(encoding='utf-8')
    lines = text.split('\n')

    result = {
        'company': '', 'city': '', 'department': '', 'role': '',
        'time_start': '', 'time_end': '', 'tags': '', 'notes': '',
        'work_items': []
    }

    section = ''
    in_code_block = False
    code_lines = []
    work_item_title = ''

    for line in lines:
        stripped = line.strip()

        if stripped.startswith('# ') and not stripped.startswith('## '):
            # Title line like "# 公司名 - 角色"
            continue

        if stripped.startswith('## '):
            section = stripped[3:].strip()
            continue

        if stripped.startswith('### '):
            sub = stripped[4:].strip()
            if '工作内容' in sub:
                # Extract title after colon if present
                if '：' in sub:
                    work_item_title = sub.split('：', 1)[1].strip()
                elif ':' in sub:
                    work_item_title = sub.split(':', 1)[1].strip()
                else:
                    work_item_title = sub
            continue

        if stripped.startswith('```'):
            if in_code_block:
                block_text = '\n'.join(code_lines).strip()
                if section == '基本信息':
                    kv = _parse_kv_block(code_lines)
                    result['company'] = kv.get('公司/机构', kv.get('公司', ''))
                    result['city'] = kv.get('所在城市', '')
                    result['department'] = kv.get('部门', '')
                    result['role'] = kv.get('职位', '')
                    time_str = kv.get('时间', '')
                    if '--' in time_str:
                        parts = time_str.split('--')
                        result['time_start'] = parts[0].strip()
                        result['time_end'] = parts[1].strip()
                elif section == '标签':
                    result['tags'] = block_text
                elif '工作内容' in section or work_item_title:
                    if block_text:
                        result['work_items'].append({
                            'title': work_item_title or block_text[:50],
                            'desc': block_text
                        })
                    work_item_title = ''
                elif '补充说明' in section:
                    result['notes'] = block_text
                code_lines = []
            in_code_block = not in_code_block
            continue

        if in_code_block:
            code_lines.append(line)

    return result


# ─── 文件名安全处理 ─────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """清理文件名，去除危险字符"""
    name = name.replace('\x00', '').replace('..', '').replace('/', '').replace('\\', '')
    name = name.strip('. ')
    if not name:
        name = 'unnamed'
    return name


# ─── 文件上传处理 ───────────────────────────────────────────────

def get_next_experience_number() -> int:
    """获取下一个经历文件编号"""
    existing = sorted(EXPERIENCES_DIR.glob('[0-9][0-9]_*.md'))
    if existing:
        try:
            return int(existing[-1].name[:2]) + 1
        except ValueError:
            pass
    return 1


def handle_md_upload(content: bytes, filename: str, company_name: str) -> str:
    """处理 .md 文件上传 → data/experiences/"""
    EXPERIENCES_DIR.mkdir(parents=True, exist_ok=True)

    if re.match(r'^\d{2}_', filename):
        target = EXPERIENCES_DIR / sanitize_filename(filename)
    else:
        num = get_next_experience_number()
        safe_name = sanitize_filename(company_name or filename.replace('.md', ''))
        target = EXPERIENCES_DIR / f'{num:02d}_{safe_name}.md'

    target.write_bytes(content)
    return str(target.relative_to(PROJECT_ROOT))


def handle_pdf_upload(content: bytes, filename: str, company_name: str) -> str:
    """处理 .pdf 文件上传 → data/work_materials/{company}/"""
    if not company_name:
        raise ValueError("PDF 文件上传需要填写关联公司名称")

    materials_dir = WORK_MATERIALS_DIR / sanitize_filename(company_name)
    materials_dir.mkdir(parents=True, exist_ok=True)

    target = materials_dir / sanitize_filename(filename)
    target.write_bytes(content)
    return str(target.relative_to(PROJECT_ROOT))


def handle_zip_upload(content: bytes, filename: str, company_name: str) -> list:
    """处理 .zip 文件上传，按类型分流"""
    if not company_name:
        raise ValueError("ZIP 文件上传需要填写关联公司名称")

    results = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if '..' in info.filename or info.filename.startswith('/'):
                raise ValueError(f"ZIP 中包含不安全路径: {info.filename}")
            if info.file_size > MAX_UPLOAD_SIZE:
                raise ValueError(f"ZIP 中文件过大: {info.filename}")

        for info in zf.infolist():
            if info.is_dir():
                continue

            file_content = zf.read(info.filename)
            basename = Path(info.filename).name

            if not basename or basename.startswith('.'):
                continue

            if basename.lower().endswith('.md'):
                result = handle_md_upload(file_content, basename, company_name)
            else:
                result = handle_pdf_upload(file_content, basename, company_name)

            results.append(result)

    return results


# ─── 经历表单录入 ───────────────────────────────────────────────

def render_experience_md(data: dict) -> str:
    """将经历表单 JSON 渲染为 _template.md 格式的 Markdown"""
    company = data.get('company', '')
    city = data.get('city', '')
    dept = data.get('department', '')
    role = data.get('role', '')
    time_start = data.get('time_start', '')
    time_end = data.get('time_end', '')
    tags = data.get('tags', '')
    work_items = data.get('work_items', [])
    notes = data.get('notes', '')

    lines = []
    lines.append(f'# {company} - {role}')
    lines.append('')
    lines.append('---')
    lines.append('')
    lines.append('## 基本信息')
    lines.append('')
    lines.append('```')
    lines.append(f'公司/机构：{company}')
    lines.append(f'所在城市：{city}')
    lines.append(f'部门：{dept}')
    lines.append(f'职位：{role}')
    lines.append(f'时间：{time_start} -- {time_end}')
    lines.append('```')
    lines.append('')
    lines.append('---')
    lines.append('')
    lines.append('## 标签')
    lines.append('')
    lines.append('```')
    lines.append(tags)
    lines.append('```')
    lines.append('')
    lines.append('---')
    lines.append('')
    lines.append('## 工作内容')
    lines.append('')

    for i, item in enumerate(work_items, 1):
        title = item.get('title', f'工作内容 {i}')
        desc = item.get('desc', '')
        lines.append(f'### 工作内容 {i}：{title}')
        lines.append('')
        lines.append('```')
        lines.append(desc)
        lines.append('```')
        lines.append('')

    if notes:
        lines.append('---')
        lines.append('')
        lines.append('## 补充说明（可选）')
        lines.append('')
        lines.append('```')
        lines.append(notes)
        lines.append('```')
        lines.append('')

    return '\n'.join(lines)


def save_experience_form(data: dict) -> str:
    """保存表单录入的经历为 .md 文件（支持新建和更新）"""
    EXPERIENCES_DIR.mkdir(parents=True, exist_ok=True)

    company = data.get('company', '').strip()
    if not company:
        raise ValueError('请填写公司/机构名')

    update_filename = data.get('update_filename', '').strip()
    if update_filename:
        # Update existing file
        safe_name = sanitize_filename(update_filename)
        target = EXPERIENCES_DIR / safe_name
        if not target.exists():
            raise ValueError(f'文件不存在: {safe_name}')
        filename = safe_name
    else:
        # Create new file
        num = get_next_experience_number()
        safe_name = sanitize_filename(company)
        filename = f'{num:02d}_{safe_name}.md'
        target = EXPERIENCES_DIR / filename

    md_content = render_experience_md(data)
    target.write_text(md_content, encoding='utf-8')

    return filename


# ─── 经历文件列表 ───────────────────────────────────────────────

def list_experiences() -> dict:
    """列出所有经历文件和工作材料"""
    experiences = []
    work_materials = []

    # 扫描 experiences/
    if EXPERIENCES_DIR.exists():
        for f in sorted(EXPERIENCES_DIR.iterdir()):
            if f.name in ('_template.md', 'README.md') or f.name.startswith('.'):
                continue
            if f.is_file():
                experiences.append({
                    'filename': f.name,
                    'size': f.stat().st_size,
                    'type': 'experience'
                })

    # 扫描 work_materials/
    if WORK_MATERIALS_DIR.exists():
        for d in sorted(WORK_MATERIALS_DIR.iterdir()):
            if d.name == 'README.md' or d.name.startswith('.'):
                continue
            if d.is_dir():
                files = [f.name for f in d.iterdir() if f.is_file() and not f.name.startswith('.')]
                if files:
                    work_materials.append({
                        'company': d.name,
                        'files': sorted(files),
                        'type': 'work_material'
                    })

    return {'experiences': experiences, 'work_materials': work_materials}


def _extract_pdf_metadata(content: bytes, filename: str) -> dict:
    """从 PDF 文件中提取论文元信息（标题、作者等）

    尝试使用 PyPDF2/pypdf 提取 PDF 元数据和首页文本。
    如果没有安装相关库，则回退为仅从文件名推断标题。
    """
    result = {
        'title': '',
        'authors': '',
        'venue': '',
        'year': '',
        'description': ''
    }

    # 尝试从文件名推断标题
    base_name = Path(filename).stem
    # 清理常见的文件名格式
    base_name = re.sub(r'[-_]', ' ', base_name)
    result['title'] = base_name

    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(content))

        # 尝试从 PDF 元数据获取信息
        meta = reader.metadata
        if meta:
            if meta.title:
                result['title'] = meta.title
            if meta.author:
                result['authors'] = meta.author

        # 提取首页文本用于描述
        if len(reader.pages) > 0:
            first_page_text = reader.pages[0].extract_text() or ''
            # 取前 500 字作为参考描述
            if first_page_text.strip():
                # 尝试提取 abstract
                abstract_match = re.search(
                    r'(?:Abstract|摘要)[:\s—\-]*(.*?)(?:\n\n|\n(?:Keywords|关键词|1[\.\s]|Introduction|引言))',
                    first_page_text,
                    re.IGNORECASE | re.DOTALL
                )
                if abstract_match:
                    result['description'] = abstract_match.group(1).strip()[:500]
                else:
                    result['description'] = first_page_text[:300].strip()

    except ImportError:
        # pypdf not installed, try PyPDF2
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(content))
            meta = reader.metadata
            if meta:
                if meta.get('/Title'):
                    result['title'] = meta['/Title']
                if meta.get('/Author'):
                    result['authors'] = meta['/Author']

            if len(reader.pages) > 0:
                first_page_text = reader.pages[0].extract_text() or ''
                if first_page_text.strip():
                    abstract_match = re.search(
                        r'(?:Abstract|摘要)[:\s—\-]*(.*?)(?:\n\n|\n(?:Keywords|关键词|1[\.\s]|Introduction|引言))',
                        first_page_text,
                        re.IGNORECASE | re.DOTALL
                    )
                    if abstract_match:
                        result['description'] = abstract_match.group(1).strip()[:500]
                    else:
                        result['description'] = first_page_text[:300].strip()

        except ImportError:
            # No PDF library available, return basic info from filename
            pass
        except Exception:
            pass
    except Exception:
        pass

    return result


# ─── 简历画廊 ─────────────────────────────────────────────────

def list_gallery_resumes() -> list:
    """扫描 output/ 目录，列出已生成的简历"""
    resumes = []
    if not OUTPUT_DIR.exists():
        return resumes

    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith('.'):
            continue
        # 目录名格式: {公司名}_{岗位名}_{YYYYMMDD}
        pdf_files = list(d.glob('*.pdf'))
        if not pdf_files:
            continue

        parts = d.name.split('_')
        company = parts[0] if len(parts) >= 1 else d.name
        role = parts[1] if len(parts) >= 2 else ''
        date = parts[2] if len(parts) >= 3 else ''
        if date and len(date) == 8:
            date = f'{date[:4]}/{date[4:6]}/{date[6:]}'

        for pdf in pdf_files:
            rel_path = str(pdf.relative_to(OUTPUT_DIR))
            resumes.append({
                'company': company,
                'role': role,
                'date': date,
                'dir_name': d.name,
                'pdf_name': pdf.name,
                'pdf_path': rel_path,
                'size': pdf.stat().st_size,
            })

    return resumes


# ─── HTTP 请求处理器 ───────────────────────────────────────────

class ResumeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        """简化日志输出"""
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, status=400):
        self._send_json({'error': message}, status)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == '/' or path == '/index.html':
            self._serve_html()
        elif path == '/api/profile':
            self._get_profile()
        elif path == '/api/experiences':
            self._get_experiences()
        elif path.startswith('/api/experiences/') and path.endswith('/content'):
            filename = urllib.parse.unquote(path[len('/api/experiences/'):-len('/content')])
            self._get_experience_content(filename)
        elif path == '/api/gallery':
            self._get_gallery()
        elif path.startswith('/api/gallery/pdf/'):
            rel_path = urllib.parse.unquote(path[len('/api/gallery/pdf/'):])
            self._serve_gallery_pdf(rel_path)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        # 检查请求大小
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > MAX_UPLOAD_SIZE:
            self._send_error_json('文件大小超过限制（50MB）', 413)
            return

        if path == '/api/profile':
            self._save_profile()
        elif path == '/api/experiences/form':
            self._save_experience_form()
        elif path == '/api/experiences':
            self._upload_experience()
        elif path == '/api/publications/upload':
            self._upload_publication_pdf()
        elif path == '/api/generate':
            self._generate_resume()
        else:
            self.send_error(404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path

        if path.startswith('/api/experiences/'):
            filename = urllib.parse.unquote(path[len('/api/experiences/'):])
            self._delete_experience(filename)
        elif path.startswith('/api/gallery/'):
            dir_name = urllib.parse.unquote(path[len('/api/gallery/'):])
            self._delete_gallery_item(dir_name)
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    # ─── 路由实现 ──────────────────────────────────────────

    def _serve_html(self):
        html_path = WEB_DIR / 'index.html'
        if not html_path.exists():
            self.send_error(404, 'index.html not found')
            return

        content = html_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def _get_profile(self):
        try:
            data = parse_profile()
            self._send_json(data)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _save_profile(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            md_content = render_profile(data)
            PROFILE_PATH.write_text(md_content, encoding='utf-8')

            self._send_json({'success': True, 'message': '个人信息已保存'})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_experiences(self):
        try:
            data = list_experiences()
            self._send_json(data)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_experience_content(self, filename):
        try:
            safe_name = sanitize_filename(filename)
            target = EXPERIENCES_DIR / safe_name
            if not target.exists():
                self._send_error_json(f'文件不存在: {safe_name}', 404)
                return
            data = parse_experience_file(target)
            self._send_json(data)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_gallery(self):
        try:
            resumes = list_gallery_resumes()
            self._send_json({'resumes': resumes})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _serve_gallery_pdf(self, rel_path):
        try:
            # Security: prevent path traversal
            if '..' in rel_path or rel_path.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            pdf_path = OUTPUT_DIR / rel_path
            if not pdf_path.exists() or not pdf_path.is_file():
                self._send_error_json('文件不存在', 404)
                return
            # Ensure path is within OUTPUT_DIR
            try:
                pdf_path.resolve().relative_to(OUTPUT_DIR.resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            content = pdf_path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Length', len(content))
            self.send_header('Content-Disposition', f'inline; filename="{pdf_path.name}"')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _delete_gallery_item(self, dir_name):
        """删除画廊中的简历目录"""
        import shutil
        try:
            # Security: prevent path traversal
            if '..' in dir_name or '/' in dir_name or dir_name.startswith('.'):
                self._send_error_json('非法路径', 403)
                return
            target = OUTPUT_DIR / dir_name
            if not target.exists() or not target.is_dir():
                self._send_error_json('目录不存在', 404)
                return
            # Ensure path is within OUTPUT_DIR
            try:
                target.resolve().relative_to(OUTPUT_DIR.resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            shutil.rmtree(target)
            self._send_json({'success': True, 'message': f'已删除: {dir_name}'})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _generate_resume(self):
        """调用生成引擎，编译 PDF 并返回结果"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            jd_text = data.get('jd', '').strip()
            interview_text = data.get('interview', '').strip()
            company_override = data.get('company', '').strip()
            role_override = data.get('role', '').strip()

            if not jd_text:
                self._send_error_json('请输入 JD 内容', 400)
                return

            # 导入生成引擎
            import sys
            tools_dir = str(PROJECT_ROOT / 'tools')
            if tools_dir not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))

            from tools.generate_resume import generate_resume

            result = generate_resume(jd_text, interview_text,
                                     company=company_override, role=role_override)
            self._send_json(result)

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_error_json(str(e), 500)

    def _save_experience_form(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            filename = save_experience_form(data)
            self._send_json({'success': True, 'filename': filename})
        except ValueError as e:
            self._send_error_json(str(e))
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _upload_experience(self):
        try:
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self._send_error_json('请使用 multipart/form-data 格式上传')
                return

            # 解析 multipart
            ctype, pdict = cgi.parse_header(content_type)
            if 'boundary' in pdict:
                if isinstance(pdict['boundary'], str):
                    pdict['boundary'] = pdict['boundary'].encode()

            content_length = int(self.headers.get('Content-Length', 0))
            environ = {
                'REQUEST_METHOD': 'POST',
                'CONTENT_TYPE': content_type,
                'CONTENT_LENGTH': str(content_length),
            }

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ=environ
            )

            company_name = form.getfirst('company_name', '').strip()

            results = []
            file_items = form['file'] if 'file' in form else []
            if not isinstance(file_items, list):
                file_items = [file_items]

            for item in file_items:
                if not hasattr(item, 'filename') or not item.filename:
                    continue

                filename = sanitize_filename(item.filename)
                file_content = item.file.read()
                ext = Path(filename).suffix.lower()

                if ext == '.md':
                    result = handle_md_upload(file_content, filename, company_name)
                    results.append({'file': filename, 'saved_to': result, 'status': 'success'})
                elif ext == '.pdf':
                    result = handle_pdf_upload(file_content, filename, company_name)
                    results.append({'file': filename, 'saved_to': result, 'status': 'success'})
                elif ext == '.zip':
                    zip_results = handle_zip_upload(file_content, filename, company_name)
                    for r in zip_results:
                        results.append({'file': f'{filename} -> {Path(r).name}', 'saved_to': r, 'status': 'success'})
                else:
                    results.append({'file': filename, 'status': 'skipped', 'reason': f'不支持的格式: {ext}'})

            self._send_json({'success': True, 'results': results})

        except ValueError as e:
            self._send_error_json(str(e))
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _upload_publication_pdf(self):
        """接收论文 PDF 上传，提取元信息"""
        try:
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self._send_error_json('请使用 multipart/form-data 格式上传')
                return

            ctype, pdict = cgi.parse_header(content_type)
            if 'boundary' in pdict:
                if isinstance(pdict['boundary'], str):
                    pdict['boundary'] = pdict['boundary'].encode()

            content_length = int(self.headers.get('Content-Length', 0))
            environ = {
                'REQUEST_METHOD': 'POST',
                'CONTENT_TYPE': content_type,
                'CONTENT_LENGTH': str(content_length),
            }

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ=environ
            )

            file_item = form['file'] if 'file' in form else None
            if not file_item or not hasattr(file_item, 'filename') or not file_item.filename:
                self._send_error_json('未找到上传的文件')
                return

            filename = file_item.filename
            file_content = file_item.file.read()

            # Try to extract metadata from PDF
            pub_data = _extract_pdf_metadata(file_content, filename)
            self._send_json(pub_data)

        except Exception as e:
            self._send_error_json(str(e), 500)

    def _delete_experience(self, filename):
        try:
            safe_name = sanitize_filename(filename)
            target = EXPERIENCES_DIR / safe_name

            if not target.exists():
                self._send_error_json(f'文件不存在: {safe_name}', 404)
                return

            if safe_name in ('_template.md', 'README.md'):
                self._send_error_json('不能删除模板文件', 403)
                return

            target.unlink()
            self._send_json({'success': True, 'message': f'已删除 {safe_name}'})
        except Exception as e:
            self._send_error_json(str(e), 500)


# ─── 启动 ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Resume Generator Pro Web UI')
    parser.add_argument('--port', type=int, default=8765, help='服务端口 (默认 8765)')
    parser.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    args = parser.parse_args()

    # 确保目录存在
    EXPERIENCES_DIR.mkdir(parents=True, exist_ok=True)
    WORK_MATERIALS_DIR.mkdir(parents=True, exist_ok=True)

    server = HTTPServer(('127.0.0.1', args.port), ResumeHandler)
    url = f'http://localhost:{args.port}'
    print(f'Resume Generator Pro Web UI')
    print(f'服务地址: {url}')
    print(f'数据目录: {DATA_DIR}')
    print(f'按 Ctrl+C 停止服务\n')

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n服务已停止')
        server.server_close()


if __name__ == '__main__':
    main()
