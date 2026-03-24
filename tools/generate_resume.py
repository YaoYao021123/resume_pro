#!/usr/bin/env python3
"""简历生成引擎

从用户数据 (profile.md + experiences/) 和 JD 文本，
自动匹配经历、生成 LaTeX、编译 PDF。

默认使用本地规则引擎；配置环境变量后可选接入外部模型。
"""

import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

# Hard global socket timeout — prevents urllib from hanging indefinitely on SSL reads
socket.setdefaulttimeout(100)


def _make_ssl_context() -> ssl.SSLContext:
    """Create an SSL context using certifi bundle when available, else system defaults."""
    try:
        import certifi  # type: ignore
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    return ctx

# ─── 路径 ─────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LATEX_TEMPLATE_DIR = PROJECT_ROOT / 'latex_src' / 'resume'
OUTPUT_DIR = PROJECT_ROOT / 'output'
DATA_DIR = PROJECT_ROOT / 'data'

# 多人档案管理支持
from tools.person_manager import (
    get_active_person_id,
    get_person_profile_path,
    get_person_experiences_dir,
    get_person_work_materials_dir,
    get_person_output_dir,
    is_multi_person_mode,
)
from tools.migrate_to_multi_person import maybe_migrate as _maybe_migrate
from tools.model_config import get_model_config, load_local_env
from tools.language_utils import normalize_language, resolve_resume_filenames
from tools import gen_log

load_local_env()

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


def _to_year_month(date_str: str) -> str:
    """将日期标准化为 YYYY/MM（输入可为 YYYY/MM、YYYY-MM、YYYY/MM/DD 等）"""
    s = (date_str or '').strip()
    if not s:
        return ''
    if s in ('至今', 'Present', 'present', 'CURRENT', 'current'):
        return '至今'
    m = re.search(r'(\d{4})[\/\-.年](\d{1,2})', s)
    if not m:
        return s
    month = str(max(1, min(12, int(m.group(2))))).zfill(2)
    return f'{m.group(1)}/{month}'


def _to_year_month_range(raw: str) -> str:
    """将范围日期标准化为 YYYY/MM -- YYYY/MM"""
    s = (raw or '').strip()
    if not s:
        return ''
    m = re.match(r'^(.*?)(?:\s*(?:--|—|–)\s*|\s+-\s+)(.+)$', s)
    if m:
        start = _to_year_month(m.group(1))
        end = _to_year_month(m.group(2))
        if not start:
            return end
        if not end:
            return start
        return f'{start} -- {end}'
    return _to_year_month(s)


def _localize_date_text(text: str, language: str = 'zh') -> str:
    """Localize normalized date text for output language."""
    if normalize_language(language) == 'en':
        return (text or '').replace('至今', 'Present')
    return text or ''


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

TEXT_WORK_MATERIAL_SUFFIXES = {'.md', '.txt', '.json', '.csv', '.yaml', '.yml'}

STRICT_AI_RULES = """
你是严格受约束的中文简历改写器，必须同时遵守以下规则：
0. 【全局意图优先】在选择任何经历之前，先整体阅读 JD，用 jd_understanding.candidate_portrait
   描述「这个岗位在找什么样的人」（能力特质 + 背景偏好 + 产出期望，2-3 句话）；
   用 jd_understanding.core_demands 列出 3-5 条核心诉求（完整句子，而非单词清单）；
   再基于这个整体画像判断哪些经历最能证明候选人匹配，而不是逐词匹配 JD 关键词
1. 只使用输入中明确提供的事实，不补充不存在的技能、成果、数字、城市、背景或外部数据
2. 仅从给定候选中选择经历、项目、奖项；不得发明新条目
3. 经历分类是固定的：intern 只能进“实习经历”，research 只能进“研究经历/项目经历”，禁止混放
4. 实习经历 2-4 段，研究/项目 0-2 段，总计不超过 5 段；严格时间倒序排列（最新在前）
5. 每段实习/工作经历写 2-3 条 bullet，最多 4 条；项目/研究经历 1-2 条
6. 每条 bullet 必须严格遵守格式「短标题：具体成果」，且“信息密度不降级”，要求如下：
   - 短标题：4-12 个字，优先“领域/对象 + 动作”结构（如「AI产品方案设计与落地」「全球AI产品研究」「数据驱动需求分析」）
   - 冒号：必须使用中文冒号「：」，且必须紧跟在短标题后面（第一个字之后最多14个字内）
   - 禁止过泛标题：如「方案设计」「项目落地」「需求梳理」「信息整理」这类空泛词，除非带具体限定词（行业/对象/方法）
   - 具体成果：优先写成“动作 + 关键对象/方法 + 结果/影响”，并保留可证实的量化信息（时间、数量、比例、覆盖范围）
   - 若原始材料出现具体技术/方法/专有名词（如 WebSocket SDK、Function Call、Prompt 工程、Top50 客户数据），改写时优先保留，不得抽象丢失为泛化表述
   - 禁止职责腔开头：避免以「负责/参与/协助」作为正文开头，改为可交付动作与结果
   - 正确示例：「AI产品策略规划：参与年度MaaS战略规划，拆解收入目标差距并提出增长路径，分析H1 Top50客户数据支撑版本迭代」
   - 正确示例：「效率工具开发：设计自动化 skill 生成日报/周报/会议纪要，将周报准备时间从30min缩短至5min，准时率达100%」
   - 禁止格式：「跟踪国家医保局、地方财政...」（直接以动词开头，无标题无冒号）
   - 禁止格式：「分析锦欣生殖、京东健康...」（无标题，冒号在句子中间而非开头）
   - 禁止格式：「负责数据分析工作」（无标题无冒号）、「data analysis: ...」（英文标题）
7. bullet 结尾绝对不能有句号、感叹号、分号或中文句号（。 . ! ！ ; ；）
8. 禁止外部过时数据，如营收、净利润、CAGR、市场规模等第三方数字
9. 禁止行业黑话、项目内部术语、难解释的研究方法堆砌；改写为通用可迁移能力
10. 城市必须沿用输入中的城市，不得猜测
11. 奖项最多 3 条；同类奖学金仅保留最高等级 1 条；低含金量和无关奖项不要
12. 如存在【用户修改反馈】，必须严格遵守其所有指令，优先级高于其他所有规则
13. 输出必须是 JSON，且字段内容尽量简洁、可直接用于生成中文简历
""".strip()

GEMINI_PLAN_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'properties': {
        'company': {'type': 'string', 'description': '目标公司名，未知时返回空字符串'},
        'role': {'type': 'string', 'description': '目标岗位名，未知时返回空字符串'},
        'jd_understanding': {
            'type': 'object',
            'description': 'JD整体意图理解，先于经历选择输出',
            'properties': {
                'candidate_portrait': {
                    'type': 'string',
                    'description': '2-3句话描述这个岗位在找什么样的人（能力特质+背景偏好+产出期望），而非关键词列表',
                },
                'core_demands': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'minItems': 3,
                    'maxItems': 5,
                    'description': '3-5条核心诉求，每条用完整句子描述（如「有实际的数据分析项目经验，能独立输出可视化报告」）',
                },
            },
            'required': ['candidate_portrait', 'core_demands'],
        },
        'selected_experiences': {
            'type': 'array',
            'maxItems': 5,
            'items': {
                'type': 'object',
                'properties': {
                    'filename': {'type': 'string', 'description': '必须等于候选经历中的 filename'},
                    'relevance_reason': {'type': 'string', 'description': '解释这段经历如何证明候选人符合candidate_portrait的描述'},
                    'rewritten_bullets': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'minItems': 2,
                        'maxItems': 4,
                    },
                },
                'required': ['filename', 'relevance_reason', 'rewritten_bullets'],
            },
        },
        'selected_projects': {
            'type': 'array',
            'maxItems': 2,
            'items': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': '必须等于候选项目中的 name'},
                    'relevance_reason': {'type': 'string', 'description': '解释这段项目如何证明候选人符合candidate_portrait的描述'},
                    'rewritten_bullets': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'minItems': 1,
                        'maxItems': 3,
                    },
                },
                'required': ['name', 'relevance_reason', 'rewritten_bullets'],
            },
        },
        'selected_awards': {
            'type': 'array',
            'maxItems': 3,
            'items': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': '必须等于候选奖项中的 name'},
                    'reason': {'type': 'string', 'description': '选择原因'},
                },
                'required': ['name', 'reason'],
            },
        },
    },
    'required': [
        'company',
        'role',
        'jd_understanding',
        'selected_experiences',
        'selected_projects',
        'selected_awards',
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


def _should_force_ai(ai_config: dict) -> bool:
    return bool(ai_config.get('enabled'))


def _has_available_ai(ai_config: dict) -> bool:
    return bool(ai_config.get('api_key') and ai_config.get('model'))


def _should_try_ai(ai_config: dict, prefer_ai: bool = False) -> bool:
    return bool(_has_available_ai(ai_config) and (prefer_ai or ai_config.get('enabled')))


def _api_key_fingerprint(api_key: str | None) -> str | None:
    if not api_key:
        return None
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:12]


def _mask_api_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    if len(api_key) <= 8:
        return '*' * len(api_key)
    return f'{api_key[:4]}{"*" * (len(api_key) - 8)}{api_key[-4:]}'


def _redact_text_with_ai_config(text: str, ai_config: dict) -> str:
    raw = str(text or '')
    api_key = str(ai_config.get('api_key') or '')
    if not api_key:
        return raw
    fingerprint = str(ai_config.get('byok_fingerprint') or _api_key_fingerprint(api_key) or '')
    masked = str(ai_config.get('byok_masked_key') or _mask_api_key(api_key) or '')
    replacement = f'[REDACTED_API_KEY:{masked}|fp:{fingerprint}]'
    return raw.replace(api_key, replacement)


def _write_generation_context(output_dir: Path, *, jd_text: str, interview_text: str,
                              company: str, role: str, engine: str,
                              ai_provider: str | None, ai_model: str | None,
                              fill_ratio: float, language: str = 'zh') -> None:
    payload = {
        'company': company,
        'role': role,
        'jd_text': jd_text.strip(),
        'interview_text': interview_text.strip(),
        'engine': engine,
        'ai_provider': ai_provider,
        'ai_model': ai_model,
        'fill_ratio': fill_ratio,
        'language': normalize_language(language),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
    }
    (output_dir / 'generation_context.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def _normalize_quotes(text: str) -> str:
    text = (text or '').replace('„', '“').replace('‟', '”').replace('＂', '"')
    result = []
    open_quote = True
    for ch in text:
        if ch == '"':
            result.append('“' if open_quote else '”')
            open_quote = not open_quote
        else:
            result.append(ch)
    return ''.join(result)


def _sanitize_bullet(text: str) -> str:
    bullet = re.sub(r'\s+', ' ', (text or '').strip())
    bullet = re.sub(r'^[\-\*\u2022\d\.\)\(、\s]+', '', bullet)
    bullet = _normalize_quotes(bullet)
    bullet = bullet.rstrip('。.!！;；')
    return bullet.strip()


# Action verbs that commonly start resume bullets — used for auto-title derivation
_RESUME_VERBS = {
    '负责', '完成', '参与', '主导', '搭建', '撰写', '分析', '跟踪', '整理', '开发',
    '设计', '调研', '优化', '建立', '维护', '支撑', '输出', '推进', '协助', '独立',
    '持续', '围绕', '通过', '基于', '拆解', '梳理', '建设', '完善', '推动', '制定',
    '统计', '收集', '汇总', '追踪', '测试', '部署', '运营', '管理', '规划', '研究',
}

_GENERIC_BULLET_TITLES = {
    '方案设计', '项目落地', '需求梳理', '信息整理', '数据复盘', '效率优化',
    '工具搭建', '竞品分析', '报告撰写', '数据维护', '流程优化', '业务支持',
}
_LOW_VALUE_OPENERS = ('负责', '参与', '协助', '支持', '配合')
_RESULT_SIGNAL_TOKENS = (
    '提升', '缩短', '降低', '增长', '优化', '支撑', '推动', '采纳', '覆盖',
    '交付', '落地', '上线', '准时率', '转化', '效率', '准确率',
)
_TECH_SIGNAL_TOKENS = (
    'SDK', 'API', 'Agent', 'Prompt', 'Function Call', 'Python', 'SQL',
    'WebSocket', 'n8n', 'Coze', '模型', '自动化', '数据库', '可视化',
)


def _auto_add_title(bullet: str) -> str:
    """If a bullet lacks the 「短标题：content」 pattern at the start, derive one.

    Strategy (in order of priority):
    1. Already has title (2-14 non-colon chars then Chinese colon within first 16 chars) → as-is
    2. First segment before first 、or ， is 2-12 chars → use as title
    3. Starts with a known 2-char action verb → title = verb + next 2 CJK chars (4 total)
    4. Use first 4 CJK chars as title (last resort)
    """
    clean = _sanitize_bullet(bullet)
    if not clean:
        return clean
    # Already has correct title format (colon within first 16 chars)
    if re.match(r'^[^\uff1a\n]{2,14}\uff1a', clean):
        return clean

    # Split at first natural break (、or ，) - use first segment as title if it's short enough
    first_seg_match = re.match(r'^([^，、；,\uff1b]{2,12})[，、](.+)', clean)
    if first_seg_match:
        title_cand = first_seg_match.group(1)
        rest = first_seg_match.group(2).strip()
        if len(rest) > 8:
            return f'{title_cand}：{rest}'

    # Starts with a known 2-char verb → verb + next EXACTLY 2 CJK chars = 4-char title
    # e.g. "独立搭建六维研究框架，..." → verb="独立", noun="搭建" → "独立搭建：六维研究框架..."
    verb_match = re.match(r'^([\u4e00-\u9fff]{2})([\u4e00-\u9fff]{2})(.{10,})', clean)
    if verb_match:
        verb = verb_match.group(1)
        noun = verb_match.group(2)
        rest_text = verb_match.group(3)
        if verb in _RESUME_VERBS:
            title_cand = verb + noun  # exactly 4 chars
            return f'{title_cand}：{rest_text}'

    # Last resort: first 4 CJK chars
    cjk_match = re.match(r'^([\u4e00-\u9fff]{4})([\u4e00-\u9fff\w].{8,})', clean)
    if cjk_match:
        return f'{cjk_match.group(1)}：{cjk_match.group(2)}'

    return clean


def _bullet_quality_score(bullet: str) -> int:
    clean = _auto_add_title(_sanitize_bullet(bullet))
    if not clean:
        return -99
    score = 0
    match = re.match(r'^([^\uff1a\n]{2,14})\uff1a(.+)$', clean)
    title = ''
    body = clean
    if match:
        title = match.group(1).strip()
        body = match.group(2).strip()
        score += 2
        if 4 <= len(title) <= 12:
            score += 2
        elif len(title) <= 3:
            score -= 1
        if title in _GENERIC_BULLET_TITLES:
            score -= 6
    else:
        score -= 1

    if re.search(r'\d', body):
        score += 2
    if any(token in body for token in _TECH_SIGNAL_TOKENS):
        score += 2
    if any(token in body for token in _RESULT_SIGNAL_TOKENS):
        score += 2
    if re.match(r'^(负责|参与|协助|支持|配合)', body):
        score -= 2
    if len(body) >= 20:
        score += 1
    if any(body.startswith(prefix) for prefix in _LOW_VALUE_OPENERS):
        score -= 1
    return score


def _select_best_bullets(ai_bullets: list[str], fallback_bullets: list[str], *,
                         min_count: int, max_count: int) -> list[str]:
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []

    def append_candidates(items: list[str], source: str, bonus: int = 0) -> None:
        for raw in items:
            clean = _sanitize_bullet(raw)
            if not clean or clean in seen:
                continue
            seen.add(clean)
            candidates.append({
                'text': clean,
                'source': source,
                'score': _bullet_quality_score(clean) + bonus,
            })

    append_candidates(ai_bullets, 'ai', bonus=1)
    append_candidates(fallback_bullets, 'fallback')

    if not candidates:
        return []

    candidates.sort(
        key=lambda item: (item['score'], item['source'] == 'ai', len(item['text'])),
        reverse=True,
    )

    selected: list[str] = []
    for item in candidates:
        if item['score'] < 4 and len(selected) >= min_count:
            continue
        selected.append(item['text'])
        if len(selected) >= max_count:
            break

    if len(selected) < min_count:
        for item in candidates:
            if item['text'] in selected:
                continue
            selected.append(item['text'])
            if len(selected) >= min_count or len(selected) >= max_count:
                break

    return selected[:max_count]


def _render_bullet_latex(bullet: str) -> str:
    """Render a single bullet to LaTeX \\item.

    Auto-adds 短标题 if missing, then bolds the title prefix.
    """
    clean = _auto_add_title(_sanitize_bullet(bullet))
    escaped = tex_escape(clean)
    # Match "短标题：rest" — title up to Chinese colon, within first 14 chars
    m = re.match(r'^([^\uff1a\n]{2,14})\uff1a(.+)$', escaped)  # \uff1a = ：
    if m:
        title = m.group(1).strip()
        content = m.group(2).strip()
        return rf'    \item \textbf{{{title}：}} {content}'
    return rf'    \item {escaped}'


def _split_notes_to_bullets(text: str, max_count: int) -> list[str]:
    parts = re.split(r'[。；;\n]+', (text or '').strip())
    bullets = []
    for part in parts:
        bullet = _sanitize_bullet(part)
        if bullet:
            bullets.append(bullet)
        if len(bullets) >= max_count:
            break
    return bullets


def _fallback_experience_bullets(exp: dict, max_count: int = 2) -> list[str]:
    candidates = []
    for wi in exp.get('work_items', []):
        title = _sanitize_bullet(wi.get('title', ''))
        desc = _sanitize_bullet(wi.get('desc', ''))
        if title and desc and title not in desc:
            candidates.append(f'{title}：{desc}')
        if desc:
            candidates.append(desc)
    if not candidates:
        notes = exp.get('notes', '')
        if notes:
            candidates = _split_notes_to_bullets(notes, max_count * 2)

    ranked = sorted(candidates, key=lambda item: (_bullet_quality_score(item), len(item)), reverse=True)
    bullets = []
    seen = set()
    for item in ranked:
        if item in seen:
            continue
        seen.add(item)
        bullets.append(item)
        if len(bullets) >= max_count:
            break
    return bullets[:max_count]


def _fallback_project_bullets(project: dict, max_count: int = 2) -> list[str]:
    candidates = _split_notes_to_bullets(project.get('desc', ''), max_count * 2)
    ranked = sorted(candidates, key=lambda item: (_bullet_quality_score(item), len(item)), reverse=True)
    bullets = []
    seen = set()
    for item in ranked:
        if item in seen:
            continue
        seen.add(item)
        bullets.append(item)
        if len(bullets) >= max_count:
            break
    return bullets[:max_count] if bullets else []


def _classify_experience(exp: dict) -> str:
    filename = exp.get('filename', '')
    tags = exp.get('tags', '')
    if '研究_' in filename or ('研究' in tags and '学术' in tags):
        return 'research'
    return 'intern'


def _time_sort_key(raw_date: str) -> tuple[int, int]:
    match = re.search(r'(\d{4})[\/\-.年](\d{1,2})', raw_date or '')
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def _profile_setup_error(person_id: str | None = None) -> str | None:
    profile_path = get_person_profile_path(person_id)
    if not profile_path.exists():
        return '⚠️ 请先完成个人信息设置：\n1. 打开 data/profile.md\n2. 将所有 [YOUR_XXX] 占位符替换为你的真实信息\n3. 完成后重新发送 JD\n\n详细说明请参考 SETUP.md'
    raw_text = profile_path.read_text(encoding='utf-8')
    code_blocks = re.findall(r'```\n(.*?)```', raw_text, re.DOTALL)
    if any('[YOUR_' in block for block in code_blocks):
        return '⚠️ 请先完成个人信息设置：\n1. 打开 data/profile.md\n2. 将所有 [YOUR_XXX] 占位符替换为你的真实信息\n3. 完成后重新发送 JD\n\n详细说明请参考 SETUP.md'
    profile = _parse_profile(person_id)
    has_education = any(edu.get('school') for edu in profile.get('education', []))
    has_skills = any(profile.get(key, '').strip() for key in ('skills_tech', 'skills_software', 'skills_lang'))
    required_fields = (profile.get('name_zh', ''), profile.get('email', ''), profile.get('phone', ''))
    if not all(field.strip() for field in required_fields) or not has_education or not has_skills:
        return '⚠️ 请先完成个人信息设置：\n1. 打开 data/profile.md\n2. 将所有 [YOUR_XXX] 占位符替换为你的真实信息\n3. 完成后重新发送 JD\n\n详细说明请参考 SETUP.md'
    return None


def _experiences_setup_error(person_id: str | None = None) -> str | None:
    exp_dir = get_person_experiences_dir(person_id)
    valid_files = []
    if exp_dir.exists():
        for file_path in exp_dir.iterdir():
            if file_path.suffix == '.md' and file_path.name not in ('_template.md', 'README.md'):
                if file_path.read_text(encoding='utf-8').strip():
                    valid_files.append(file_path)
    if valid_files:
        return None
    return '⚠️ 请先添加至少一段经历：\n1. 复制 data/experiences/_template.md\n2. 重命名为 01_公司名.md 并填写\n3. 完成后重新发送 JD\n\n详细说明请参考 SETUP.md'


def _keywords_set(jd_keywords: dict) -> set[str]:
    keywords = set()
    for category in ('tech', 'domain', 'skill'):
        for keyword in jd_keywords.get(category, []):
            if keyword:
                keywords.add(keyword.lower())
    return keywords


def _award_group_key(name: str) -> str:
    if '奖学金' not in name:
        return name
    return re.sub(r'(特等|一等|二等|三等|一等奖|二等奖|三等奖)', '', name)


def _award_score(award: dict, jd_keywords: dict, preferred_order: dict[str, int]) -> int:
    name = award.get('name', '')
    org = award.get('org', '')
    score = 0
    if name in preferred_order:
        score += 100 - preferred_order[name]
    lowered_name = name.lower()
    for keyword in _keywords_set(jd_keywords):
        if keyword in lowered_name:
            score += 20
    high_value_tokens = ('国际', '全国', '国家', '教育部', 'COMAP', 'Meritorious', '一等奖', '特等奖')
    medium_value_tokens = ('二等奖', '数学', '建模', '创新创业', '英语')
    low_value_tokens = ('三等奖', '公益', '爱心', '优秀学生')
    score += sum(12 for token in high_value_tokens if token in name or token in org)
    score += sum(5 for token in medium_value_tokens if token in name or token in org)
    score -= sum(10 for token in low_value_tokens if token in name or token in org)
    if '奖学金' in name:
        score += 6
    return score


def _filter_awards(profile: dict, jd_keywords: dict, preferred_names: list[str] | None = None, max_count: int = 3) -> list[dict]:
    preferred_order = {name: index for index, name in enumerate(preferred_names or [])}
    grouped: dict[str, dict] = {}
    for award in profile.get('awards', []):
        name = award.get('name', '').strip()
        if not name:
            continue
        group_key = _award_group_key(name)
        score = _award_score(award, jd_keywords, preferred_order)
        current = grouped.get(group_key)
        if current is None or score > current['_score']:
            grouped[group_key] = {**award, '_score': score}
    ranked = sorted(grouped.values(), key=lambda item: item['_score'], reverse=True)
    return [{k: v for k, v in award.items() if not k.startswith('_')} for award in ranked[:max_count]]


def _score_project(project: dict, jd_keywords: dict) -> int:
    full_text = ' '.join(
        [
            project.get('name', ''),
            project.get('role', ''),
            project.get('desc', ''),
            project.get('tags', ''),
        ]
    ).lower()
    return sum(3 for keyword in _keywords_set(jd_keywords) if keyword in full_text)


def _filter_projects(profile: dict, jd_keywords: dict,
                     preferred_entries: list[dict] | None = None,
                     remaining_slots: int = 2) -> list[dict]:
    if remaining_slots <= 0:
        return []
    project_map = {project.get('name', ''): project for project in profile.get('projects', []) if project.get('name')}
    selected = []
    used_names = set()
    for entry in preferred_entries or []:
        name = entry.get('name', '')
        if name in used_names or name not in project_map:
            continue
        project = dict(project_map[name])
        ai_bullets = [_sanitize_bullet(item) for item in entry.get('rewritten_bullets', [])]
        ai_bullets = [item for item in ai_bullets if item][:3]
        project['selected_bullets'] = _select_best_bullets(
            ai_bullets,
            _fallback_project_bullets(project, 3),
            min_count=1,
            max_count=2,
        )
        if not project['selected_bullets']:
            continue
        project['_reason'] = entry.get('relevance_reason', '')
        selected.append(project)
        used_names.add(name)
        if len(selected) >= min(2, remaining_slots):
            return selected
    ranked = sorted(
        (project for project in project_map.values() if project.get('name') not in used_names),
        key=lambda item: (_score_project(item, jd_keywords), _time_sort_key(item.get('time', ''))),
        reverse=True,
    )
    for project in ranked:
        if _score_project(project, jd_keywords) <= 0:
            continue
        project = dict(project)
        project['selected_bullets'] = _fallback_project_bullets(project, 2)
        if not project['selected_bullets']:
            continue
        selected.append(project)
        if len(selected) >= min(2, remaining_slots):
            break
    return selected


def _apply_experience_selection_rules(experiences: list, jd_keywords: dict,
                                      preferred_entries: list[dict] | None = None,
                                      max_total: int = 5) -> list[dict]:
    preferred_map = {entry.get('filename', ''): entry for entry in preferred_entries or []}
    heuristic_ranked = match_experiences(experiences, jd_keywords, max_count=len(experiences))
    experience_map = {exp.get('filename', ''): exp for exp in heuristic_ranked}
    ordered_candidates = []
    for entry in preferred_entries or []:
        exp = experience_map.get(entry.get('filename', ''))
        if exp is not None:
            ordered_candidates.append(exp)
    for exp in heuristic_ranked:
        if exp not in ordered_candidates:
            ordered_candidates.append(exp)

    selected_intern = []
    selected_research = []
    selected_names = set()

    def try_add(exp: dict) -> bool:
        filename = exp.get('filename', '')
        if filename in selected_names:
            return False
        classification = _classify_experience(exp)
        if classification == 'intern' and len(selected_intern) >= 4:
            return False
        if classification == 'research' and len(selected_research) >= 2:
            return False
        if len(selected_intern) + len(selected_research) >= max_total:
            return False
        selected = dict(exp)
        preferred = preferred_map.get(filename, {})
        ai_bullets = [_sanitize_bullet(item) for item in preferred.get('rewritten_bullets', [])]
        ai_bullets = [item for item in ai_bullets if item][:5]
        min_count = 2 if classification == 'intern' else 1
        selected['selected_bullets'] = _select_best_bullets(
            ai_bullets,
            _fallback_experience_bullets(selected, 5),
            min_count=min_count,
            max_count=4,
        )
        if not selected['selected_bullets']:
            return False
        selected['_reason'] = preferred.get('relevance_reason', '')
        if classification == 'intern':
            selected_intern.append(selected)
        else:
            selected_research.append(selected)
        selected_names.add(filename)
        return True

    for exp in ordered_candidates:
        try_add(exp)

    if len(selected_intern) < 2:
        for exp in heuristic_ranked:
            if _classify_experience(exp) != 'intern':
                continue
            try_add(exp)
            if len(selected_intern) >= 2:
                break

    if len(selected_intern) + len(selected_research) < 3:
        for exp in heuristic_ranked:
            try_add(exp)
            if len(selected_intern) + len(selected_research) >= 3:
                break

    selected_intern.sort(key=lambda item: _time_sort_key(item.get('time_start', '')), reverse=True)
    selected_research.sort(key=lambda item: _time_sort_key(item.get('time_start', '')), reverse=True)
    return selected_research + selected_intern


def _merge_ai_keywords(jd_keywords: dict, ai_keywords: dict) -> dict:
    merged = {key: list(value) if isinstance(value, list) else value for key, value in jd_keywords.items()}
    mapping = {
        'hard_skills': 'tech',
        'functions': 'domain',
        'domains': 'domain',
        'soft_skills': 'skill',
    }
    for source_key, target_key in mapping.items():
        bucket = merged.setdefault(target_key, [])
        for keyword in ai_keywords.get(source_key, []):
            if keyword and keyword not in bucket:
                bucket.append(keyword)
    return merged


def _normalize_work_material_name(value: str) -> str:
    value = re.sub(r'（.*?）|\(.*?\)', '', value or '').strip().lower()
    return re.sub(r'[\s_\-]+', '', value)


def _match_work_material_dirs(person_id: str | None, exp: dict) -> list[Path]:
    base_dir = get_person_work_materials_dir(person_id)
    if not base_dir.exists():
        return []

    filename_stem = re.sub(r'^\d+_', '', Path(exp.get('filename', '')).stem)
    candidates = {
        _normalize_work_material_name(exp.get('company', '')),
        _normalize_work_material_name(filename_stem),
    }
    candidates = {token for token in candidates if token}

    matched: list[Path] = []
    for child in sorted(base_dir.rglob('*')):
        if not child.is_dir():
            continue
        relative_parts = child.relative_to(base_dir).parts
        if any(part.startswith('.') for part in relative_parts):
            continue
        child_name = _normalize_work_material_name(child.name)
        if any(token in child_name or child_name in token for token in candidates):
            matched.append(child)

    selected: list[Path] = []
    for child in sorted(matched, key=lambda p: (len(p.relative_to(base_dir).parts), str(p))):
        if any(existing == child or existing in child.parents for existing in selected):
            continue
        selected.append(child)
    return selected


def _load_text_work_materials(person_id: str | None, exp: dict) -> list[dict]:
    materials = []
    for folder in _match_work_material_dirs(person_id, exp):
        for file_path in sorted(folder.rglob('*')):
            if any(part.startswith('.') for part in file_path.relative_to(folder).parts):
                continue
            if not file_path.is_file() or file_path.suffix.lower() not in TEXT_WORK_MATERIAL_SUFFIXES:
                continue
            content = file_path.read_text(encoding='utf-8', errors='ignore').strip()
            if not content:
                continue
            materials.append({
                'name': file_path.name,
                'content': content[:4000],
            })
    return materials


def _truncate(text: str, max_chars: int, label: str = '') -> str:
    """Truncate text with an ellipsis marker to stay within token budgets."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    suffix = f'\n…[{label}截断，共 {len(text)} 字]' if label else f'\n…[截断，共 {len(text)} 字]'
    return text[:half] + '\n…\n' + text[-(max_chars - half - len(suffix)):] + suffix


def _build_ai_prompt(profile: dict, experiences: list, projects: list,
                     awards: list, jd_text: str, interview_text: str,
                     person_id: str | None,
                     max_jd_chars: int = 3000,
                     max_interview_chars: int = 2000,
                     max_work_material_chars: int = 1500) -> str:
    experience_payload = []
    for exp in experiences:
        raw_materials = _load_text_work_materials(person_id, exp)
        # Enforce a TOTAL budget per experience across all files
        if raw_materials:
            budget = max_work_material_chars
            limited: list[dict] = []
            for m in raw_materials:
                if budget <= 0:
                    break
                content = m.get('content', '')
                if len(content) > budget:
                    content = content[:budget] + f'…[截断]'
                limited.append({'name': m['name'], 'content': content})
                budget -= len(content)
            raw_materials = limited
        experience_payload.append({
            'filename': exp.get('filename', ''),
            'classification': _classify_experience(exp),
            'company': exp.get('company', ''),
            'city': exp.get('city', ''),
            'department': exp.get('department', ''),
            'role': exp.get('role', ''),
            'time_start': exp.get('time_start', ''),
            'time_end': exp.get('time_end', ''),
            'tags': exp.get('tags', ''),
            'notes': exp.get('notes', ''),
            'work_items': exp.get('work_items', []),
            'work_materials': raw_materials,
        })

    project_payload = [{
        'name': project.get('name', ''),
        'role': project.get('role', ''),
        'time': project.get('time', ''),
        'desc': project.get('desc', ''),
        'tags': project.get('tags', ''),
    } for project in projects if project.get('name')]

    award_payload = [{
        'name': award.get('name', ''),
        'org': award.get('org', ''),
        'time': award.get('time', ''),
    } for award in awards if award.get('name')]

    profile_payload = {
        'name_zh': profile.get('name_zh', ''),
        'name_en': profile.get('name_en', ''),
        'education': profile.get('education', []),
        'skills_tech': profile.get('skills_tech', ''),
        'skills_software': profile.get('skills_software', ''),
        'skills_lang': profile.get('skills_lang', ''),
    }

    jd_clipped = _truncate(jd_text, max_jd_chars, 'JD')
    interview_clipped = _truncate(interview_text, max_interview_chars, '面经') if interview_text.strip() else '无'

    output_example = ('{"company":"目标公司","role":"目标岗位","jd_understanding":{"candidate_portrait":"这个岗位在找能将复杂业务需求转化为可落地AI方案的人，核心诉求是具备产品策略判断、跨团队推动能力和可量化产出","core_demands":["具备AI产品或数据分析经验，能将分散信息整合为有逻辑的方案并支撑决策","能够跨部门协同推进需求从PoC到交付闭环，形成可复用的方法与流程","在项目中有可验证的效率提升或业务价值产出，而非仅描述参与过程"]},"selected_experiences":[{"filename":"必须等于候选经历的filename字段","relevance_reason":"说明这段经历如何证明候选人符合candidate_portrait的描述","rewritten_bullets":["AI产品方案设计与落地：基于WebSocket SDK实现健康管家Agent能力并集成Function Call工具调用，推动需求分析到客户交付全流程落地","全球AI产品研究与策略支持：研究15+家全球AI产品并输出50页分析报告，关键结论被采纳并推动2个Q3规划项目调整","效率工具开发：搭建自动化流程生成日报/周报/会议纪要，将周报准备时间从30min缩短至5min，准时率达100%"]}],"selected_projects":[{"name":"必须等于候选项目name字段","relevance_reason":"说明项目如何体现candidate_portrait要求的能力","rewritten_bullets":["数据产品化落地：使用XXX搭建YYY模块，覆盖ZZZ用户场景并形成可复用流程"]}],"selected_awards":[{"name":"必须等于候选奖项name字段","reason":"选择原因"}]}')

    return textwrap.dedent(
        f"""
        你只能从下面的候选条目中做选择。

        【JD】
        {jd_clipped}

        【面经/补充信息】
        {interview_clipped}

        【个人信息】
        {json.dumps(profile_payload, ensure_ascii=False, indent=2)}

        【候选经历】
        {json.dumps(experience_payload, ensure_ascii=False, indent=2)}

        【候选项目】
        {json.dumps(project_payload, ensure_ascii=False, indent=2)}

        【候选奖项】
        {json.dumps(award_payload, ensure_ascii=False, indent=2)}

        【输出格式】
        选择经历时，优先判断哪段经历最能整体证明候选人符合你在 candidate_portrait 中描述的要求，而非简单寻找 JD 文字相同的关键词。

        必须严格按照以下 JSON 结构返回，不得添加其他字段，不得输出 personal_info 或完整简历：
        {output_example}
        """
    ).strip()


def _request_json(url: str, *, headers: dict[str, str], payload: dict) -> dict:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with urllib_request.urlopen(request, timeout=90, context=_make_ssl_context()) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib_error.HTTPError as exc:
        error_body = exc.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'HTTP {exc.code}: {error_body}') from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f'网络错误: {exc.reason}') from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f'请求超时或网络错误: {exc}') from exc


def _api_join(base_url: str, path: str) -> str:
    return base_url.rstrip('/') + path


def _extract_json_text(text: str) -> dict:
    if not text:
        raise RuntimeError('模型未返回可解析内容')
    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r'(\{.*\})', cleaned, re.DOTALL)
        if not match:
            raise RuntimeError(f'模型返回了非 JSON 内容: {cleaned[:500]}')
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'模型返回了不可解析的 JSON 内容: {cleaned[:500]}') from exc


def _call_gemini_resume_planner(ai_config: dict, profile: dict, experiences: list,
                                jd_text: str, interview_text: str,
                                person_id: str | None) -> dict:
    prompt = _build_ai_prompt(
        profile,
        experiences,
        profile.get('projects', []),
        profile.get('awards', []),
        jd_text,
        interview_text,
        person_id,
    )
    response_payload = _request_json(
        _api_join(ai_config['base_url'], f"/models/{ai_config['model']}:generateContent"),
        headers={
            'Content-Type': 'application/json',
            'x-goog-api-key': ai_config['api_key'],
        },
        payload={
            'contents': [{'parts': [{'text': f'{STRICT_AI_RULES}\n\n{prompt}'}]}],
            'generationConfig': {
                'responseMimeType': 'application/json',
                'responseJsonSchema': GEMINI_PLAN_SCHEMA,
                'temperature': 0.2,
            },
        },
    )
    candidates = response_payload.get('candidates', [])
    if not candidates:
        raise RuntimeError(f'Gemini 返回空结果: {response_payload}')
    parts = candidates[0].get('content', {}).get('parts', [])
    text = ''.join(part.get('text', '') for part in parts if part.get('text'))
    plan = _extract_json_text(text)
    plan['_model'] = ai_config['model']
    plan['_provider'] = ai_config['provider']
    return plan


def _is_token_limit_error(msg: str) -> bool:
    """Detect token-limit 400 errors from any provider."""
    lower = msg.lower()
    return any(k in lower for k in (
        'max message tokens', 'context length', 'maximum context',
        'token limit', 'too many tokens', 'tokens exceed',
    ))


def _build_prompt_with_budget(ai_config, profile, experiences, jd_text, interview_text, person_id,
                               *, wm_chars: int, jd_chars: int, iv_chars: int) -> str:
    return _build_ai_prompt(
        profile, experiences,
        profile.get('projects', []),
        profile.get('awards', []),
        jd_text, interview_text, person_id,
        max_jd_chars=jd_chars,
        max_interview_chars=iv_chars,
        max_work_material_chars=wm_chars,
    )


def _is_json_format_unsupported(msg: str) -> bool:
    lower = msg.lower()
    return 'json_object' in lower or (
        'response_format' in lower and ('not support' in lower or 'invalid' in lower or 'not valid' in lower)
    )


def _is_thinking_unsupported(msg: str) -> bool:
    lower = msg.lower()
    return 'thinking' in lower and ('not support' in lower or 'invalid' in lower or 'not valid' in lower or 'unknown' in lower)


def _call_openai_compatible_resume_planner(ai_config: dict, profile: dict, experiences: list,
                                           jd_text: str, interview_text: str,
                                           person_id: str | None) -> dict:
    api_url = _api_join(ai_config['base_url'], '/chat/completions')
    auth_headers = {
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {ai_config['api_key']}",
    }

    # Progressive budget tiers: try with more context first, fall back on token errors
    budget_tiers = [
        {'wm_chars': 600,  'jd_chars': 3000, 'iv_chars': 2000},  # full
        {'wm_chars': 200,  'jd_chars': 2000, 'iv_chars': 1000},  # reduced
        {'wm_chars': 0,    'jd_chars': 1500, 'iv_chars': 800},   # minimal (no work_materials)
    ]

    # Use provider metadata to skip known-unsupported features from the start
    use_json_format = ai_config.get('supports_json_object', True)
    use_thinking_off = ai_config.get('supports_thinking_off', False)
    last_error: Exception | None = None

    gen_log.emit('step', f'AI 请求: {ai_config["provider"]} / {ai_config["model"]}')

    tier_idx = 0
    while tier_idx < len(budget_tiers):
        tier = budget_tiers[tier_idx]
        prompt = _build_prompt_with_budget(
            ai_config, profile, experiences, jd_text, interview_text, person_id, **tier
        )
        payload: dict = {
            'model': ai_config['model'],
            'messages': [
                {'role': 'system', 'content': STRICT_AI_RULES},
                {'role': 'user', 'content': prompt},
            ],
            'temperature': 0.2,
            'max_tokens': 2500,
        }
        if use_thinking_off:
            payload['thinking'] = {'type': 'disabled'}  # disable CoT reasoning for speed
        if use_json_format:
            payload['response_format'] = {'type': 'json_object'}

        tier_label = f'Tier {tier_idx + 1} / wm={tier["wm_chars"]} jd={tier["jd_chars"]} iv={tier["iv_chars"]}'
        gen_log.emit('ai_req', tier_label,
                     data={'system': STRICT_AI_RULES, 'prompt': prompt,
                           'thinking_off': use_thinking_off, 'json_format': use_json_format})

        try:
            response_payload = _request_json(api_url, headers=auth_headers, payload=payload)
        except RuntimeError as exc:
            msg = _redact_text_with_ai_config(str(exc), ai_config)
            gen_log.emit('error', f'AI 调用失败: {msg}')
            if _is_token_limit_error(msg) or '超时' in msg or 'timeout' in msg.lower():
                last_error = exc
                tier_idx += 1  # shrink budget
                continue
            if _is_json_format_unsupported(msg):
                use_json_format = False  # disable for all remaining attempts
                last_error = exc
                # retry SAME tier without response_format (don't advance tier_idx)
                continue
            if _is_thinking_unsupported(msg):
                use_thinking_off = False  # model doesn't support thinking param
                last_error = exc
                continue  # retry same tier without thinking param
            raise  # unrecoverable error (auth, server error, etc.)
        else:
            break  # success
    else:
        raise RuntimeError(f'所有 token 预算方案均失败: {last_error}')

    choices = response_payload.get('choices', [])
    if not choices:
        raise RuntimeError(f'模型返回空结果: {response_payload}')
    message = choices[0].get('message', {})
    content = message.get('content', '')
    # Log raw AI response including thinking/reasoning if present
    reasoning = message.get('reasoning_content', '') or message.get('thinking', '')
    if reasoning:
        gen_log.emit('think', f'思考过程 ({len(reasoning)} 字符)', data=_redact_text_with_ai_config(reasoning, ai_config))
    safe_content = _redact_text_with_ai_config(content, ai_config)
    gen_log.emit('ai_resp', f'AI 响应原文 ({len(content)} 字符)',
                 data={'raw': safe_content, 'usage': response_payload.get('usage', {})})
    if isinstance(content, list):
        content = ''.join(part.get('text', '') for part in content if isinstance(part, dict))
    plan = _extract_json_text(content)
    gen_log.emit('parse', f'AI 计划解析成功: {len(plan.get("selected_experiences", []))} 段经历',
                 data=plan)
    plan['_model'] = ai_config['model']
    plan['_provider'] = ai_config['provider']
    return plan


def _call_anthropic_resume_planner(ai_config: dict, profile: dict, experiences: list,
                                   jd_text: str, interview_text: str,
                                   person_id: str | None) -> dict:
    prompt = _build_ai_prompt(
        profile,
        experiences,
        profile.get('projects', []),
        profile.get('awards', []),
        jd_text,
        interview_text,
        person_id,
    )
    response_payload = _request_json(
        _api_join(ai_config['base_url'], '/v1/messages'),
        headers={
            'Content-Type': 'application/json',
            'x-api-key': ai_config['api_key'],
            'anthropic-version': '2023-06-01',
        },
        payload={
            'model': ai_config['model'],
            'max_tokens': 4096,
            'temperature': 0.2,
            'system': STRICT_AI_RULES,
            'messages': [{'role': 'user', 'content': prompt}],
        },
    )
    content_blocks = response_payload.get('content', [])
    text = ''.join(block.get('text', '') for block in content_blocks if block.get('type') == 'text')
    plan = _extract_json_text(text)
    plan['_model'] = ai_config['model']
    plan['_provider'] = ai_config['provider']
    return plan


def _call_ai_resume_planner(ai_config: dict, profile: dict, experiences: list,
                            jd_text: str, interview_text: str,
                            person_id: str | None) -> dict:
    provider = ai_config.get('provider', '')
    api_style = ai_config.get('api_style', 'openai')
    if provider == 'anthropic':
        return _call_anthropic_resume_planner(ai_config, profile, experiences, jd_text, interview_text, person_id)
    if api_style == 'gemini':
        # Legacy native Gemini API path (only if explicitly using native style)
        return _call_gemini_resume_planner(ai_config, profile, experiences, jd_text, interview_text, person_id)
    # Default: OpenAI-compatible (covers Gemini OpenAI endpoint, doubao, qwen, etc.)
    return _call_openai_compatible_resume_planner(ai_config, profile, experiences, jd_text, interview_text, person_id)


# ─── 经历匹配 ─────────────────────────────────────────────────

def _parse_experience_file(filepath: Path) -> dict:
    """解析经历 .md 文件（简化版，与 server.py 的 parse_experience_file 兼容）"""
    text = filepath.read_text(encoding='utf-8')
    lines = text.split('\n')

    result = {
        'company': '', 'city': '', 'department': '', 'role': '',
        'time_start': '', 'time_end': '', 'tags': '', 'notes': '',
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
                elif '补充说明' in section:
                    result['notes'] = block
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


def _parse_profile(person_id: str | None = None) -> dict:
    """解析 profile.md（简化版）"""
    profile_path = get_person_profile_path(person_id)
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


def load_all_experiences(person_id: str | None = None) -> list:
    """加载所有经历文件"""
    exp_dir = get_person_experiences_dir(person_id)
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
        notes_text = exp.get('notes', '').lower()
        full_text = tags_lower + ' ' + work_text + ' ' + notes_text

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

def _gen_education_section(profile: dict, jd_keywords: dict, language: str = 'zh') -> str:
    """生成教育背景 section"""
    language = normalize_language(language)
    section_title = 'Education' if language == 'en' else '教育背景'
    lines = [rf'\section{{{section_title}}}']
    jd_kw_set = set(k.lower() for cat in ['tech', 'domain'] for k in jd_keywords.get(cat, []))

    for edu in profile.get('education', []):
        if not edu.get('school'):
            continue
        school = tex_escape(edu['school'])
        degree = tex_escape(edu.get('degree', ''))
        time = tex_escape(_localize_date_text(_to_year_month_range(edu.get('time', '')), language))
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
            info_line += rf' \quad \textbf{{GPA:}} {gpa}'
            if rank:
                if language == 'en':
                    info_line += f', Rank: {rank}'
                else:
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
            if language == 'en':
                lines.append(r'\textbf{Relevant Coursework:} ' + '; '.join(formatted))
            else:
                lines.append(r'\textbf{主修课程：} ' + '；'.join(formatted))

        lines.append('')

    return '\n'.join(lines)


def _gen_experience_section(experiences: list, section_title: str = '实习经历', language: str = 'zh') -> str:
    """生成经历 section（实习 or 研究）"""
    if not experiences:
        return ''

    language = normalize_language(language)
    lines = [rf'\section{{{section_title}}}']

    for exp in experiences:
        company = tex_escape(exp.get('company', ''))
        city = tex_escape(exp.get('city', ''))
        role = tex_escape(exp.get('role', ''))
        dept = tex_escape(exp.get('department', ''))
        ts = _localize_date_text(_to_year_month(exp.get('time_start', '')), language)
        te = _localize_date_text(_to_year_month(exp.get('time_end', '')), language)
        date_range = f'{ts} -- {te}' if ts and te else (ts or te)
        lines.append(rf'\datedsubsection{{\textbf{{{company}}} \quad \normalsize {city}}}{{{tex_escape(date_range)}}}')
        lines.append(rf'\role{{{role}}}{{{dept}}}')
        lines.append(r'\vspace{-6pt}')
        lines.append(r'\begin{itemize}')

        selected_bullets = [bullet for bullet in exp.get('selected_bullets', []) if bullet]
        # Clamp to max 4 bullets (or 3 for research exp)
        max_bullets = 3 if _classify_experience(exp) == 'research' else 4
        if selected_bullets:
            for bullet in selected_bullets[:max_bullets]:
                lines.append(_render_bullet_latex(bullet))
        else:
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


def _gen_project_section(projects: list, language: str = 'zh') -> str:
    """生成项目经历 section"""
    if not projects:
        return ''

    language = normalize_language(language)
    section_title = 'Projects' if language == 'en' else '项目经历'
    lines = [rf'\section{{{section_title}}}']
    for proj in projects:
        name = tex_escape(proj.get('name', ''))
        role = tex_escape(proj.get('role', ''))
        time = tex_escape(_localize_date_text(_to_year_month_range(proj.get('time', '')), language))
        desc = tex_escape(proj.get('desc', ''))
        tags = tex_escape(proj.get('tags', ''))

        lines.append(rf'\datedsubsection{{\textbf{{{name}}}}}{{{time}}}')
        if role:
            tag_str = tags if tags else ''
            lines.append(rf'\role{{{role}}}{{{tag_str}}}')
        lines.append(r'\vspace{-8pt}')
        lines.append(r'\begin{itemize}')
        bullets = [bullet for bullet in proj.get('selected_bullets', []) if bullet]
        if bullets:
            for bullet in bullets[:2]:
                lines.append(_render_bullet_latex(bullet))
        else:
            lines.append(rf'    \item {desc}')
        lines.append(r'\end{itemize}')
        lines.append(r'\vspace{-4pt}')
        lines.append('')

    return '\n'.join(lines)


def _gen_publications_section(profile: dict, language: str = 'zh') -> str:
    """生成论文发表 section"""
    pubs = profile.get('publications', [])
    if not pubs:
        return ''

    language = normalize_language(language)
    section_title = 'Publications' if language == 'en' else '论文发表'
    lines = [rf'\section{{{section_title}}}', r'\vspace{2pt}']
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


def _gen_awards_section(awards: list, language: str = 'zh') -> str:
    """生成获奖情况 section"""
    awards = [a for a in awards if a.get('name')]
    if not awards:
        return ''

    language = normalize_language(language)
    section_title = 'Honors and Awards' if language == 'en' else '获奖情况'
    lines = [rf'\section{{{section_title}}}', r'\vspace{1pt}']
    for a in awards:
        name = tex_escape(a['name'])
        time = tex_escape(_localize_date_text(_to_year_month(a.get('time', '--')), language))
        if not time:
            time = '--'
        lines.append(rf'\datedline{{\textit{{{name}}}}}{{{time}}}')

    lines.append(r'\vspace{-2pt}')
    lines.append('')
    return '\n'.join(lines)


def _gen_skills_section(profile: dict, jd_keywords: dict, language: str = 'zh') -> str:
    """生成技能 section"""
    tech = profile.get('skills_tech', '')
    software = profile.get('skills_software', '')
    lang = profile.get('skills_lang', '')

    if not tech and not software and not lang:
        return ''

    language = normalize_language(language)
    section_title = 'Skills' if language == 'en' else '技能'
    lines = [rf'\section{{{section_title}}}', r'\begin{itemize}[parsep=0.5ex]']

    if language == 'en':
        if tech:
            lines.append(rf'    \item \textbf{{Programming \& Technical:}} {tex_escape(tech)}')
        if software:
            sw_line = rf'    \item \textbf{{Tools:}} {tex_escape(software)}'
            if lang:
                sw_line += rf' \quad \textbf{{Languages:}} {tex_escape(lang)}'
            lines.append(sw_line)
        elif lang:
            lines.append(rf'    \item \textbf{{Languages:}} {tex_escape(lang)}')
    else:
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


def generate_latex(profile: dict, experiences: list, jd_keywords: dict,
                   selected_projects: list | None = None,
                   selected_awards: list | None = None,
                   language: str = 'zh') -> str:
    """组装完整 .tex 文件"""
    language = normalize_language(language)

    # Header
    if language == 'en':
        name_value = profile.get('name_en', '') or profile.get('name_zh', '')
    else:
        zh = profile.get('name_zh', '')
        en = profile.get('name_en', '')
        name_value = f'{zh} {en}'.strip() if (zh or en) else ''
    name = tex_escape(name_value)
    email = profile.get('email', '')
    phone = profile.get('phone', '')
    github = profile.get('github', '')
    linkedin = profile.get('linkedin', '')

    header_lines = [
        r'% !TEX TS-program = xelatex',
        r'% !TEX encoding = UTF-8 Unicode',
        r'',
        r'\documentclass{resume}',
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
    if language == 'zh':
        header_lines.insert(4, r'\usepackage{zh_CN-Adobefonts_external}')

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
    sections.append(_gen_education_section(profile, jd_keywords, language=language))

    # 经历（分 研究 vs 实习）
    research_exp = [e for e in experiences if _classify_experience(e) == 'research']
    intern_exp = [e for e in experiences if e not in research_exp]

    if research_exp:
        research_title = 'Research Experience' if language == 'en' else '研究经历'
        sections.append(_gen_experience_section(research_exp, research_title, language=language))
    if intern_exp:
        exp_title = 'Experience' if language == 'en' else '实习经历'
        sections.append(_gen_experience_section(intern_exp, exp_title, language=language))

    # 项目
    sections.append(_gen_project_section(
        selected_projects if selected_projects is not None else profile.get('projects', [])[:2],
        language=language,
    ))

    # 论文
    sections.append(_gen_publications_section(profile, language=language))

    # 获奖
    sections.append(_gen_awards_section(
        selected_awards if selected_awards is not None else profile.get('awards', [])[:3],
        language=language,
    ))

    # 技能
    sections.append(_gen_skills_section(profile, jd_keywords, language=language))

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
        _tune_remove_project,
        _tune_reduce_bullets,
        _tune_reduce_font_size,
        _tune_reduce_section_spacing,
    ]

    for strategy in strategies:
        applied = strategy(tex_path, cls_path)
        if applied:
            actions.append(applied)
            # 重新编译检查
            result = _compile_and_check(tex_path.parent, tex_filename=tex_path.name)
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
        result = _compile_and_check(tex_path.parent, tex_filename=tex_path.name)
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


def _comment_out_section(tex_path: Path, section_name: str, label: str) -> str:
    content = tex_path.read_text(encoding='utf-8')
    lines = content.split('\n')

    start_idx = None
    for i, line in enumerate(lines):
        if rf'\section{{{section_name}}}' in line:
            start_idx = i
            break

    if start_idx is None:
        return ''

    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        stripped = lines[i].lstrip()
        if stripped.startswith(r'\section{') or stripped.startswith(r'\end{Form}'):
            end_idx = i
            break

    for i in range(start_idx, end_idx):
        if lines[i] and not lines[i].startswith('% '):
            lines[i] = '% ' + lines[i]

    content = '\n'.join(lines)
    tex_path.write_text(content, encoding='utf-8')
    return label


def _tune_remove_research(tex_path: Path, cls_path: Path) -> str:
    """注释掉研究经历 section"""
    for section_name, label in (
        ('研究经历', '删除研究经历 section'),
        ('Research Experience', '删除 Research Experience section'),
    ):
        applied = _comment_out_section(tex_path, section_name, label)
        if applied:
            return applied
    return ''


def _tune_remove_project(tex_path: Path, cls_path: Path) -> str:
    """注释掉项目经历 section"""
    for section_name, label in (
        ('项目经历', '删除项目经历 section'),
        ('Projects', '删除 Projects section'),
    ):
        applied = _comment_out_section(tex_path, section_name, label)
        if applied:
            return applied
    return ''


def _tune_reduce_bullets(tex_path: Path, cls_path: Path) -> str:
    """将每个 itemize 收紧到最多 2 条 bullet。"""
    lines = tex_path.read_text(encoding='utf-8').split('\n')
    in_itemize = False
    bullet_count = 0
    changed = False
    new_lines = []

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(r'\begin{itemize}'):
            in_itemize = True
            bullet_count = 0
            new_lines.append(line)
            continue
        if stripped.startswith(r'\end{itemize}'):
            in_itemize = False
            bullet_count = 0
            new_lines.append(line)
            continue
        if in_itemize and stripped.startswith(r'\item'):
            bullet_count += 1
            if bullet_count > 2:
                changed = True
                continue
        new_lines.append(line)

    if changed:
        tex_path.write_text('\n'.join(new_lines), encoding='utf-8')
        return '减少 bullet 数量至每段最多 2 条'
    return ''


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


def _compile_and_check(output_dir: Path, *, tex_filename: str = 'resume-zh_CN.tex') -> dict:
    """编译并检查填充率，返回 fill_data 或 None"""
    xelatex = find_xelatex()
    tex_file = output_dir / tex_filename

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

        aux_file = output_dir / f'{tex_file.stem}.aux'
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


def compile_latex(output_dir: Path, xelatex: str = None, *, tex_filename: str = 'resume-zh_CN.tex') -> dict:
    """编译 LaTeX，返回 {success, pdf_path, log}"""
    if not xelatex:
        xelatex = find_xelatex()

    tex_file = output_dir / tex_filename

    result = subprocess.run(
        [xelatex, '-interaction=nonstopmode', tex_file.name],
        cwd=str(output_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )

    pdf_file = output_dir / f'{tex_file.stem}.pdf'
    success = pdf_file.exists() and pdf_file.stat().st_size > 0

    return {
        'success': success,
        'pdf_path': str(pdf_file) if success else None,
        'log': result.stdout[-2000:] if result.stdout else '',
        'returncode': result.returncode,
    }


# ─── 主入口 ───────────────────────────────────────────────────

def generate_resume(jd_text: str, interview_text: str = '', *,
                    company: str = '', role: str = '',
                    person_id: str | None = None,
                    prefer_ai: bool = False,
                    feedback: str = '',
                    language: str = 'zh',
                    ai_config_override: dict | None = None) -> dict:
    """
    完整的简历生成流程。

    参数:
        jd_text: JD 原文
        interview_text: 面经文本（可选）
        company: 公司名覆盖（可选，优先于 JD 提取结果）
        role: 岗位名覆盖（可选，优先于 JD 提取结果）
        person_id: 人员 ID（None 表示使用活跃人员或 legacy 模式）

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
    # 自动迁移检查
    _maybe_migrate()

    # 解析 person_id：如果未指定，尝试获取活跃人员
    if person_id is None and is_multi_person_mode():
        person_id = get_active_person_id()
    language = normalize_language(language)
    tex_filename, pdf_filename = resolve_resume_filenames(language)
    log_lines = []
    engine = 'heuristic'
    ai_provider = None
    ai_model = None
    if ai_config_override is not None:
        ai_config = dict(ai_config_override)
        ai_config.setdefault('enabled', True)
    else:
        ai_config = get_model_config()

    # Merge feedback into interview_text (feedback takes priority: prepend)
    if feedback and feedback.strip():
        prefix = f'【用户修改反馈（优先遵守）】\n{feedback.strip()}\n\n'
        interview_text = prefix + (interview_text or '')

    gen_log.emit('step', f'▶ 开始生成简历  company={company or "自动"} role={role or "自动"}  person={person_id}',
                 data={'jd_preview': jd_text[:400], 'feedback': feedback})

    # Step 1: 加载数据
    log_lines.append('## 步骤 1: 加载用户数据')
    gen_log.emit('step', '步骤 1: 加载用户数据')
    profile_error = _profile_setup_error(person_id)
    if profile_error:
        return {'success': False, 'error': profile_error}
    experiences_error = _experiences_setup_error(person_id)
    if experiences_error:
        return {'success': False, 'error': experiences_error}
    profile = _parse_profile(person_id)
    experiences = load_all_experiences(person_id)
    log_lines.append(f'- 个人信息: {profile["name_zh"]}')
    log_lines.append(f'- 经历数量: {len(experiences)} 段')

    # Step 2: 分析 JD
    log_lines.append('\n## 步骤 2: 分析 JD 关键词')
    gen_log.emit('step', '步骤 2: 分析 JD 关键词')

    # 合并面经关键词
    combined_text = jd_text
    if interview_text:
        combined_text += '\n' + interview_text

    jd_keywords = extract_jd_keywords(combined_text)
    log_lines.append(f'- 技术关键词: {", ".join(jd_keywords["tech"][:10])}')
    log_lines.append(f'- 领域关键词: {", ".join(jd_keywords["domain"][:10])}')
    log_lines.append(f'- 公司: {jd_keywords["company"]}')
    log_lines.append(f'- 岗位: {jd_keywords["role"]}')
    gen_log.emit('info', f'JD 关键词 | 公司: {jd_keywords["company"]} | 岗位: {jd_keywords["role"]}',
                 data={'tech': jd_keywords['tech'][:15], 'domain': jd_keywords['domain'][:15]})

    # Step 3: 匹配经历
    log_lines.append('\n## 步骤 3: 匹配经历')
    gen_log.emit('step', '步骤 3: 匹配经历')
    matched = _apply_experience_selection_rules(experiences, jd_keywords)
    selected_projects = _filter_projects(profile, jd_keywords, remaining_slots=max(0, 5 - len(matched)))
    selected_awards = _filter_awards(profile, jd_keywords)

    if _should_try_ai(ai_config, prefer_ai=prefer_ai):
        try:
            ai_plan = _call_ai_resume_planner(ai_config, profile, experiences, jd_text, interview_text, person_id)
            # Use AI company/role; supplement jd_keywords from core_demands text for LaTeX highlighting
            _jd_understanding = ai_plan.get('jd_understanding') or {}
            _core_demands_text = ' '.join(_jd_understanding.get('core_demands', []))
            if _core_demands_text:
                _extra = extract_jd_keywords(_core_demands_text)
                for _cat in ('tech', 'domain', 'skill'):
                    for _kw in _extra.get(_cat, []):
                        if _kw not in jd_keywords.get(_cat, []):
                            jd_keywords.setdefault(_cat, []).append(_kw)
            if ai_plan.get('company'):
                jd_keywords['company'] = ai_plan['company']
            if ai_plan.get('role'):
                jd_keywords['role'] = ai_plan['role']
            matched = _apply_experience_selection_rules(
                experiences,
                jd_keywords,
                preferred_entries=ai_plan.get('selected_experiences', []),
            )
            selected_projects = _filter_projects(
                profile,
                jd_keywords,
                preferred_entries=ai_plan.get('selected_projects', []),
                remaining_slots=max(0, 5 - len(matched)),
            )
            selected_awards = _filter_awards(
                profile,
                jd_keywords,
                preferred_names=[item.get('name', '') for item in ai_plan.get('selected_awards', [])],
            )
            engine = 'ai'
            ai_provider = ai_plan.get('_provider', ai_config.get('provider'))
            ai_model = ai_plan.get('_model', ai_config.get('model'))
            log_lines.append(f'- 引擎: {ai_provider} ({ai_model})')
            _portrait = (ai_plan.get('jd_understanding') or {}).get('candidate_portrait', '') or ai_plan.get('job_summary', '')
            log_lines.append(f'- 候选人画像: {_portrait}')
            gen_log.emit('info', f'候选人画像: {_portrait[:120]}',
                         data={'portrait': _portrait,
                               'core_demands': (ai_plan.get('jd_understanding') or {}).get('core_demands', [])})
        except RuntimeError as exc:
            safe_exc = _redact_text_with_ai_config(str(exc), ai_config)
            if _should_force_ai(ai_config):
                return {'success': False, 'error': f'{ai_config.get("provider", "模型")} 生成失败：{safe_exc}'}
            log_lines.append(f'- 模型调用失败，已回退本地规则引擎: {safe_exc}')
    else:
        if _should_force_ai(ai_config):
            return {'success': False, 'error': '已启用模型生成，但当前环境未配置可用的 API Key 或模型'}
        log_lines.append('- 引擎: 本地规则引擎')

    for i, exp in enumerate(matched):
        score = exp.get('_score', 0)
        kws = ', '.join(exp.get('_matched', [])[:5])
        reason = exp.get('_reason', '')
        reason_suffix = f' | AI理由: {reason}' if reason else ''
        log_lines.append(f'- [{score}分] {exp["company"]} - {exp["role"]} (匹配: {kws}){reason_suffix}')
    if selected_projects:
        log_lines.append('- 项目经历: ' + '；'.join(project.get('name', '') for project in selected_projects))
    if selected_awards:
        log_lines.append('- 获奖情况: ' + '；'.join(award.get('name', '') for award in selected_awards))
    gen_log.emit('info', f'选中经历: {len(matched)} 段 | 项目: {len(selected_projects)} | 奖项: {len(selected_awards)}',
                 data={'experiences': [f'{e["company"]} - {e["role"]}' for e in matched],
                       'projects': [p.get('name') for p in selected_projects],
                       'awards': [a.get('name') for a in selected_awards]})

    # Step 4: 生成 LaTeX
    log_lines.append('\n## 步骤 4: 生成 LaTeX')
    gen_log.emit('step', '步骤 4: 生成 LaTeX')
    tex_content = generate_latex(
        profile,
        matched,
        jd_keywords,
        selected_projects=selected_projects,
        selected_awards=selected_awards,
        language=language,
    )
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

    output_base = get_person_output_dir(person_id)
    output_dir = output_base / dir_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # 复制模板文件（cls, sty 复制；fonts 用符号链接节省空间）
    for item in LATEX_TEMPLATE_DIR.iterdir():
        if item.name.endswith('.tex'):
            continue  # 不复制模板 tex
        dest = output_dir / item.name
        if dest.exists() or dest.is_symlink():
            continue
        if item.is_dir():
            os.symlink(item.resolve(), dest)
        elif item.is_file():
            shutil.copy2(item, dest)

    # 写入 tex
    tex_path = output_dir / tex_filename
    tex_path.write_text(tex_content, encoding='utf-8')
    log_lines.append(f'- 输出目录: {dir_name}')

    # Step 6: 编译
    log_lines.append('\n## 步骤 5: 编译 PDF')
    gen_log.emit('step', '步骤 5: 编译 PDF')
    compile_result = compile_latex(output_dir, tex_filename=tex_filename)

    if not compile_result['success']:
        log_lines.append('- 编译失败')
        gen_log.emit('error', '编译失败', data=compile_result.get('log', '')[:2000])
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
    gen_log.emit('compile', '编译成功')

    # Step 7: 填充率检查
    log_lines.append('\n## 步骤 6: 填充率检查')
    gen_log.emit('step', '步骤 6: 填充率检查')
    fill_ratio = 0.0
    fill_result = {}
    try:
        from tools.page_fill_check import check_page_fill
        fill_result = check_page_fill(str(output_dir), tex_filename=tex_filename)
        fill_ratio = fill_result.get('ratio', 0)
        page_count = fill_result.get('page_count', 1)
        log_lines.append(f'- 填充率: {fill_ratio * 100:.1f}%')
        log_lines.append(f'- 页数: {page_count}')
        log_lines.append(f'- 状态: {fill_result.get("message", "")}')
        gen_log.emit('info', f'填充率: {fill_ratio * 100:.1f}%  页数: {page_count}  {fill_result.get("message", "")}')
    except Exception as e:
        log_lines.append(f'- 填充率检查失败: {e}')
        gen_log.emit('error', f'填充率检查失败: {e}')
        fill_ratio = -1

    # Step 8: 自动调优（溢出或偏空时）
    tex_path = output_dir / tex_filename
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
            compile_result2 = compile_latex(output_dir, tex_filename=tex_filename)
            if compile_result2['success']:
                try:
                    fill_result = check_page_fill(str(output_dir), tex_filename=tex_filename)
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
        compile_result2 = compile_latex(output_dir, tex_filename=tex_filename)
        if compile_result2['success']:
            try:
                fill_result2 = check_page_fill(str(output_dir), tex_filename=tex_filename)
                fill_ratio = fill_result2.get('ratio', fill_ratio)
                log_lines.append(f'- 最终填充率: {fill_ratio * 100:.1f}%')
                log_lines.append(f'- 最终页数: {fill_result2.get("page_count", 1)}')
            except Exception:
                pass

    # 写 generation_log.md
    log_lines.append(f'\n## 生成时间\n{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    log_path = output_dir / 'generation_log.md'
    log_path.write_text('# Generation Log\n\n' + '\n'.join(log_lines), encoding='utf-8')
    _write_generation_context(
        output_dir,
        jd_text=jd_text,
        interview_text=interview_text,
        company=company,
        role=role_,
        engine=engine,
        ai_provider=ai_provider,
        ai_model=ai_model,
        fill_ratio=fill_ratio,
        language=language,
    )

    pdf_rel = f'{dir_name}/{pdf_filename}'

    gen_log.emit('done', f'✅ 生成完成  填充率: {fill_ratio*100:.1f}%  输出: {dir_name}')

    return {
        'success': True,
        'output_dir': dir_name,
        'pdf_path': pdf_rel,
        'language': language,
        'tex_filename': tex_filename,
        'company': company,
        'role': role_,
        'fill_ratio': fill_ratio,
        'generation_log': '\n'.join(log_lines),
        'engine': engine,
        'ai_provider': ai_provider,
        'ai_model': ai_model,
        'error': None,
    }


if __name__ == '__main__':
    import sys
    # 自动迁移
    _maybe_migrate()

    # 简单的 CLI 参数解析
    person_id = None
    language = 'zh'
    args = sys.argv[1:]
    filtered_args = []
    i = 0
    while i < len(args):
        if args[i] in ('--person', '-p') and i + 1 < len(args):
            person_id = args[i + 1]
            i += 2
        elif args[i] in ('--language', '-l') and i + 1 < len(args):
            language = args[i + 1]
            i += 2
        else:
            filtered_args.append(args[i])
            i += 1

    if not filtered_args:
        print("用法: python3 tools/generate_resume.py [--person ID] [--language zh|en] '你的JD文本'")
        sys.exit(1)

    jd = filtered_args[0]
    try:
        result = generate_resume(jd, person_id=person_id, language=language)
    except ValueError as exc:
        print(str(exc))
        sys.exit(2)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
