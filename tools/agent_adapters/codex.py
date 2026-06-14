from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import AgentAdapter

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None


class CodexAdapter(AgentAdapter):
    name = 'codex'
    executable = 'codex'
    VALID_SERVICE_TIERS = {'fast', 'flex'}

    def config_path(self) -> Path:
        codex_home = os.getenv('CODEX_HOME')
        if codex_home:
            return Path(codex_home).expanduser() / 'config.toml'
        return Path.home() / '.codex' / 'config.toml'

    def _read_config(self) -> dict[str, Any]:
        path = self.config_path()
        if not path.exists():
            return {}
        if tomllib:
            with path.open('rb') as fh:
                return tomllib.load(fh)
        config: dict[str, Any] = {}
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            key, value = stripped.split('=', 1)
            config[key.strip()] = value.strip().strip('"').strip("'")
        return config

    def health_check(self) -> dict[str, Any]:
        health = super().health_check()
        if not health.get('ok'):
            return health
        config_path = self.config_path()
        try:
            config = self._read_config()
        except Exception as exc:
            return {
                **health,
                'ok': False,
                'status': 'fail',
                'detail': f'Codex CLI 已安装，但无法读取配置 {config_path}: {exc}',
                'action': '修复 Codex 配置',
                'config_path': str(config_path),
            }

        service_tier = str(config.get('service_tier') or '').strip()
        if service_tier and service_tier not in self.VALID_SERVICE_TIERS:
            return {
                **health,
                'ok': False,
                'status': 'fail',
                'detail': (
                    f'Codex CLI 已安装，但 {config_path} 中 service_tier="{service_tier}" '
                    '会导致启动失败；请改为 fast 或 flex'
                ),
                'action': '修复 Codex 配置',
                'config_path': str(config_path),
                'error_code': 'CODEX_CONFIG_INVALID_SERVICE_TIER',
            }
        return {
            **health,
            'detail': f'Codex CLI 可用{f"，service_tier={service_tier}" if service_tier else ""}',
            'config_path': str(config_path) if config_path.exists() else '',
        }

    def command(self) -> list[str]:
        return [
            self.executable,
            'exec',
            '--cd',
            str(self.project_root),
            '--sandbox',
            'workspace-write',
            '--json',
            '-',
        ]
