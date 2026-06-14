from __future__ import annotations

import re
from pathlib import Path


LOCAL_SKILL_CATALOG = {
    'tech': [
        ('Python', [r'\bPython\b']),
        ('SQL', [r'\bSQL\b']),
        ('Pandas', [r'\bPandas\b']),
        ('NumPy', [r'\bNumPy\b']),
        ('Selenium', [r'\bSelenium\b']),
        ('Stata', [r'\bStata\b']),
        ('R', [r'\bR\b']),
        ('C', [r'(?<![A-Za-z])C(?![A-Za-z+#])']),
        ('C++', [r'\bC\+\+\b', r'C\+\+']),
        ('JavaScript', [r'\bJavaScript\b']),
        ('TypeScript', [r'\bTypeScript\b']),
        ('HTML', [r'\bHTML\b']),
        ('CSS', [r'\bCSS\b']),
        ('React', [r'\bReact\b']),
        ('Electron', [r'\bElectron\b']),
        ('LaTeX', [r'\bLaTeX\b']),
        ('XeLaTeX', [r'\bXeLaTeX\b']),
        ('Node.js', [r'\bNode\.js\b', r'\bNodeJS\b']),
        ('API 对接', [r'\bAPI\b', r'接口对接']),
        ('Prompt Engineering', [r'\bPrompt\b', r'提示词']),
        ('Function Call', [r'\bFunction\s+Call\b']),
        ('AI Agent', [r'\bAgent\b', r'智能体']),
        ('机器学习', [r'机器学习', r'\bMachine\s+Learning\b']),
        ('深度学习', [r'深度学习', r'\bDeep\s+Learning\b']),
        ('NLP', [r'\bNLP\b', r'自然语言处理']),
        ('LLM', [r'\bLLM\b', r'大模型']),
        ('数据分析', [r'数据分析', r'\bData\s+Analysis\b']),
        ('量化研究', [r'量化研究', r'量化']),
    ],
    'software': [
        ('Cursor', [r'\bCursor\b']),
        ('Codex', [r'\bCodex\b']),
        ('Claude Code', [r'\bClaude\s+Code\b']),
        ('Coze', [r'\bCoze\b']),
        ('n8n', [r'\bn8n\b']),
        ('Microsoft Office', [r'\bMicrosoft\s+Office\b']),
        ('Excel', [r'\bExcel\b']),
        ('PowerPoint', [r'\bPowerPoint\b', r'\bPPT\b']),
        ('Word', [r'\bWord\b']),
        ('Visio', [r'\bVisio\b']),
        ('Figma', [r'\bFigma\b']),
        ('Notion', [r'\bNotion\b']),
        ('Tableau', [r'\bTableau\b']),
        ('Power BI', [r'\bPower\s+BI\b']),
        ('Git', [r'\bGit\b']),
        ('GitHub', [r'\bGitHub\b']),
        ('Docker', [r'\bDocker\b']),
        ('Bloomberg', [r'\bBloomberg\b']),
        ('Wind', [r'\bWind\b']),
        ('CSMAR', [r'\bCSMAR\b']),
        ('Choice', [r'\bChoice\b']),
    ],
}


def split_skill_values(raw: object) -> list[str]:
    return [item.strip() for item in re.split(r'[,，;；\n]', str(raw or '')) if item.strip()]


def count_skill_values(skills: dict[str, list[str] | str]) -> int:
    total = 0
    for value in skills.values():
        if isinstance(value, list):
            total += len([item for item in value if str(item).strip()])
        else:
            total += len(split_skill_values(value))
    return total


def existing_skill_values_from_profile(profile: dict) -> set[str]:
    values: set[str] = set()
    nested = profile.get('skills') if isinstance(profile.get('skills'), dict) else {}
    for value in nested.values():
        values.update(item.lower() for item in split_skill_values(value))
    for key in ('skills_tech', 'skills_software', 'skills_lang'):
        values.update(item.lower() for item in split_skill_values(profile.get(key, '')))
    return values


def safe_read_text(path: Path, limit: int = 200_000) -> str:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return ''
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    return text[:limit]


def derive_skills_from_text(corpus: str, *, existing: set[str] | None = None) -> dict[str, list[str]]:
    existing = existing or set()
    suggestions: dict[str, list[str]] = {'tech': [], 'software': [], 'languages': []}

    def add(category: str, value: str) -> None:
        if value.lower() in existing:
            return
        if value not in suggestions[category]:
            suggestions[category].append(value)

    for category, entries in LOCAL_SKILL_CATALOG.items():
        for label, patterns in entries:
            if any(re.search(pattern, corpus, flags=re.IGNORECASE) for pattern in patterns):
                add(category, label)

    language_parts: list[str] = []
    ielts = re.search(r'\bIELTS\s*[:：]?\s*([0-9](?:\.[0-9])?)\b', corpus, flags=re.IGNORECASE)
    cet6 = re.search(r'\bCET\s*[- ]?6\s*[:：]?\s*([0-9]{3})\b', corpus, flags=re.IGNORECASE)
    cet4 = re.search(r'\bCET\s*[- ]?4\s*[:：]?\s*([0-9]{3})\b', corpus, flags=re.IGNORECASE)
    if re.search(r'英语|英文|\bEnglish\b|\bIELTS\b|\bCET\b', corpus, flags=re.IGNORECASE):
        if ielts:
            language_parts.append(f'IELTS {ielts.group(1)}')
        if cet6:
            language_parts.append(f'CET6 {cet6.group(1)}')
        if cet4:
            language_parts.append(f'CET4 {cet4.group(1)}')
        label = '英语'
        if language_parts:
            label += ' - ' + ', '.join(language_parts)
        add('languages', label)
    if re.search(r'中文|普通话|\bChinese\b|\bMandarin\b', corpus, flags=re.IGNORECASE):
        add('languages', '中文')

    return suggestions


def merge_skill_strings(saved: str, derived: list[str], *, max_items: int) -> str:
    items: list[str] = []
    seen: set[str] = set()
    for item in [*split_skill_values(saved), *derived]:
        normalized = item.lower()
        if not item or normalized in seen:
            continue
        items.append(item)
        seen.add(normalized)
        if len(items) >= max_items:
            break
    return ', '.join(items)
