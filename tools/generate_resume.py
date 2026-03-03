#!/usr/bin/env python3
"""简历生成引擎

从用户数据 (profile.md + experiences/) 和 JD 文本，
自动匹配经历、生成 LaTeX、编译 PDF。

不依赖外部 AI API —— 使用关键词匹配 + 模板渲染。
"""

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

# ─── 路径 ─────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LATEX_TEMPLATE_DIR = PROJECT_ROOT / 'latex_src' / 'resume'
OUTPUT_DIR = PROJECT_ROOT / 'output'
DATA_DIR = PROJECT_ROOT / 'data'

# ─── LaTeX 转义 ───────────────────────────────────────────────

_LATEX_ESCAPE_MAP = {
    '&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#',
    '_': r'\_', '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}',
    '^': r'\textasciicircum{}',
}
_LATEX_ESCAPE_RE = re.compile('|'.join(re.escape(k) for k in _LATEX_ESCAPE_MAP))

def tex_escape(text: str) -> str:
    """转义 LaTeX 特殊字符"""
    if not text:
        return ''
    return _LATEX_ESCAPE_RE.sub(lambda m: _LATEX_ESCAPE_MAP[m.group()], text)


# ─── JD 关键词提取 ────────────────────────────────────────────

# 常见技术 / 业务关键词库（用于加权匹配）
KEYWORD_CATEGORIES = {
    'tech': [
        'python', 'java', 'c++', 'sql', 'r', 'spark', 'pyspark', 'hadoop',
        'pytorch', 'tensorflow', 'pandas', 'numpy', 'sklearn', 'scikit-learn',
        'docker', 'aws', 'gcp', 'linux', 'git', 'tableau', 'power bi',
        'llm', 'nlp', 'ml', 'deep learning', 'machine learning',
        '大模型', '深度学习', '机器学习', '自然语言处理', '推荐系统',
        '数据分析', '数据挖掘', '特征工程', 'a/b测试', 'ab测试',
        '算法', 'rl', '强化学习', 'rlhf', 'agent', '多模态',
        'text-to-sql', 'rag', 'prompt', 'fine-tune', '微调',
    ],
    'domain': [
        '搜索', '推荐', '广告', '金融', '电商', '社交', '教育',
        '医疗', '物流', '制造', '零售', '游戏', '内容', '安全',
        '产品', '运营', '策略', '投资', '咨询', '研究',
    ],
    'skill': [
        '沟通', '团队', '独立', '分析', '解决问题', '领导',
        '项目管理', '需求分析', '用户研究', '数据驱动',
    ],
}


def extract_jd_keywords(jd_text: str) -> dict:
    """从 JD 文本中提取关键词"""
    jd_lower = jd_text.lower()
    result = {'tech': [], 'domain': [], 'skill': [], 'raw_text': jd_text}

    for category, keywords in KEYWORD_CATEGORIES.items():
        for kw in keywords:
            if kw.lower() in jd_lower:
                result[category].append(kw)

    # 提取 JD 中公司名和岗位名（启发式）
    lines = jd_text.strip().split('\n')
    result['company'] = ''
    result['role'] = ''

    for line in lines[:10]:
        line = line.strip()
        if not line:
            continue
        # 常见 JD 格式
        for prefix in ['公司', '企业', '单位']:
            if prefix in line and '：' in line:
                result['company'] = line.split('：', 1)[1].strip()
                break
        for prefix in ['岗位', '职位', '角色']:
            if prefix in line and '：' in line:
                result['role'] = line.split('：', 1)[1].strip()
                break

    # 如果没从字段提取到，尝试从第一行/第二行猜
    if not result['company'] and not result['role']:
        for line in lines[:5]:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('-'):
                continue
            # 常见格式: "公司名 - 岗位名" or "岗位名 | 公司名"
            for sep in [' - ', '—', ' | ', '｜']:
                if sep in line:
                    parts = line.split(sep, 1)
                    result['company'] = parts[0].strip().strip('#').strip()
                    result['role'] = parts[1].strip()
                    break
            if result['company']:
                break

    return result


# ─── 经历匹配 ─────────────────────────────────────────────────

def _parse_experience_file(filepath: Path) -> dict:
    """解析经历 .md 文件（简化版，与 server.py 的 parse_experience_file 兼容）"""
    text = filepath.read_text(encoding='utf-8')
    lines = text.split('\n')

    result = {
        'company': '', 'city': '', 'department': '', 'role': '',
        'time_start': '', 'time_end': '', 'tags': '',
        'work_items': [], 'filename': filepath.name,
    }

    section = ''
    in_code = False
    code_lines = []
    work_title = ''

    for line in lines:
        s = line.strip()

        if s.startswith('## '):
            section = s[3:].strip()
            continue
        if s.startswith('### '):
            sub = s[4:].strip()
            if '工作内容' in sub:
                work_title = sub.split('：')[-1].strip() if '：' in sub else sub.split(':')[-1].strip() if ':' in sub else sub
            continue

        if s.startswith('```'):
            if in_code:
                block = '\n'.join(code_lines).strip()
                if section == '基本信息':
                    for cl in code_lines:
                        cl = cl.strip()
                        if '：' in cl:
                            k, v = cl.split('：', 1)
                            k, v = k.strip(), v.strip()
                            if '公司' in k:
                                result['company'] = v
                            elif '城市' in k:
                                result['city'] = v
                            elif '部门' in k:
                                result['department'] = v
                            elif '职位' in k:
                                result['role'] = v
                            elif '时间' in k and '--' in v:
                                parts = v.split('--')
                                result['time_start'] = parts[0].strip()
                                result['time_end'] = parts[1].strip()
                elif section == '标签':
                    result['tags'] = block
                elif '工作内容' in section or work_title:
                    if block:
                        result['work_items'].append({
                            'title': work_title or block[:50],
                            'desc': block,
                        })
                    work_title = ''
                code_lines = []
            in_code = not in_code
            continue

        if in_code:
            code_lines.append(line)

    return result


def _parse_profile() -> dict:
    """解析 profile.md（简化版）"""
    profile_path = DATA_DIR / 'profile.md'
    if not profile_path.exists():
        return {}

    text = profile_path.read_text(encoding='utf-8')

    def extract_kv(text_block):
        kv = {}
        for line in text_block.split('\n'):
            line = line.strip()
            if '：' in line:
                k, v = line.split('：', 1)
                kv[k.strip()] = v.strip()
        return kv

    # 用正则提取代码块
    code_blocks = re.findall(r'```\n(.*?)```', text, re.DOTALL)

    result = {
        'name_zh': '', 'name_en': '', 'email': '', 'phone': '',
        'github': '', 'linkedin': '', 'website': '',
        'education': [],
        'awards': [],
        'skills_tech': '', 'skills_software': '', 'skills_lang': '',
        'projects': [],
        'publications': [],
    }

    # 按 section 分段
    sections = re.split(r'^## ', text, flags=re.MULTILINE)

    for sec in sections:
        if sec.startswith('基本信息'):
            blocks = re.findall(r'```\n(.*?)```', sec, re.DOTALL)
            if blocks:
                kv = extract_kv(blocks[0])
                result['name_zh'] = kv.get('姓名（中文）', '')
                result['name_en'] = kv.get('姓名（英文）', '')
                result['email'] = kv.get('邮箱', '')
                result['phone'] = kv.get('电话', '')
            if len(blocks) > 1:
                kv2 = extract_kv(blocks[1])
                result['linkedin'] = kv2.get('LinkedIn（可选）', '')
                result['github'] = kv2.get('GitHub（可选）', '')
                result['website'] = kv2.get('个人网站（可选）', '')

        elif sec.startswith('教育背景'):
            subsections = re.split(r'### ', sec)
            for sub in subsections[1:]:  # skip header
                blocks = re.findall(r'```\n(.*?)```', sub, re.DOTALL)
                edu = {'school': '', 'degree': '', 'major': '', 'department': '',
                       'time': '', 'gpa': '', 'rank': '', 'courses': ''}
                if blocks:
                    kv = extract_kv(blocks[0])
                    edu['school'] = kv.get('学校', '')
                    edu['degree'] = kv.get('学历', '')
                    edu['major'] = kv.get('专业', '')
                    edu['department'] = kv.get('学院', '')
                    edu['time'] = kv.get('时间', '')
                    edu['gpa'] = kv.get('GPA', '')
                    edu['rank'] = kv.get('排名', '')
                if len(blocks) > 1:
                    edu['courses'] = blocks[1].strip()
                result['education'].append(edu)

        elif sec.startswith('获奖情况'):
            blocks = re.findall(r'```\n(.*?)```', sec, re.DOTALL)
            if blocks:
                for line in blocks[0].strip().split('\n'):
                    line = line.strip()
                    if line and not line.startswith('#'):
                        parts = [p.strip() for p in line.split('|')]
                        result['awards'].append({
                            'name': parts[0] if parts else '',
                            'org': parts[1] if len(parts) > 1 else '',
                            'time': parts[2] if len(parts) > 2 else '',
                        })

        elif sec.startswith('技能'):
            subsections = re.split(r'### ', sec)
            for sub in subsections[1:]:
                blocks = re.findall(r'```\n(.*?)```', sub, re.DOTALL)
                content = blocks[0].strip() if blocks else ''
                if '编程' in sub or '技术' in sub:
                    result['skills_tech'] = content
                elif '软件' in sub:
                    result['skills_software'] = content
                elif '外语' in sub or '语言' in sub:
                    result['skills_lang'] = content

        elif sec.startswith('项目经历'):
            subsections = re.split(r'### ', sec)
            for sub in subsections[1:]:
                blocks = re.findall(r'```\n(.*?)```', sub, re.DOTALL)
                if blocks:
                    kv = extract_kv(blocks[0])
                    result['projects'].append({
                        'name': kv.get('项目名称', ''),
                        'role': kv.get('角色', ''),
                        'time': kv.get('时间', ''),
                        'desc': kv.get('描述', ''),
                        'tags': kv.get('标签', ''),
                    })

        elif sec.startswith('论文发表'):
            blocks = re.findall(r'```\n(.*?)```', sec, re.DOTALL)
            if blocks:
                for line in blocks[0].strip().split('\n'):
                    line = line.strip()
                    if line and '|' in line:
                        parts = [p.strip() for p in line.split('|')]
                        result['publications'].append({
                            'title': parts[0] if parts else '',
                            'authors': parts[1] if len(parts) > 1 else '',
                            'venue': parts[2] if len(parts) > 2 else '',
                            'year': parts[3] if len(parts) > 3 else '',
                        })

    return result


def load_all_experiences() -> list:
    """加载所有经历文件"""
    exp_dir = DATA_DIR / 'experiences'
    if not exp_dir.exists():
        return []

    experiences = []
    for f in sorted(exp_dir.iterdir()):
        if f.suffix == '.md' and f.name not in ('_template.md', 'README.md'):
            try:
                exp = _parse_experience_file(f)
                if exp.get('company'):
                    experiences.append(exp)
            except Exception:
                pass
    return experiences


def match_experiences(experiences: list, jd_keywords: dict, max_count: int = 5) -> list:
    """根据 JD 关键词对经历评分排序，返回匹配的经历列表"""
    jd_all_kw = set()
    for cat in ['tech', 'domain', 'skill']:
        for kw in jd_keywords.get(cat, []):
            jd_all_kw.add(kw.lower())

    scored = []
    for exp in experiences:
        tags_lower = exp.get('tags', '').lower()
        # 合并所有 work_item 文本
        work_text = ' '.join(
            (w.get('title', '') + ' ' + w.get('desc', ''))
            for w in exp.get('work_items', [])
        ).lower()
        full_text = tags_lower + ' ' + work_text

        score = 0
        matched_kw = []
        for kw in jd_all_kw:
            if kw in full_text:
                score += 2
                matched_kw.append(kw)
            elif kw in tags_lower:
                score += 3  # 标签匹配权重更高
                matched_kw.append(kw)

        scored.append({
            **exp,
            '_score': score,
            '_matched': matched_kw,
        })

    # 先按分数降序，再按时间倒序
    def sort_key(e):
        ts = e.get('time_start', '0000/00')
        return (-e['_score'], ts)  # 分数高优先，同分按时间早的先（会被翻转）

    scored.sort(key=sort_key)

    # 取 top N，但至少保留分数 > 0 的
    selected = [e for e in scored if e['_score'] > 0][:max_count]

    # 如果选中不够，补充分数为 0 的
    if len(selected) < 2:
        remaining = [e for e in scored if e['_score'] == 0]
        selected.extend(remaining[:max_count - len(selected)])

    # 按时间倒序排列（最新在前）
    def time_sort(e):
        ts = e.get('time_start', '0000/00')
        return ts

    selected.sort(key=time_sort, reverse=True)

    return selected


# ─── LaTeX 生成 ───────────────────────────────────────────────

def _gen_education_section(profile: dict, jd_keywords: dict) -> str:
    """生成教育背景 section"""
    lines = [r'\section{教育背景}']
    jd_kw_set = set(k.lower() for cat in ['tech', 'domain'] for k in jd_keywords.get(cat, []))

    for edu in profile.get('education', []):
        if not edu.get('school'):
            continue
        school = tex_escape(edu['school'])
        degree = tex_escape(edu.get('degree', ''))
        time = tex_escape(edu.get('time', ''))
        major = tex_escape(edu.get('major', ''))
        dept = tex_escape(edu.get('department', ''))
        gpa = tex_escape(edu.get('gpa', ''))
        rank = tex_escape(edu.get('rank', ''))

        lines.append(rf'\datedsubsection{{\textbf{{{school}}} \quad \normalsize {degree}}}{{{time}}}')

        # 专业 + 学院 + GPA
        info_parts = []
        if major:
            info_parts.append(major)
        if dept:
            info_parts.append(dept)
        info_line = r'\textit{' + r' \quad '.join(info_parts) + '}'
        if gpa:
            info_line += rf' \quad \textbf{{GPA：}} {gpa}'
            if rank:
                info_line += f'，排名{rank}'
        info_line += r' \\'
        lines.append(info_line)

        # 课程（按 JD 关键词加粗）
        courses_raw = edu.get('courses', '')
        if courses_raw:
            course_list = [c.strip() for c in re.split(r'[;；,，]', courses_raw) if c.strip()]
            formatted = []
            for c in course_list[:5]:
                c_esc = tex_escape(c)
                if any(kw in c.lower() for kw in jd_kw_set):
                    formatted.append(rf'\textbf{{{c_esc}}}')
                else:
                    formatted.append(c_esc)
            lines.append(r'\textbf{主修课程：} ' + '；'.join(formatted))

        lines.append('')

    return '\n'.join(lines)


def _gen_experience_section(experiences: list, section_title: str = '实习经历') -> str:
    """生成经历 section（实习 or 研究）"""
    if not experiences:
        return ''

    lines = [rf'\section{{{section_title}}}']

    for exp in experiences:
        company = tex_escape(exp.get('company', ''))
        city = tex_escape(exp.get('city', ''))
        role = tex_escape(exp.get('role', ''))
        dept = tex_escape(exp.get('department', ''))
        ts = tex_escape(exp.get('time_start', ''))
        te = tex_escape(exp.get('time_end', ''))

        lines.append(rf'\datedsubsection{{\textbf{{{company}}} \quad \normalsize {city}}}{{{ts} -- {te}}}')
        lines.append(rf'\role{{{role}}}{{{dept}}}')
        lines.append(r'\vspace{-6pt}')
        lines.append(r'\begin{itemize}')

        for wi in exp.get('work_items', [])[:3]:
            title = tex_escape(wi.get('title', ''))
            desc = tex_escape(wi.get('desc', ''))
            # 如果 title 不在 desc 中，加粗作为前缀
            if title and title not in desc:
                lines.append(rf'    \item \textbf{{{title}：}} {desc}')
            else:
                lines.append(rf'    \item {desc}')

        lines.append(r'\end{itemize}')
        lines.append(r'\vspace{-2pt}')
        lines.append('')

    return '\n'.join(lines)


def _gen_project_section(profile: dict) -> str:
    """生成项目经历 section"""
    projects = profile.get('projects', [])
    if not projects:
        return ''

    lines = [r'\section{项目经历}']
    for proj in projects:
        name = tex_escape(proj.get('name', ''))
        role = tex_escape(proj.get('role', ''))
        time = tex_escape(proj.get('time', ''))
        desc = tex_escape(proj.get('desc', ''))
        tags = tex_escape(proj.get('tags', ''))

        lines.append(rf'\datedsubsection{{\textbf{{{name}}}}}{{{time}}}')
        if role:
            tag_str = tags if tags else ''
            lines.append(rf'\role{{{role}}}{{{tag_str}}}')
        lines.append(r'\vspace{-8pt}')
        lines.append(r'\begin{itemize}')
        lines.append(rf'    \item {desc}')
        lines.append(r'\end{itemize}')
        lines.append(r'\vspace{-4pt}')
        lines.append('')

    return '\n'.join(lines)


def _gen_publications_section(profile: dict) -> str:
    """生成论文发表 section"""
    pubs = profile.get('publications', [])
    if not pubs:
        return ''

    lines = [r'\section{论文发表}', r'\vspace{2pt}']
    for pub in pubs:
        title = tex_escape(pub.get('title', ''))
        authors = tex_escape(pub.get('authors', ''))
        venue = tex_escape(pub.get('venue', ''))

        lines.append(rf'\datedline{{\textbf{{{title}}}}}{{{venue}}}')
        lines.append(r'\vspace{-1pt}')
        lines.append(r'{\small ' + authors + '}')

    lines.append(r'\vspace{-4pt}')
    lines.append('')
    return '\n'.join(lines)


def _gen_awards_section(profile: dict) -> str:
    """生成获奖情况 section"""
    awards = profile.get('awards', [])
    awards = [a for a in awards if a.get('name')]
    if not awards:
        return ''

    lines = [r'\section{获奖情况}', r'\vspace{1pt}']
    for a in awards:
        name = tex_escape(a['name'])
        time = tex_escape(a.get('time', '--'))
        if not time:
            time = '--'
        lines.append(rf'\datedline{{\textit{{{name}}}}}{{{time}}}')

    lines.append(r'\vspace{-2pt}')
    lines.append('')
    return '\n'.join(lines)


def _gen_skills_section(profile: dict, jd_keywords: dict) -> str:
    """生成技能 section"""
    tech = profile.get('skills_tech', '')
    software = profile.get('skills_software', '')
    lang = profile.get('skills_lang', '')

    if not tech and not software and not lang:
        return ''

    lines = [r'\section{技能}', r'\begin{itemize}[parsep=0.5ex]']

    if tech:
        lines.append(rf'    \item \textbf{{编程语言：}} {tex_escape(tech)}')
    if software:
        sw_line = rf'    \item \textbf{{工具：}} {tex_escape(software)}'
        if lang:
            sw_line += rf' \quad \textbf{{语言：}} {tex_escape(lang)}'
        lines.append(sw_line)
    elif lang:
        lines.append(rf'    \item \textbf{{语言：}} {tex_escape(lang)}')

    lines.append(r'\end{itemize}')
    lines.append('')
    return '\n'.join(lines)


def generate_latex(profile: dict, experiences: list, jd_keywords: dict) -> str:
    """组装完整 .tex 文件"""

    # Header
    name = f"{tex_escape(profile.get('name_zh', ''))} {tex_escape(profile.get('name_en', ''))}"
    email = profile.get('email', '')
    phone = profile.get('phone', '')
    github = profile.get('github', '')
    linkedin = profile.get('linkedin', '')

    header_lines = [
        r'% !TEX TS-program = xelatex',
        r'% !TEX encoding = UTF-8 Unicode',
        r'',
        r'\documentclass{resume}',
        r'\usepackage{zh_CN-Adobefonts_external}',
        r'\usepackage{linespacing_fix}',
        r'\usepackage{cite}',
        r'',
        r'\begin{document}',
        r'\begin{Form}',
        r'',
        r'\pagenumbering{gobble}',
        r'',
        rf'\name{{{name}}}',
        r'',
    ]

    # Basic info line
    info_parts = []
    if email:
        info_parts.append(rf'\email{{{email}}}')
    if phone:
        info_parts.append(rf'\phone{{{phone}}}')
    if github:
        gh_user = github.rstrip('/').split('/')[-1]
        info_parts.append(rf'\github[{gh_user}]{{{github}}}')
    if linkedin:
        li_user = linkedin.rstrip('/').split('/')[-1]
        info_parts.append(rf'\linkedin[{li_user}]{{{linkedin}}}')

    header_lines.append(r'\basicInfo{')
    header_lines.append(r' \textperiodcentered '.join(info_parts))
    header_lines.append('}')
    header_lines.append(r'\vspace{-8pt}')
    header_lines.append('')

    # Sections
    sections = []

    # 教育背景
    sections.append(_gen_education_section(profile, jd_keywords))

    # 经历（分 研究 vs 实习）
    research_exp = [e for e in experiences if '研究' in e.get('role', '') or '研究' in e.get('company', '')]
    intern_exp = [e for e in experiences if e not in research_exp]

    if research_exp:
        sections.append(_gen_experience_section(research_exp, '研究经历'))
    if intern_exp:
        sections.append(_gen_experience_section(intern_exp, '实习经历'))

    # 项目
    sections.append(_gen_project_section(profile))

    # 论文
    sections.append(_gen_publications_section(profile))

    # 获奖
    sections.append(_gen_awards_section(profile))

    # 技能
    sections.append(_gen_skills_section(profile, jd_keywords))

    # Footer
    footer = [
        r'\end{Form}',
        r'\end{document}',
    ]

    all_sections = [s for s in sections if s.strip()]
    return '\n'.join(header_lines) + '\n' + '\n'.join(all_sections) + '\n' + '\n'.join(footer) + '\n'


# ─── 自动调优 ─────────────────────────────────────────────────

def _tune_overflow(tex_path: Path, cls_path: Path, fill_data: dict, log_lines: list) -> list:
    """溢出时自动调优。按优先级逐步调整，返回操作记录列表。"""
    actions = []

    # 调优策略列表（按优先级）
    strategies = [
        _tune_reduce_vspace,
        _tune_reduce_margins,
        _tune_reduce_list_spacing,
        _tune_remove_research,
        _tune_reduce_font_size,
        _tune_reduce_section_spacing,
    ]

    for strategy in strategies:
        applied = strategy(tex_path, cls_path)
        if applied:
            actions.append(applied)
            # 重新编译检查
            result = _compile_and_check(tex_path.parent)
            if result and result.get('ratio', 999) <= 1.0:
                actions.append(f"✅ 填充率调整至 {result['ratio']*100:.1f}%")
                return actions

    return actions


def _tune_underfill(tex_path: Path, cls_path: Path, fill_data: dict, log_lines: list) -> list:
    """内容偏空时自动调优。逐步增大间距，每步编译检查，直到 95-99%。"""
    actions = []
    ratio = fill_data.get('ratio', 0)

    if ratio >= 0.95:
        return actions

    # 扩展步骤表：(文件类型, 旧值, 新值, 描述)
    # 按影响从小到大排列，每步编译后检查
    expansion_steps = [
        # ── 列表间距（逐级恢复/增大） ──
        ('cls', 'itemsep=0.05em', 'itemsep=0.1em', '列表间距 0.05→0.1em'),
        ('cls', 'topsep=0.05em', 'topsep=0.1em', '列表顶距 0.05→0.1em'),
        ('cls', 'itemsep=0.1em', 'itemsep=0.15em', '列表间距 0.1→0.15em'),
        ('cls', 'topsep=0.1em', 'topsep=0.15em', '列表顶距 0.1→0.15em'),
        ('cls', 'itemsep=0.15em', 'itemsep=0.2em', '列表间距 0.15→0.2em'),
        ('cls', 'topsep=0.15em', 'topsep=0.2em', '列表顶距 0.15→0.2em'),
        # ── section 间距（逐级恢复） ──
        ('cls', r'*1.0}{*0.8}', r'*1.2}{*1.0}', 'section 间距 *1.0/*0.8→*1.2/*1.0'),
        ('cls', r'*1.2}{*1.0}', r'*1.5}{*1.3}', 'section 间距 *1.2/*1.0→*1.5/*1.3'),
        # ── 页边距（逐级恢复） ──
        ('cls', 'top=0.4in', 'top=0.45in', '上边距 0.4→0.45in'),
        ('cls', 'bottom=0.4in', 'bottom=0.45in', '下边距 0.4→0.45in'),
        ('cls', 'top=0.45in', 'top=0.5in', '上边距 0.45→0.5in'),
        ('cls', 'bottom=0.45in', 'bottom=0.5in', '下边距 0.45→0.5in'),
        # ── vspace 恢复 ──
        ('tex', r'\vspace{-8pt}', r'\vspace{-6pt}', 'vspace -8→-6pt'),
        ('tex', r'\vspace{-6pt}', r'\vspace{-4pt}', 'vspace -6→-4pt'),
        ('tex', r'\vspace{-4pt}', r'\vspace{-2pt}', 'vspace -4→-2pt'),
        ('tex', r'\vspace{-1pt}', r'\vspace{1pt}', 'vspace -1→1pt'),
        ('tex', r'\vspace{0pt}', r'\vspace{2pt}', 'vspace 0→2pt'),
        # ── 字号恢复 ──
        ('cls', r'\LoadClass[9pt]{article}', r'\LoadClass[9.5pt]{article}', '字号 9→9.5pt'),
        ('cls', r'\LoadClass[9.5pt]{article}', r'\LoadClass[10pt]{article}', '字号 9.5→10pt'),
        # ── 继续增大列表间距 ──
        ('cls', 'itemsep=0.2em', 'itemsep=0.25em', '列表间距 0.2→0.25em'),
        ('cls', 'topsep=0.2em', 'topsep=0.25em', '列表顶距 0.2→0.25em'),
        ('cls', 'itemsep=0.25em', 'itemsep=0.3em', '列表间距 0.25→0.3em'),
        ('cls', 'topsep=0.25em', 'topsep=0.3em', '列表顶距 0.25→0.3em'),
        # ── section 间距继续增大 ──
        ('cls', r'*1.5}{*1.3}', r'*1.8}{*1.5}', 'section 间距 *1.5/*1.3→*1.8/*1.5'),
        # ── 页边距继续增大 ──
        ('cls', 'top=0.5in', 'top=0.55in', '上边距 0.5→0.55in'),
        ('cls', 'bottom=0.5in', 'bottom=0.55in', '下边距 0.5→0.55in'),
    ]

    for file_type, old_val, new_val, desc in expansion_steps:
        path = cls_path if file_type == 'cls' else tex_path
        content = path.read_text(encoding='utf-8')

        if old_val not in content:
            continue

        # 应用替换
        content = content.replace(old_val, new_val)
        path.write_text(content, encoding='utf-8')
        actions.append(desc)

        # 编译检查
        result = _compile_and_check(tex_path.parent)
        if not result:
            continue

        ratio = result.get('ratio', 0)
        if 0.95 <= ratio <= 0.99:
            actions.append(f'✅ 填充率调整至 {ratio*100:.1f}%')
            return actions

        if ratio > 0.99:
            # 过满，回退此步
            content = path.read_text(encoding='utf-8')
            content = content.replace(new_val, old_val)
            path.write_text(content, encoding='utf-8')
            actions[-1] = f'↩️ 回退 {desc}（{ratio*100:.1f}% 超上限）'
            # 尝试到此为止
            break

    return actions


def _tune_reduce_vspace(tex_path: Path, cls_path: Path) -> str:
    """缩小 tex 中的 vspace"""
    content = tex_path.read_text(encoding='utf-8')
    changed = False

    # \vspace{-6pt} → \vspace{-8pt}
    if r'\vspace{-6pt}' in content:
        content = content.replace(r'\vspace{-6pt}', r'\vspace{-8pt}')
        changed = True

    # \vspace{-2pt} → \vspace{-4pt}
    if r'\vspace{-2pt}' in content:
        content = content.replace(r'\vspace{-2pt}', r'\vspace{-4pt}')
        changed = True

    # \vspace{-4pt} at end of itemize → \vspace{-6pt}
    if r'\vspace{-4pt}' in content and not changed:
        content = content.replace(r'\vspace{-4pt}', r'\vspace{-6pt}')
        changed = True

    # \vspace{2pt} → \vspace{0pt}
    if r'\vspace{2pt}' in content:
        content = content.replace(r'\vspace{2pt}', r'\vspace{0pt}')
        changed = True

    # \vspace{1pt} → \vspace{-1pt}
    if r'\vspace{1pt}' in content:
        content = content.replace(r'\vspace{1pt}', r'\vspace{-1pt}')
        changed = True

    if changed:
        tex_path.write_text(content, encoding='utf-8')
        return '缩小 vspace 间距'
    return ''


def _tune_reduce_margins(tex_path: Path, cls_path: Path) -> str:
    """缩小页边距（不低于 0.35in）"""
    content = cls_path.read_text(encoding='utf-8')
    changed = False

    for old, new in [
        ('top=0.5in', 'top=0.4in'),
        ('bottom=0.5in', 'bottom=0.4in'),
    ]:
        if old in content:
            content = content.replace(old, new)
            changed = True

    if changed:
        cls_path.write_text(content, encoding='utf-8')
        return '缩小页边距 0.5in → 0.4in'
    return ''


def _tune_reduce_list_spacing(tex_path: Path, cls_path: Path) -> str:
    """缩小列表间距"""
    content = cls_path.read_text(encoding='utf-8')
    changed = False

    for old, new in [
        ('itemsep=0.2em', 'itemsep=0.1em'),
        ('topsep=0.2em', 'topsep=0.1em'),
    ]:
        if old in content:
            content = content.replace(old, new)
            changed = True

    if changed:
        cls_path.write_text(content, encoding='utf-8')
        return '缩小列表间距 0.2em → 0.1em'
    return ''


def _tune_remove_research(tex_path: Path, cls_path: Path) -> str:
    """注释掉研究经历 section"""
    content = tex_path.read_text(encoding='utf-8')
    lines = content.split('\n')

    # 找到 \section{研究经历} 所在行
    start_idx = None
    for i, line in enumerate(lines):
        if r'\section{研究经历}' in line:
            start_idx = i
            break

    if start_idx is None:
        return ''

    # 找到下一个 \section{ 或 \end{Form} 所在行
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        stripped = lines[i].lstrip()
        if stripped.startswith(r'\section{') or stripped.startswith(r'\end{Form}'):
            end_idx = i
            break

    # 只注释 start_idx 到 end_idx-1 的行
    for i in range(start_idx, end_idx):
        if lines[i] and not lines[i].startswith('% '):
            lines[i] = '% ' + lines[i]

    content = '\n'.join(lines)
    tex_path.write_text(content, encoding='utf-8')
    return '删除研究经历 section'


def _tune_reduce_font_size(tex_path: Path, cls_path: Path) -> str:
    """缩小字号（不低于 9pt）"""
    content = cls_path.read_text(encoding='utf-8')

    for old, new in [
        (r'\LoadClass[10pt]{article}', r'\LoadClass[9.5pt]{article}'),
        (r'\LoadClass[9.5pt]{article}', r'\LoadClass[9pt]{article}'),
    ]:
        if old in content:
            content = content.replace(old, new)
            cls_path.write_text(content, encoding='utf-8')
            return f'缩小字号: {old.split("[")[1].split("]")[0]}'
    return ''


def _tune_reduce_section_spacing(tex_path: Path, cls_path: Path) -> str:
    """缩小 section 间距"""
    content = cls_path.read_text(encoding='utf-8')

    if r'*1.5}{*1.3}' in content:
        content = content.replace(r'*1.5}{*1.3}', r'*1.0}{*0.8}')
        cls_path.write_text(content, encoding='utf-8')
        return '缩小 section 间距 *1.5/*1.3 → *1.0/*0.8'
    return ''


def _compile_and_check(output_dir: Path) -> dict:
    """编译并检查填充率，返回 fill_data 或 None"""
    xelatex = find_xelatex()
    tex_file = output_dir / 'resume-zh_CN.tex'

    result = subprocess.run(
        [xelatex, '-interaction=nonstopmode', tex_file.name],
        cwd=str(output_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )

    try:
        from tools.page_fill_check import inject_measurement, remove_measurement, parse_fill_ratio

        injected = inject_measurement(tex_file)
        subprocess.run(
            [xelatex, '-interaction=nonstopmode', tex_file.name],
            cwd=str(output_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )

        aux_file = output_dir / 'resume-zh_CN.aux'
        fill_data = parse_fill_ratio(aux_file)

        if injected:
            remove_measurement(tex_file)
            # 重新编译还原干净 PDF
            subprocess.run(
                [xelatex, '-interaction=nonstopmode', tex_file.name],
                cwd=str(output_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )

        return fill_data
    except Exception:
        return None


# ─── 编译 ─────────────────────────────────────────────────────

def find_xelatex() -> str:
    """查找 xelatex 路径"""
    candidates = [
        Path.home() / 'Library' / 'TinyTeX' / 'bin' / 'universal-darwin' / 'xelatex',
        Path.home() / '.TinyTeX' / 'bin' / 'x86_64-linux' / 'xelatex',
        Path('/usr/local/bin/xelatex'),
        Path('/usr/bin/xelatex'),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return 'xelatex'


def compile_latex(output_dir: Path, xelatex: str = None) -> dict:
    """编译 LaTeX，返回 {success, pdf_path, log}"""
    if not xelatex:
        xelatex = find_xelatex()

    tex_file = output_dir / 'resume-zh_CN.tex'

    result = subprocess.run(
        [xelatex, '-interaction=nonstopmode', tex_file.name],
        cwd=str(output_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )

    pdf_file = output_dir / 'resume-zh_CN.pdf'
    success = pdf_file.exists() and pdf_file.stat().st_size > 0

    return {
        'success': success,
        'pdf_path': str(pdf_file) if success else None,
        'log': result.stdout[-2000:] if result.stdout else '',
        'returncode': result.returncode,
    }


# ─── 主入口 ───────────────────────────────────────────────────

def generate_resume(jd_text: str, interview_text: str = '', *,
                    company: str = '', role: str = '') -> dict:
    """
    完整的简历生成流程。

    参数:
        jd_text: JD 原文
        interview_text: 面经文本（可选）
        company: 公司名覆盖（可选，优先于 JD 提取结果）
        role: 岗位名覆盖（可选，优先于 JD 提取结果）

    返回:
        {
            success: bool,
            output_dir: str,
            pdf_path: str,       # 相对于 output/ 的路径
            company: str,
            role: str,
            fill_ratio: float,
            generation_log: str,
            error: str | None,
        }
    """
    log_lines = []

    # Step 1: 加载数据
    log_lines.append('## 步骤 1: 加载用户数据')
    profile = _parse_profile()
    if not profile.get('name_zh'):
        return {'success': False, 'error': '请先在 data/profile.md 中填写个人信息'}

    experiences = load_all_experiences()
    if not experiences:
        return {'success': False, 'error': '请先在 data/experiences/ 中添加至少一段经历'}

    log_lines.append(f'- 个人信息: {profile["name_zh"]}')
    log_lines.append(f'- 经历数量: {len(experiences)} 段')

    # Step 2: 分析 JD
    log_lines.append('\n## 步骤 2: 分析 JD 关键词')

    # 合并面经关键词
    combined_text = jd_text
    if interview_text:
        combined_text += '\n' + interview_text

    jd_keywords = extract_jd_keywords(combined_text)
    log_lines.append(f'- 技术关键词: {", ".join(jd_keywords["tech"][:10])}')
    log_lines.append(f'- 领域关键词: {", ".join(jd_keywords["domain"][:10])}')
    log_lines.append(f'- 公司: {jd_keywords["company"]}')
    log_lines.append(f'- 岗位: {jd_keywords["role"]}')

    # Step 3: 匹配经历
    log_lines.append('\n## 步骤 3: 匹配经历')
    matched = match_experiences(experiences, jd_keywords)
    for i, exp in enumerate(matched):
        score = exp.get('_score', 0)
        kws = ', '.join(exp.get('_matched', [])[:5])
        log_lines.append(f'- [{score}分] {exp["company"]} - {exp["role"]} (匹配: {kws})')

    # Step 4: 生成 LaTeX
    log_lines.append('\n## 步骤 4: 生成 LaTeX')
    tex_content = generate_latex(profile, matched, jd_keywords)
    log_lines.append(f'- LaTeX 行数: {len(tex_content.splitlines())}')

    # Step 5: 准备输出目录
    # Step 5: 准备输出目录（用户指定的 company/role 优先）
    company = company or jd_keywords.get('company', '未知公司') or '未知公司'
    role_ = role or jd_keywords.get('role', '未知岗位') or '未知岗位'
    # 清理文件名
    company_clean = re.sub(r'[/\\:*?"<>|]', '', company)[:20]
    role_clean = re.sub(r'[/\\:*?"<>|]', '', role_)[:20]
    date_str = datetime.now().strftime('%Y%m%d')
    dir_name = f'{company_clean}_{role_clean}_{date_str}'

    output_dir = OUTPUT_DIR / dir_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # 复制模板文件（fonts, cls, sty）
    for item in LATEX_TEMPLATE_DIR.iterdir():
        if item.name.endswith('.tex'):
            continue  # 不复制模板 tex
        dest = output_dir / item.name
        if item.is_dir() and not dest.exists():
            shutil.copytree(item, dest)
        elif item.is_file() and not dest.exists():
            shutil.copy2(item, dest)

    # 写入 tex
    tex_path = output_dir / 'resume-zh_CN.tex'
    tex_path.write_text(tex_content, encoding='utf-8')
    log_lines.append(f'- 输出目录: {dir_name}')

    # Step 6: 编译
    log_lines.append('\n## 步骤 5: 编译 PDF')
    compile_result = compile_latex(output_dir)

    if not compile_result['success']:
        log_lines.append('- 编译失败')
        # 写 log
        log_path = output_dir / 'generation_log.md'
        log_path.write_text('# Generation Log\n\n' + '\n'.join(log_lines), encoding='utf-8')
        return {
            'success': False,
            'error': '编译失败，请检查 LaTeX 日志',
            'output_dir': dir_name,
            'log': compile_result.get('log', ''),
        }

    log_lines.append('- 编译成功')

    # Step 7: 填充率检查
    log_lines.append('\n## 步骤 6: 填充率检查')
    fill_ratio = 0.0
    fill_result = {}
    try:
        from tools.page_fill_check import check_page_fill
        fill_result = check_page_fill(str(output_dir))
        fill_ratio = fill_result.get('ratio', 0)
        page_count = fill_result.get('page_count', 1)
        log_lines.append(f'- 填充率: {fill_ratio * 100:.1f}%')
        log_lines.append(f'- 页数: {page_count}')
        log_lines.append(f'- 状态: {fill_result.get("message", "")}')
    except Exception as e:
        log_lines.append(f'- 填充率检查失败: {e}')
        fill_ratio = -1

    # Step 8: 自动调优（溢出或偏空时）
    tex_path = output_dir / 'resume-zh_CN.tex'
    cls_path = output_dir / 'resume.cls'
    tuning_applied = False

    if fill_ratio > 1.0:
        log_lines.append('\n## 步骤 7: 自动调优（溢出）')
        actions = _tune_overflow(tex_path, cls_path, fill_result, log_lines)
        for a in actions:
            log_lines.append(f'- {a}')
        if actions:
            tuning_applied = True
            # 溢出调优后重新编译 + 检查
            compile_result2 = compile_latex(output_dir)
            if compile_result2['success']:
                try:
                    fill_result = check_page_fill(str(output_dir))
                    fill_ratio = fill_result.get('ratio', fill_ratio)
                    log_lines.append(f'- 溢出调优后填充率: {fill_ratio * 100:.1f}%')
                except Exception:
                    pass

    if 0 < fill_ratio < 0.95:
        log_lines.append('\n## 步骤 7b: 自动调优（填充）')
        actions = _tune_underfill(tex_path, cls_path,
                                  fill_result if fill_result else {'ratio': fill_ratio},
                                  log_lines)
        for a in actions:
            log_lines.append(f'- {a}')
        if actions:
            tuning_applied = True

    # 调优后重新编译 + 检查
    if tuning_applied:
        compile_result2 = compile_latex(output_dir)
        if compile_result2['success']:
            try:
                fill_result2 = check_page_fill(str(output_dir))
                fill_ratio = fill_result2.get('ratio', fill_ratio)
                log_lines.append(f'- 最终填充率: {fill_ratio * 100:.1f}%')
                log_lines.append(f'- 最终页数: {fill_result2.get("page_count", 1)}')
            except Exception:
                pass

    # 写 generation_log.md
    log_lines.append(f'\n## 生成时间\n{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    log_path = output_dir / 'generation_log.md'
    log_path.write_text('# Generation Log\n\n' + '\n'.join(log_lines), encoding='utf-8')

    pdf_rel = f'{dir_name}/resume-zh_CN.pdf'

    return {
        'success': True,
        'output_dir': dir_name,
        'pdf_path': pdf_rel,
        'company': company,
        'role': role_,
        'fill_ratio': fill_ratio,
        'generation_log': '\n'.join(log_lines),
        'error': None,
    }


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("用法: python3 tools/generate_resume.py '你的JD文本'")
        sys.exit(1)
    jd = sys.argv[1]
    result = generate_resume(jd)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
