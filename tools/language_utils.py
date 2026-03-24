from __future__ import annotations

from pathlib import Path
import json


ALLOWED_LANGUAGES = {'zh', 'en'}
_LANGUAGE_DEFAULT = 'zh'

_LANGUAGE_FILENAME_MAP = {
    'zh': ('resume-zh_CN.tex', 'resume-zh_CN.pdf'),
    'en': ('resume-en.tex', 'resume-en.pdf'),
}


def normalize_language(value: str | None) -> str:
    raw = str(value or '').strip().lower()
    if not raw:
        return _LANGUAGE_DEFAULT
    if raw not in ALLOWED_LANGUAGES:
        raise ValueError(f'invalid language: {raw}; allowed: zh,en')
    return raw


def resolve_resume_filenames(language: str | None) -> tuple[str, str]:
    normalized = normalize_language(language)
    return _LANGUAGE_FILENAME_MAP[normalized]


def infer_language_from_output_dir(output_dir: Path) -> str:
    context_path = output_dir / 'generation_context.json'
    if context_path.exists():
        try:
            payload = json.loads(context_path.read_text(encoding='utf-8'))
            return normalize_language(payload.get('language'))
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    if (output_dir / 'resume-en.tex').exists():
        return 'en'
    if (output_dir / 'resume-zh_CN.tex').exists():
        return 'zh'
    return _LANGUAGE_DEFAULT

