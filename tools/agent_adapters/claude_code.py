from __future__ import annotations

from .base import AgentAdapter


class ClaudeCodeAdapter(AgentAdapter):
    name = 'claude'
    executable = 'claude'

    def command(self) -> list[str]:
        return [
            self.executable,
            '-p',
            '--output-format',
            'stream-json',
            '--permission-mode',
            'acceptEdits',
        ]

