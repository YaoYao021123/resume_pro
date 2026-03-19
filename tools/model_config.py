#!/usr/bin/env python3
"""Environment-backed model provider configuration."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / '.env.local'

MODEL_PROVIDER_PRESETS = {
    'openai': {
        'label': 'OpenAI',
        'platform_url': 'https://platform.openai.com/',
        'docs_url': 'https://platform.openai.com/docs/overview',
        'default_base_url': 'https://api.openai.com/v1',
        'default_model': 'gpt-5',
        'models': ['gpt-5', 'gpt-5-mini', 'gpt-4.1'],
        'api_style': 'openai',
    },
    'gemini': {
        'label': 'Gemini',
        'platform_url': 'https://aistudio.google.com/',
        'docs_url': 'https://ai.google.dev/gemini-api/docs',
        'default_base_url': 'https://generativelanguage.googleapis.com/v1beta/openai',
        'default_model': 'gemini-3-flash-preview',
        'models': ['gemini-3-flash-preview', 'gemini-3.1-pro-preview', 'gemini-2.5-pro', 'gemini-2.0-flash'],
        'api_style': 'openai',
        'supports_json_object': True,
        'supports_thinking_off': False,
    },
    'anthropic': {
        'label': 'Anthropic',
        'platform_url': 'https://console.anthropic.com/',
        'docs_url': 'https://docs.anthropic.com/en/docs/about-claude/models',
        'default_base_url': 'https://api.anthropic.com',
        'default_model': 'claude-sonnet-4-6',
        'models': ['claude-sonnet-4-6', 'claude-opus-4-6', 'claude-haiku-4-5'],
        'api_style': 'anthropic',
    },
    'glm': {
        'label': 'GLM',
        'platform_url': 'https://open.bigmodel.cn/',
        'docs_url': 'https://open.bigmodel.cn/dev/howuse/model',
        'default_base_url': 'https://open.bigmodel.cn/api/paas/v4',
        'default_model': 'glm-4.5',
        'models': ['glm-4.5', 'glm-4.5-air', 'glm-4.5-flash'],
        'api_style': 'openai',
    },
    'kimi': {
        'label': 'Kimi',
        'platform_url': 'https://platform.moonshot.cn/',
        'docs_url': 'https://platform.moonshot.cn/docs',
        'default_base_url': 'https://api.moonshot.cn/v1',
        'default_model': 'kimi-k2-turbo-preview',
        'models': ['kimi-k2-turbo-preview', 'kimi-k2-preview', 'moonshot-v1-128k'],
        'api_style': 'openai',
    },
    'minimax': {
        'label': 'MiniMax',
        'platform_url': 'https://platform.minimaxi.com/',
        'docs_url': 'https://platform.minimaxi.com/document',
        'default_base_url': 'https://api.minimax.chat/v1',
        'default_model': 'MiniMax-M1',
        'models': ['MiniMax-M1', 'MiniMax-Text-01', 'abab7-chat-preview'],
        'api_style': 'openai',
    },
    'grok': {
        'label': 'Grok / xAI',
        'platform_url': 'https://console.x.ai/',
        'docs_url': 'https://docs.x.ai/docs/models',
        'default_base_url': 'https://api.x.ai/v1',
        'default_model': 'grok-4',
        'models': ['grok-4', 'grok-3-mini', 'grok-3'],
        'api_style': 'openai',
    },
    'qwen': {
        'label': 'Qwen / 阿里百炼',
        'platform_url': 'https://bailian.console.aliyun.com/',
        'docs_url': 'https://help.aliyun.com/zh/model-studio/getting-started/models',
        'default_base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'default_model': 'qwen-max',
        'models': ['qwen-max', 'qwen-plus', 'qwen-flash', 'qwen-coder-plus'],
        'api_style': 'openai',
    },
    'doubao': {
        'label': 'Doubao / 火山方舟',
        'platform_url': 'https://console.volcengine.com/ark',
        'docs_url': 'https://www.volcengine.com/docs/82379/1330310',
        'default_base_url': 'https://ark.cn-beijing.volces.com/api/v3',
        'default_model': 'doubao-seed-2-0-pro-260215',
        'models': [
            'doubao-seed-2-0-pro-260215', 'doubao-seed-2-0-lite-250121',
            'doubao-seed-2.0-pro', 'doubao-seed-2.0-lite', 'doubao-seed-2.0-code',
            'doubao-seed-code', 'doubao-1-5-pro-32k',
            'deepseek-v3.2', 'minimax-m2.5', 'glm-4.7', 'kimi-k2.5',
        ],
        'api_style': 'openai',
        'supports_json_object': False,   # doubao rejects response_format json_object
        'supports_thinking_off': True,   # doubao-seed supports thinking:{type:disabled}
    },
    'other': {
        'label': '其他 / 自定义',
        'platform_url': '',
        'docs_url': '',
        'default_base_url': '',
        'default_model': '',
        'models': [],
        'api_style': 'openai',
    },
}

TRACKED_ENV_KEYS = [
    'RESUME_USE_AI',
    'RESUME_MODEL_PROVIDER',
    'RESUME_MODEL_NAME',
    'RESUME_API_BASE_URL',
    'RESUME_API_KEY',
    'RESUME_API_PLATFORM_URL',
]

LEGACY_ENV_KEYS = ['GEMINI_API_KEY', 'RESUME_AI_MODEL', 'RESUME_USE_GEMINI', 'RESUME_GEMINI_REQUIRED']
_ENV_LOADED = False


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_env_file(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not path.exists():
        return parsed
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        parsed[key.strip()] = _strip_wrapping_quotes(value.strip())
    return parsed


def load_local_env(force: bool = False) -> dict[str, str]:
    global _ENV_LOADED
    if _ENV_LOADED and not force:
        return {key: os.environ.get(key, '') for key in TRACKED_ENV_KEYS}
    parsed = _parse_env_file(ENV_FILE)
    for key, value in parsed.items():
        os.environ[key] = value
    _ENV_LOADED = True
    return parsed


def _env_flag(name: str) -> bool:
    value = os.environ.get(name, '').strip().lower()
    return value in {'1', 'true', 'yes', 'on'}


def get_provider_presets() -> list[dict]:
    return [{'id': provider_id, **meta} for provider_id, meta in MODEL_PROVIDER_PRESETS.items()]


def get_model_config() -> dict:
    load_local_env()
    provider = os.environ.get('RESUME_MODEL_PROVIDER', '').strip().lower()
    if not provider and os.environ.get('GEMINI_API_KEY', '').strip():
        provider = 'gemini'
    if provider not in MODEL_PROVIDER_PRESETS:
        provider = 'other' if provider else 'gemini'
    preset = MODEL_PROVIDER_PRESETS.get(provider, MODEL_PROVIDER_PRESETS['other'])
    api_key = os.environ.get('RESUME_API_KEY', '').strip() or os.environ.get('GEMINI_API_KEY', '').strip()
    model = os.environ.get('RESUME_MODEL_NAME', '').strip() or os.environ.get('RESUME_AI_MODEL', '').strip() or preset.get('default_model', '')
    base_url = os.environ.get('RESUME_API_BASE_URL', '').strip() or preset.get('default_base_url', '')
    platform_url = os.environ.get('RESUME_API_PLATFORM_URL', '').strip() or preset.get('platform_url', '')
    if 'RESUME_USE_AI' in os.environ:
        enabled = _env_flag('RESUME_USE_AI')
    else:
        enabled = _env_flag('RESUME_USE_GEMINI') or bool(api_key)
    return {
        'enabled': enabled,
        'provider': provider,
        'model': model,
        'base_url': base_url,
        'api_key': api_key,
        'platform_url': platform_url,
        'providers': get_provider_presets(),
        'api_style': preset.get('api_style', 'openai'),
        'supports_json_object': preset.get('supports_json_object', True),
        'supports_thinking_off': preset.get('supports_thinking_off', False),
    }


def _quote_env_value(value: str) -> str:
    if value == '':
        return '""'
    if any(ch.isspace() for ch in value) or '#' in value or '"' in value:
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return value


def save_model_config(config: dict) -> dict:
    load_local_env()
    existing = _parse_env_file(ENV_FILE)
    provider = str(config.get('provider', '')).strip().lower()
    if provider not in MODEL_PROVIDER_PRESETS:
        provider = 'other'
    preset = MODEL_PROVIDER_PRESETS[provider]
    enabled = bool(config.get('enabled'))
    model = str(config.get('model', '')).strip() or preset.get('default_model', '')
    base_url = str(config.get('base_url', '')).strip() or preset.get('default_base_url', '')
    api_key = str(config.get('api_key', '')).strip()
    platform_url = str(config.get('platform_url', '')).strip() or preset.get('platform_url', '')

    updates = {
        'RESUME_USE_AI': '1' if enabled else '0',
        'RESUME_MODEL_PROVIDER': provider,
        'RESUME_MODEL_NAME': model,
        'RESUME_API_BASE_URL': base_url,
        'RESUME_API_KEY': api_key,
        'RESUME_API_PLATFORM_URL': platform_url,
    }

    for key in LEGACY_ENV_KEYS:
        existing.pop(key, None)
        os.environ.pop(key, None)
    for key, value in updates.items():
        existing[key] = value
        os.environ[key] = value

    lines = ['# Local AI model configuration for Resume Generator Pro']
    for key, value in existing.items():
        if key in LEGACY_ENV_KEYS:
            continue
        lines.append(f'{key}={_quote_env_value(value)}')
    ENV_FILE.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return get_model_config()
