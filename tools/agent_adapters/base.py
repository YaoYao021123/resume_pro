from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentLaunch:
    process: subprocess.Popen
    command: list[str]


class AgentAdapter:
    """Base class for local coding-agent CLI adapters."""

    name = ''
    executable = ''

    def __init__(self, project_root: Path):
        self.project_root = project_root

    def is_available(self) -> bool:
        return bool(self.executable and shutil.which(self.executable))

    def health_check(self) -> dict[str, Any]:
        executable_path = shutil.which(self.executable) if self.executable else None
        if not executable_path:
            return {
                'ok': False,
                'status': 'fail',
                'name': self.name,
                'executable': self.executable,
                'detail': f'{self.executable} CLI 未安装或不在 PATH 中',
                'action': '安装或登录本地 Agent CLI',
                'error_code': 'AGENT_NOT_AVAILABLE',
            }
        return {
            'ok': True,
            'status': 'pass',
            'name': self.name,
            'executable': self.executable,
            'path': executable_path,
            'detail': f'已找到 {self.executable}',
            'action': '',
        }

    def command(self) -> list[str]:
        raise NotImplementedError

    def launch(self, *, prompt_path: Path, stdout_path: Path, stderr_path: Path) -> AgentLaunch:
        if not self.is_available():
            raise FileNotFoundError(f'{self.executable} CLI 未安装或不在 PATH 中')

        cmd = self.command()
        stdin_f = prompt_path.open('rb')
        stdout_f = stdout_path.open('ab')
        stderr_f = stderr_path.open('ab')
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                stdin=stdin_f,
                stdout=stdout_f,
                stderr=stderr_f,
            )
        except Exception:
            stdin_f.close()
            stdout_f.close()
            stderr_f.close()
            raise
        return AgentLaunch(process=process, command=cmd)
