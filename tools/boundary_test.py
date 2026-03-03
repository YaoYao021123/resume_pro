#!/usr/bin/env python3
"""单页容量边界测试

测量不同 section 配置下，一页简历最多能放多少 bullet，
使页面填充率控制在 95-99% 的理想区间。

使用方式：
    python3 tools/boundary_test.py

输出：n_sections | max_bullets | approx_chars | fill_rate 的结果表
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ─── 导入本项目模块 ─────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.generate_resume import find_xelatex, LATEX_TEMPLATE_DIR
from tools.page_fill_check import inject_measurement, remove_measurement, parse_fill_ratio

# ─── 常量 ──────────────────────────────────────────────────────

TARGET_LOW = 0.95
TARGET_HIGH = 0.99

# 标准 bullet：约 80 个中文字符，排版后大约占 2 行
STANDARD_BULLET = (
    '负责搭建端到端的数据分析流水线，覆盖数据采集、清洗、特征工程与模型训练全流程，'
    '累计处理超过五千万条用户行为日志，将核心指标的预测准确率提升了百分之十五以上'
)

# Section 配置方案
SECTION_CONFIGS = [
    {
        'n': 3,
        'label': '教育背景 + 实习经历 + 技能',
        'sections': ['education', 'intern', 'skills'],
    },
    {
        'n': 4,
        'label': '教育背景 + 实习经历 + 项目经历 + 技能',
        'sections': ['education', 'intern', 'project', 'skills'],
    },
    {
        'n': 5,
        'label': '教育背景 + 实习经历 + 项目经历 + 获奖情况 + 技能',
        'sections': ['education', 'intern', 'project', 'awards', 'skills'],
    },
    {
        'n': 6,
        'label': '教育背景 + 研究经历 + 实习经历 + 项目经历 + 获奖情况 + 技能',
        'sections': ['education', 'research', 'intern', 'project', 'awards', 'skills'],
    },
    {
        'n': 7,
        'label': '教育背景 + 研究经历 + 实习经历 + 项目经历 + 论文发表 + 获奖情况 + 技能',
        'sections': ['education', 'research', 'intern', 'project', 'publications', 'awards', 'skills'],
    },
]


# ─── 生成测试 .tex 文件 ─────────────────────────────────────

def _gen_header() -> str:
    return r"""\documentclass{resume}
\usepackage{zh_CN-Adobefonts_external}
\usepackage{linespacing_fix}
\usepackage{cite}

\begin{document}
\begin{Form}

\pagenumbering{gobble}

\name{张三 San Zhang}

\basicInfo{
\email{zhangsan@example.com} \textperiodcentered
\phone{(+86) 138-0000-0000} \textperiodcentered
\github[zhangsan]{https://github.com/zhangsan}
}
\vspace{-8pt}
"""


def _gen_education() -> str:
    return r"""
\section{教育背景}
\datedsubsection{\textbf{北京大学} \quad \normalsize 硕士}{2022/09 -- 2025/06}
\textit{计算机科学与技术 \quad 信息科学技术学院} \quad \textbf{GPA：} 3.85/4.0，排名前5\% \\
\textbf{主修课程：} 机器学习；深度学习；自然语言处理；数据挖掘

\datedsubsection{\textbf{清华大学} \quad \normalsize 本科}{2018/09 -- 2022/06}
\textit{软件工程 \quad 软件学院} \quad \textbf{GPA：} 3.78/4.0，排名前10\% \\
\textbf{主修课程：} 数据结构；算法设计；数据库原理；操作系统
"""


def _gen_intern_bullets(n_bullets: int) -> str:
    """生成实习经历 section，包含 n_bullets 条 bullet，分成若干段经历"""
    lines = [r'\section{实习经历}']

    # 每段经历 3 条 bullet，分配到多段
    bullets_per_exp = 3
    n_exps = max(1, (n_bullets + bullets_per_exp - 1) // bullets_per_exp)

    companies = [
        ('腾讯', '深圳', '数据分析师', 'PCG 内容与平台事业群', '2024/06', '2024/09'),
        ('百度', '北京', '算法工程师', '搜索技术平台研发部', '2024/01', '2024/05'),
        ('阿里巴巴', '杭州', '数据科学家', '达摩院语言技术实验室', '2023/06', '2023/09'),
        ('字节跳动', '北京', '策略研发', '推荐架构团队', '2023/01', '2023/05'),
        ('美团', '北京', '后端开发', '到店事业群', '2022/06', '2022/09'),
    ]

    remaining = n_bullets
    for i in range(min(n_exps, len(companies))):
        if remaining <= 0:
            break
        comp, city, role, dept, ts, te = companies[i]
        lines.append(rf'\datedsubsection{{\textbf{{{comp}}} \quad \normalsize {city}}}{{{ts} -- {te}}}')
        lines.append(rf'\role{{{role}}}{{{dept}}}')
        lines.append(r'\vspace{-6pt}')
        lines.append(r'\begin{itemize}')

        count = min(bullets_per_exp, remaining)
        for _ in range(count):
            lines.append(rf'    \item {STANDARD_BULLET}')
            remaining -= 1

        lines.append(r'\end{itemize}')
        lines.append(r'\vspace{-2pt}')
        lines.append('')

    return '\n'.join(lines)


def _gen_research_bullets(n_bullets: int) -> str:
    """生成研究经历 section"""
    lines = [r'\section{研究经历}']
    lines.append(r'\datedsubsection{\textbf{北京大学} \quad \normalsize 北京}{2023/09 -- 2024/06}')
    lines.append(r'\role{研究助理}{自然语言处理实验室}')
    lines.append(r'\vspace{-6pt}')
    lines.append(r'\begin{itemize}')
    for _ in range(n_bullets):
        lines.append(rf'    \item {STANDARD_BULLET}')
    lines.append(r'\end{itemize}')
    lines.append(r'\vspace{-2pt}')
    lines.append('')
    return '\n'.join(lines)


def _gen_project() -> str:
    return r"""
\section{项目经历}
\datedsubsection{\textbf{智能问答系统}}{2023/03 -- 2023/06}
\role{项目负责人}{NLP + RAG}
\vspace{-8pt}
\begin{itemize}
    \item """ + STANDARD_BULLET + r"""
\end{itemize}
\vspace{-4pt}
"""


def _gen_publications() -> str:
    return r"""
\section{论文发表}
\vspace{2pt}
\datedline{\textbf{A Novel Approach to Cross-lingual Transfer Learning}}{ACL 2024}
\vspace{-1pt}
{\small Zhang San, Li Si, Wang Wu}
\vspace{-4pt}
"""


def _gen_awards() -> str:
    return r"""
\section{获奖情况}
\vspace{1pt}
\datedline{\textit{国家奖学金}}{2024/10}
\datedline{\textit{校级优秀学生}}{2023/10}
\vspace{-2pt}
"""


def _gen_skills() -> str:
    return r"""
\section{技能}
\begin{itemize}[parsep=0.5ex]
    \item \textbf{编程语言：} Python, Java, C++, SQL, R
    \item \textbf{工具：} PyTorch, PySpark, Docker, Git, Tableau \quad \textbf{语言：} 英语（TOEFL 105）
\end{itemize}
"""


def _gen_footer() -> str:
    return r"""
\end{Form}
\end{document}
"""


def generate_test_tex(config: dict, n_intern_bullets: int) -> str:
    """根据 section 配置和 bullet 数量生成测试 .tex"""
    parts = [_gen_header()]

    for sec in config['sections']:
        if sec == 'education':
            parts.append(_gen_education())
        elif sec == 'intern':
            parts.append(_gen_intern_bullets(n_intern_bullets))
        elif sec == 'research':
            parts.append(_gen_research_bullets(2))  # 研究经历固定 2 条
        elif sec == 'project':
            parts.append(_gen_project())
        elif sec == 'publications':
            parts.append(_gen_publications())
        elif sec == 'awards':
            parts.append(_gen_awards())
        elif sec == 'skills':
            parts.append(_gen_skills())

    parts.append(_gen_footer())
    return '\n'.join(parts)


# ─── 编译 & 测量 ────────────────────────────────────────────

def compile_and_measure(tex_content: str, work_dir: Path, xelatex: str) -> float | None:
    """编译 tex 并返回填充率，失败返回 None"""
    tex_file = work_dir / 'resume-zh_CN.tex'
    aux_file = work_dir / 'resume-zh_CN.aux'

    tex_file.write_text(tex_content, encoding='utf-8')

    # 注入测量代码
    inject_measurement(tex_file)

    # 编译
    result = subprocess.run(
        [xelatex, '-interaction=nonstopmode', tex_file.name],
        cwd=str(work_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )

    if not aux_file.exists():
        return None

    try:
        data = parse_fill_ratio(aux_file)
        return data['ratio']
    except Exception:
        return None


def setup_work_dir(tmpdir: Path):
    """复制 LaTeX 模板资源到工作目录"""
    for item in LATEX_TEMPLATE_DIR.iterdir():
        if item.name.endswith('.tex'):
            continue
        dest = tmpdir / item.name
        if item.is_dir() and not dest.exists():
            shutil.copytree(item, dest)
        elif item.is_file() and not dest.exists():
            shutil.copy2(item, dest)


# ─── 二分搜索 ───────────────────────────────────────────────

def find_max_bullets(config: dict, xelatex: str) -> dict:
    """用二分搜索找到使填充率在 95-99% 的 bullet 数量"""

    with tempfile.TemporaryDirectory(prefix='boundary_test_') as tmpdir:
        work_dir = Path(tmpdir)
        setup_work_dir(work_dir)

        lo, hi = 1, 30
        best = {'n_bullets': 0, 'fill_rate': 0.0}

        while lo <= hi:
            mid = (lo + hi) // 2
            tex = generate_test_tex(config, mid)
            ratio = compile_and_measure(tex, work_dir, xelatex)

            if ratio is None:
                # 编译失败，减少 bullet
                hi = mid - 1
                continue

            if TARGET_LOW <= ratio <= TARGET_HIGH:
                best = {'n_bullets': mid, 'fill_rate': ratio}
                # 尝试更多 bullet 看是否还在范围内
                lo = mid + 1
            elif ratio < TARGET_LOW:
                lo = mid + 1
            else:
                # ratio > TARGET_HIGH (可能溢出)
                hi = mid - 1

            # 记录最近一个在 ≤1.0 范围的
            if ratio <= 1.0 and ratio > best.get('fill_rate', 0):
                best = {'n_bullets': mid, 'fill_rate': ratio}

        # 最终验证 best
        if best['n_bullets'] > 0 and not (TARGET_LOW <= best['fill_rate'] <= TARGET_HIGH):
            # 线性扫描 best-2 到 best+2 找精确值
            for n in range(max(1, best['n_bullets'] - 2), best['n_bullets'] + 3):
                tex = generate_test_tex(config, n)
                ratio = compile_and_measure(tex, work_dir, xelatex)
                if ratio and TARGET_LOW <= ratio <= TARGET_HIGH:
                    best = {'n_bullets': n, 'fill_rate': ratio}
                    break
                if ratio and ratio <= 1.0 and ratio > best.get('fill_rate', 0):
                    best = {'n_bullets': n, 'fill_rate': ratio}

        return best


# ─── 主流程 ──────────────────────────────────────────────────

def main():
    xelatex = find_xelatex()
    print(f"使用 xelatex: {xelatex}")
    print(f"目标填充率: {TARGET_LOW*100:.0f}% ~ {TARGET_HIGH*100:.0f}%")
    print(f"标准 bullet 长度: {len(STANDARD_BULLET)} 字符 (约 2 行)")
    print()

    # 表头
    print(f"{'n_sections':>10} | {'max_bullets':>11} | {'approx_chars':>12} | {'fill_rate':>10} | {'sections'}")
    print('-' * 90)

    for config in SECTION_CONFIGS:
        print(f"  测试 {config['n']} sections: {config['label']} ...", end='', flush=True)
        result = find_max_bullets(config, xelatex)
        n_bullets = result['n_bullets']
        fill_rate = result['fill_rate']
        approx_chars = n_bullets * len(STANDARD_BULLET)

        print(f"\r{config['n']:>10} | {n_bullets:>11} | {approx_chars:>12} | {fill_rate*100:>9.1f}% | {config['label']}")

    print()
    print("说明：max_bullets 为实习经历中的 bullet 总数（其他 section 使用固定内容）")
    print("      approx_chars 为 bullet 总字符数（每条约 80 中文字符）")


if __name__ == '__main__':
    main()
