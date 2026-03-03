#!/usr/bin/env python3
"""PDF 页面填充率检查工具

在 xelatex 编译后，检测简历 PDF 的页面填充率。
如果页面内容过少（底部留白过多），给出具体调优建议。

使用方式：
    python3 tools/page_fill_check.py <output_dir>

依赖：无外部依赖（纯 stdlib）

原理：
    在 .tex 文件末尾（\\end{document} 前）注入 LaTeX 测量代码，
    利用 \\pagetotal 和 \\pagegoal 获取内容高度和页面可用高度，
    写入 .aux 文件后由本脚本解析。
"""

import re
import sys
import subprocess
from pathlib import Path

# ─── 常量 ──────────────────────────────────────────────────────

FILL_MEASURE_SNIPPET = r"""
% === PAGE FILL MEASUREMENT (auto-injected, do not edit) ===
\makeatletter
\AtEndDocument{%
  \immediate\write\@auxout{%
    \string\newlabel{pagefill}{{\the\pagetotal}{\the\pagegoal}}%
  }%
}
\makeatother
% === END MEASUREMENT ===
"""

# 填充率阈值
THRESHOLD_UNDERFILL = 0.95   # 低于 95% 视为偏空
THRESHOLD_IDEAL_LOW = 0.95   # 理想范围下界
THRESHOLD_IDEAL_HIGH = 0.99  # 理想范围上界
THRESHOLD_OVERFLOW = 1.0     # 超过 100% 已溢出到第二页


# ─── 注入测量代码 ──────────────────────────────────────────────

def inject_measurement(tex_path: Path) -> bool:
    """在 .tex 文件的 \\begin{document} 之后注入测量代码。
    如果已注入则跳过。返回是否有修改。"""
    content = tex_path.read_text(encoding='utf-8')

    if 'PAGE FILL MEASUREMENT' in content:
        return False  # 已注入

    # 在 \begin{document} 后插入
    marker = r'\begin{document}'
    idx = content.find(marker)
    if idx == -1:
        raise ValueError(f"找不到 \\begin{{document}} in {tex_path}")

    insert_pos = idx + len(marker)
    new_content = content[:insert_pos] + FILL_MEASURE_SNIPPET + content[insert_pos:]
    tex_path.write_text(new_content, encoding='utf-8')
    return True


def remove_measurement(tex_path: Path) -> bool:
    """移除注入的测量代码，还原干净的 .tex 文件。"""
    content = tex_path.read_text(encoding='utf-8')

    if 'PAGE FILL MEASUREMENT' not in content:
        return False

    # 移除注入的代码块
    content = re.sub(
        r'\n% === PAGE FILL MEASUREMENT.*?% === END MEASUREMENT ===\n',
        '',
        content,
        flags=re.DOTALL
    )
    tex_path.write_text(content, encoding='utf-8')
    return True


# ─── 解析填充率 ───────────────────────────────────────────────

def parse_fill_ratio(aux_path: Path) -> dict:
    """从 .aux 文件解析 pagetotal 和 pagegoal。
    返回 {'total_pt': float, 'goal_pt': float, 'ratio': float, 'page_count': int}

    注意：\pagetotal 在 \AtEndDocument 时测量的是最后一页的内容高度。
    如果内容溢出到多页，需要通过 \@abspage@last 检测实际页数。
    多页时 ratio 用 (page_count - 1 + last_page_ratio) 来估算真实溢出量。
    """
    if not aux_path.exists():
        raise FileNotFoundError(f"找不到 .aux 文件: {aux_path}")

    content = aux_path.read_text(encoding='utf-8', errors='ignore')

    # 匹配 \newlabel{pagefill}{{123.456pt}{678.901pt}}
    m = re.search(r'\\newlabel\{pagefill\}\{\{([\d.]+)pt\}\{([\d.]+)pt\}\}', content)
    if not m:
        raise ValueError("在 .aux 文件中未找到 pagefill 标签，请确保已注入测量代码并编译")

    total_pt = float(m.group(1))  # 最后一页的内容高度
    goal_pt = float(m.group(2))   # 一页的可用高度

    # 检测总页数
    page_count = 1
    pm = re.search(r'\\@abspage@last\{(\d+)\}', content)
    if pm:
        page_count = int(pm.group(1))

    if page_count > 1:
        # 溢出情况：真实内容高度 = (page_count-1) * goal + last_page_total
        effective_total_pt = (page_count - 1) * goal_pt + total_pt
        ratio = effective_total_pt / goal_pt if goal_pt > 0 else 0
    else:
        effective_total_pt = total_pt
        ratio = total_pt / goal_pt if goal_pt > 0 else 0

    return {
        'total_pt': effective_total_pt,
        'goal_pt': goal_pt,
        'ratio': ratio,
        'page_count': page_count,
        'last_page_total_pt': total_pt,
        'total_mm': effective_total_pt * 0.3528,   # 1pt ≈ 0.3528mm
        'goal_mm': goal_pt * 0.3528,
        'remaining_mm': (goal_pt - effective_total_pt) * 0.3528,
    }


# ─── 生成建议 ─────────────────────────────────────────────────

def generate_advice(ratio: float, remaining_mm: float) -> dict:
    """根据填充率生成调优建议"""
    pct = ratio * 100

    if ratio > THRESHOLD_OVERFLOW:
        return {
            'status': 'overflow',
            'level': 'error',
            'message': f'页面内容溢出（{pct:.1f}%），需要缩减内容或调整排版',
            'suggestions': [
                '1. 检查是否有 bullet 末尾悬挂行（1-3字独占一行），改写收紧',
                '2. 缩小页边距：top/bottom 0.5in → 0.4in',
                '3. 缩小列表间距：itemsep/topsep 0.2em → 0.1em',
                '4. 删除最不相关的经历或研究经历',
                '5. 减少每段经历的 bullet 数量',
                '6. 缩小字号：10pt → 9.5pt（不低于 9pt）',
            ]
        }

    if ratio >= THRESHOLD_IDEAL_HIGH:
        return {
            'status': 'perfect',
            'level': 'success',
            'message': f'页面填充率 {pct:.1f}%，排版饱满',
            'suggestions': []
        }

    if ratio >= THRESHOLD_IDEAL_LOW:
        return {
            'status': 'good',
            'level': 'success',
            'message': f'页面填充率 {pct:.1f}%，在理想范围内',
            'suggestions': []
        }

    # 偏空 - 需要充实内容
    remaining = remaining_mm
    suggestions = []

    if remaining > 80:
        # 大量留白，建议加一整段经历
        suggestions.append('1. 增加一段经历（实习/项目/研究），每段约 3 条 bullet 可填充 ~60mm')
        suggestions.append('2. 如有项目经历或论文发表，考虑加入对应 section')
        suggestions.append('3. 给现有经历增加 bullet（每段最多 4 条）')
    elif remaining > 40:
        # 中等留白
        suggestions.append('1. 给现有经历各增加 1 条 bullet')
        suggestions.append('2. 考虑增加一段短经历或项目经历')
        suggestions.append('3. 展开现有 bullet 的描述，补充更多量化数据')
    else:
        # 轻微留白
        suggestions.append('1. 展开现有 bullet 描述，增加量化细节')
        suggestions.append('2. 增大列表间距：itemsep/topsep 0.2em → 0.3em')
        suggestions.append('3. 增大 section 间距：\\titlespacing 的 *1.5/*1.3 → *1.8/*1.5')

    # 通用排版调整建议（偏空时）
    if remaining > 20:
        suggestions.append(f'{len(suggestions)+1}. 增大页边距或间距以改善视觉效果')

    return {
        'status': 'underfill',
        'level': 'warning',
        'message': f'页面填充率 {pct:.1f}%（底部留白约 {remaining:.0f}mm），建议补充内容',
        'suggestions': suggestions
    }


# ─── 主流程 ───────────────────────────────────────────────────

def check_page_fill(output_dir: str, xelatex_path: str = None) -> dict:
    """
    完整的页面填充率检查流程：
    1. 注入测量代码
    2. 编译
    3. 解析结果
    4. 清理测量代码
    5. 重新编译（还原干净 PDF）

    参数:
        output_dir: 包含 resume-zh_CN.tex 的目录
        xelatex_path: xelatex 可执行文件路径（可选）

    返回: {ratio, status, message, suggestions, ...}
    """
    output_path = Path(output_dir)
    tex_file = output_path / 'resume-zh_CN.tex'
    aux_file = output_path / 'resume-zh_CN.aux'

    if not tex_file.exists():
        raise FileNotFoundError(f"找不到 .tex 文件: {tex_file}")

    # 自动检测 xelatex
    if not xelatex_path:
        candidates = [
            Path.home() / 'Library' / 'TinyTeX' / 'bin' / 'universal-darwin' / 'xelatex',
            Path.home() / '.TinyTeX' / 'bin' / 'x86_64-linux' / 'xelatex',
            Path('/usr/local/bin/xelatex'),
            Path('/usr/bin/xelatex'),
        ]
        for c in candidates:
            if c.exists():
                xelatex_path = str(c)
                break
        if not xelatex_path:
            xelatex_path = 'xelatex'  # fallback to PATH

    # Step 1: 注入测量代码
    injected = inject_measurement(tex_file)

    try:
        # Step 2: 编译（带测量代码）
        result = subprocess.run(
            [xelatex_path, '-interaction=nonstopmode', tex_file.name],
            cwd=str(output_path),
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0 and not aux_file.exists():
            return {
                'status': 'compile_error',
                'level': 'error',
                'message': f'xelatex 编译失败',
                'log_tail': result.stdout[-500:] if result.stdout else '',
                'suggestions': ['检查 .log 文件中的错误信息']
            }

        # Step 3: 解析填充率
        fill_data = parse_fill_ratio(aux_file)
        advice = generate_advice(fill_data['ratio'], fill_data['remaining_mm'])

        # 合并结果
        result_data = {**fill_data, **advice}

    finally:
        # Step 4: 清理测量代码
        if injected:
            remove_measurement(tex_file)

        # Step 5: 重新编译还原干净 PDF
        subprocess.run(
            [xelatex_path, '-interaction=nonstopmode', tex_file.name],
            cwd=str(output_path),
            capture_output=True,
            text=True,
            timeout=120
        )

    return result_data


def main():
    if len(sys.argv) < 2:
        print("用法: python3 tools/page_fill_check.py <output_dir> [xelatex_path]")
        print("示例: python3 tools/page_fill_check.py output/百度_数据分析_20260228")
        sys.exit(1)

    output_dir = sys.argv[1]
    xelatex_path = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        result = check_page_fill(output_dir, xelatex_path)
    except Exception as e:
        print(f"❌ 检查失败: {e}")
        sys.exit(1)

    # 输出结果
    icon = {'success': '✅', 'warning': '⚠️', 'error': '❌'}.get(result['level'], 'ℹ️')
    print(f"\n{icon} {result['message']}")
    page_count = result.get('page_count', 1)
    if page_count > 1:
        print(f"   ⚠️  检测到 {page_count} 页，内容溢出！")
    print(f"   内容高度: {result.get('total_mm', 0):.1f}mm / 可用高度: {result.get('goal_mm', 0):.1f}mm")
    print(f"   填充率: {result.get('ratio', 0) * 100:.1f}%")

    if result.get('suggestions'):
        print(f"\n📋 调优建议:")
        for s in result['suggestions']:
            print(f"   {s}")

    print()


if __name__ == '__main__':
    main()
