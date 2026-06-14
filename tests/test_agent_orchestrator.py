import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import agent_orchestrator as orch
from tools.agent_adapters.codex import CodexAdapter


class AgentOrchestratorTests(unittest.TestCase):
    def test_codex_health_check_rejects_invalid_service_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / 'config.toml').write_text('service_tier = "default"\n', encoding='utf-8')
            adapter = CodexAdapter(Path(tmp))
            with (
                mock.patch.dict('os.environ', {'CODEX_HOME': str(config_dir)}),
                mock.patch('shutil.which', return_value='/usr/local/bin/codex'),
            ):
                health = adapter.health_check()

        self.assertFalse(health['ok'])
        self.assertEqual(health['status'], 'fail')
        self.assertEqual(health['error_code'], 'CODEX_CONFIG_INVALID_SERVICE_TIER')
        self.assertIn('fast 或 flex', health['detail'])

    def test_start_agent_job_rejects_missing_jd(self):
        status, data = orch.start_agent_job({'agent': 'codex', 'jd': ''}, active_person_id='default')
        self.assertEqual(status, 400)
        self.assertEqual(data['error'], '请输入 JD 内容')

    def test_start_agent_job_rejects_unknown_agent(self):
        status, data = orch.start_agent_job({'agent': 'trae', 'jd': 'JD'}, active_person_id='default')
        self.assertEqual(status, 400)
        self.assertEqual(data['error_code'], 'UNSUPPORTED_AGENT')

    def test_auto_agent_uses_first_healthy_fallback(self):
        class BadCodex:
            name = 'codex'

            def __init__(self, project_root):
                self.project_root = project_root

            def health_check(self):
                return {
                    'ok': False,
                    'detail': 'Codex service_tier 配置错误',
                    'error_code': 'CODEX_CONFIG_INVALID_SERVICE_TIER',
                }

        class GoodClaude:
            name = 'claude'

            def __init__(self, project_root):
                self.project_root = project_root

            def health_check(self):
                return {'ok': True, 'detail': 'Claude Code 可用'}

        with (
            mock.patch.object(orch, 'CodexAdapter', BadCodex),
            mock.patch.object(orch, 'ClaudeCodeAdapter', GoodClaude),
        ):
            adapter = orch._adapter_for('auto')

        self.assertEqual(adapter.name, 'claude')

    def test_start_agent_job_rejects_unhealthy_selected_agent(self):
        class FakeAdapter:
            name = 'codex'
            executable = 'codex'

            def health_check(self):
                return {
                    'ok': False,
                    'detail': 'Codex service_tier 配置错误',
                    'error_code': 'CODEX_CONFIG_INVALID_SERVICE_TIER',
                }

        with (
            mock.patch.object(orch, '_adapter_for', return_value=FakeAdapter()),
            mock.patch.object(orch, '_resolve_person_id', return_value='default'),
            mock.patch.object(orch, '_active_running_jobs', return_value=[]),
        ):
            status, data = orch.start_agent_job({'agent': 'codex', 'jd': 'JD'}, active_person_id='default')

        self.assertEqual(status, 400)
        self.assertEqual(data['error_code'], 'CODEX_CONFIG_INVALID_SERVICE_TIER')
        self.assertIn('agent_health', data)

    def test_get_agent_job_rejects_unsafe_job_id(self):
        status, data = orch.get_agent_job('../bad')
        self.assertEqual(status, 400)
        self.assertEqual(data['error_code'], 'INVALID_JOB_ID')

    def test_get_agent_job_log_returns_incremental_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(orch, 'JOB_ROOT', Path(tmp)):
                job_dir = Path(tmp) / 'agent_test'
                job_dir.mkdir(parents=True)
                (job_dir / 'events.jsonl').write_text(
                    json.dumps({'seq': 1, 'message': 'one'}, ensure_ascii=False) + '\n'
                    + json.dumps({'seq': 2, 'message': 'two'}, ensure_ascii=False) + '\n',
                    encoding='utf-8',
                )

                status, data = orch.get_agent_job_log('agent_test', since_seq=1)

        self.assertEqual(status, 200)
        self.assertEqual([e['message'] for e in data['events']], ['two'])
        self.assertEqual(data['next_seq'], 2)

    def test_get_agent_job_returns_task_summary_without_full_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(orch, 'JOB_ROOT', Path(tmp)):
                job_dir = Path(tmp) / 'agent_test'
                job_dir.mkdir(parents=True)
                (job_dir / 'status.json').write_text(
                    json.dumps({'job_id': 'agent_test', 'status': 'failed', 'language': 'zh'}, ensure_ascii=False),
                    encoding='utf-8',
                )
                (job_dir / 'task.json').write_text(
                    json.dumps({
                        'agent': 'codex',
                        'company': '测试公司',
                        'role': 'AI 产品经理',
                        'language': 'zh',
                        'jd': '完整 JD 不应返回',
                        'interview': '面经',
                        'feedback': '更偏产品',
                        'selection_plan': {'selected_experiences': []},
                    }, ensure_ascii=False),
                    encoding='utf-8',
                )

                status, data = orch.get_agent_job('agent_test')

        self.assertEqual(status, 200)
        self.assertEqual(data['task_summary']['company'], '测试公司')
        self.assertTrue(data['task_summary']['has_interview'])
        self.assertTrue(data['task_summary']['has_selection_plan'])
        self.assertNotIn('jd', data['task_summary'])
        self.assertNotIn('完整 JD 不应返回', json.dumps(data, ensure_ascii=False))

    def test_list_agent_jobs_returns_safe_recent_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(orch, 'JOB_ROOT', Path(tmp)):
                running_dir = Path(tmp) / 'agent_running'
                running_dir.mkdir(parents=True)
                (running_dir / 'status.json').write_text(
                    json.dumps({
                        'job_id': 'agent_running',
                        'status': 'running',
                        'agent': 'claude',
                        'company': '测试公司',
                        'role': 'AI 产品经理',
                        'language': 'zh',
                        'progress': 35,
                    }, ensure_ascii=False),
                    encoding='utf-8',
                )
                (running_dir / 'task.json').write_text(
                    json.dumps({
                        'agent': 'claude',
                        'company': '测试公司',
                        'role': 'AI 产品经理',
                        'language': 'zh',
                        'jd': '完整 JD 不应返回',
                        'interview': '面经',
                    }, ensure_ascii=False),
                    encoding='utf-8',
                )
                done_dir = Path(tmp) / 'agent_done'
                done_dir.mkdir(parents=True)
                (done_dir / 'status.json').write_text(
                    json.dumps({'job_id': 'agent_done', 'status': 'completed'}, ensure_ascii=False),
                    encoding='utf-8',
                )

                status, data = orch.list_agent_jobs(limit=10)

        self.assertEqual(status, 200)
        by_id = {item['job_id']: item for item in data['jobs']}
        self.assertTrue(by_id['agent_running']['recoverable'])
        self.assertFalse(by_id['agent_done']['recoverable'])
        self.assertEqual(by_id['agent_running']['task_summary']['company'], '测试公司')
        self.assertTrue(by_id['agent_running']['task_summary']['has_interview'])
        self.assertNotIn('jd', by_id['agent_running']['task_summary'])
        self.assertNotIn('完整 JD 不应返回', json.dumps(data, ensure_ascii=False))

    def test_apply_memory_suggestions_requires_user_confirmed_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profile = tmp_path / 'profile.md'
            profile.write_text('profile', encoding='utf-8')
            with mock.patch.object(orch, 'JOB_ROOT', tmp_path / 'jobs'):
                job_dir = orch.JOB_ROOT / 'agent_test'
                job_dir.mkdir(parents=True)
                (job_dir / 'status.json').write_text(
                    json.dumps({'job_id': 'agent_test', 'person_id': 'default'}, ensure_ascii=False),
                    encoding='utf-8',
                )
                (job_dir / 'memory_suggestions.json').write_text(
                    json.dumps({'suggestions': [{'type': 'preference', 'text': '突出产品策略'}]}, ensure_ascii=False),
                    encoding='utf-8',
                )
                with mock.patch.object(orch, 'get_person_profile_path', return_value=profile):
                    status, data = orch.apply_memory_suggestions('agent_test')

            memory_file = tmp_path / 'memory' / 'agent_suggestions.jsonl'
            self.assertEqual(status, 200)
            self.assertEqual(data['count'], 1)
            self.assertTrue(memory_file.exists())
            self.assertIn('突出产品策略', memory_file.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
