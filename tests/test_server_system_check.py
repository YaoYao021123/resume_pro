import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web import server  # noqa: E402


class ServerSystemCheckTests(unittest.TestCase):
    def _profile(self):
        return {
            'basic': {
                'name_zh': '姚尧',
                'email': 'davidyaofin@163.com',
                'phone': '18362821123',
            },
            'education': [{'school': '香港大学'}],
            'skills': {'tech': 'Python, SQL', 'software': 'LaTeX', 'languages': 'English'},
            'projects': [{'name': 'Resume Gen'}],
            'campus_experiences': [],
        }

    def test_system_check_ready_when_required_capabilities_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            exp_dir = Path(tmp)
            (exp_dir / '百度.md').write_text('experience', encoding='utf-8')
            with (
                mock.patch.object(server, 'parse_profile', return_value=self._profile()),
                mock.patch.object(server, '_experiences_dir', return_value=exp_dir),
                mock.patch.object(server, '_find_xelatex', return_value='/usr/bin/xelatex'),
                mock.patch.object(server, '_binary_available', return_value=(True, '/usr/bin/xelatex')),
                mock.patch.object(server, '_check_output_writable', return_value=(True, '/tmp/output')),
                mock.patch('tools.generate_resume._find_pdftoppm', return_value='/usr/bin/pdftoppm'),
                mock.patch('tools.agent_adapters.codex.CodexAdapter.health_check', return_value={'ok': True, 'name': 'codex', 'detail': 'Codex CLI 可用'}),
                mock.patch('tools.agent_adapters.claude_code.ClaudeCodeAdapter.health_check', return_value={'ok': False, 'name': 'claude', 'detail': '未找到 claude', 'error_code': 'AGENT_NOT_AVAILABLE'}),
                mock.patch.object(server, 'list_gallery_resumes', return_value=[{'dir_name': 'demo'}]),
            ):
                result = server.build_system_check()

        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['label'], '系统就绪')
        self.assertTrue(all(c['status'] == 'pass' for c in result['checks']))
        self.assertEqual(result['agent_health'][0]['name'], 'codex')
        self.assertTrue(result['agent_health'][0]['ok'])

    def test_system_check_blocks_when_required_items_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(server, 'parse_profile', return_value={}),
                mock.patch.object(server, '_experiences_dir', return_value=Path(tmp)),
                mock.patch.object(server, '_find_xelatex', return_value='xelatex'),
                mock.patch.object(server, '_binary_available', return_value=(False, 'xelatex')),
                mock.patch.object(server, '_check_output_writable', return_value=(True, '/tmp/output')),
                mock.patch('tools.generate_resume._find_pdftoppm', return_value=None),
                mock.patch('tools.agent_adapters.codex.CodexAdapter.health_check', return_value={'ok': False, 'name': 'codex', 'detail': '未找到 codex', 'error_code': 'AGENT_NOT_AVAILABLE'}),
                mock.patch('tools.agent_adapters.claude_code.ClaudeCodeAdapter.health_check', return_value={'ok': False, 'name': 'claude', 'detail': '未找到 claude', 'error_code': 'AGENT_NOT_AVAILABLE'}),
                mock.patch.object(server, 'list_gallery_resumes', return_value=[]),
            ):
                result = server.build_system_check()

        self.assertEqual(result['status'], 'blocked')
        failed_required = {c['key'] for c in result['checks'] if c['required'] and c['status'] == 'fail'}
        self.assertIn('profile', failed_required)
        self.assertIn('xelatex', failed_required)
        self.assertIn('local_agent', failed_required)

    def test_system_check_warns_missing_skills_without_blocking_generation(self):
        profile = self._profile()
        profile['skills'] = {'tech': '', 'software': '', 'languages': ''}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_path = root / 'profile.md'
            profile_path.write_text('姚尧', encoding='utf-8')
            exp_dir = root / 'experiences'
            exp_dir.mkdir()
            (exp_dir / '百度.md').write_text('experience', encoding='utf-8')
            materials_dir = root / 'work_materials'
            materials_dir.mkdir()
            output_dir = root / 'output'
            output_dir.mkdir()
            with (
                mock.patch.object(server, 'parse_profile', return_value=profile),
                mock.patch.object(server, '_profile_path', return_value=profile_path),
                mock.patch.object(server, '_experiences_dir', return_value=exp_dir),
                mock.patch.object(server, '_work_materials_dir', return_value=materials_dir),
                mock.patch.object(server, '_output_dir', return_value=output_dir),
                mock.patch.object(server, '_find_xelatex', return_value='/usr/bin/xelatex'),
                mock.patch.object(server, '_binary_available', return_value=(True, '/usr/bin/xelatex')),
                mock.patch.object(server, '_check_output_writable', return_value=(True, '/tmp/output')),
                mock.patch('tools.generate_resume._find_pdftoppm', return_value='/usr/bin/pdftoppm'),
                mock.patch('tools.agent_adapters.codex.CodexAdapter.health_check', return_value={'ok': True, 'name': 'codex', 'detail': 'Codex CLI 可用'}),
                mock.patch('tools.agent_adapters.claude_code.ClaudeCodeAdapter.health_check', return_value={'ok': False, 'name': 'claude', 'detail': '未找到 claude', 'error_code': 'AGENT_NOT_AVAILABLE'}),
                mock.patch.object(server, 'list_gallery_resumes', return_value=[{'dir_name': 'demo'}]),
            ):
                result = server.build_system_check()

        profile_check = next(c for c in result['checks'] if c['key'] == 'profile')
        skill_check = next(c for c in result['checks'] if c['key'] == 'profile_skills')
        self.assertEqual(result['status'], 'attention')
        self.assertEqual(profile_check['status'], 'pass')
        self.assertEqual(skill_check['status'], 'warning')
        self.assertFalse(skill_check['required'])
        self.assertEqual(skill_check['target_page'], 'skills')
        self.assertEqual(skill_check['action'], '从资料提取')

    def test_system_check_passes_missing_saved_skills_when_derived_skills_exist(self):
        profile = self._profile()
        profile['skills'] = {'tech': '', 'software': '', 'languages': ''}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_path = root / 'profile.md'
            profile_path.write_text('姚尧', encoding='utf-8')
            exp_dir = root / 'experiences'
            exp_dir.mkdir()
            (exp_dir / '百度.md').write_text('使用 Python、SQL、Pandas 和 Function Call 搭建 AI Agent', encoding='utf-8')
            materials_dir = root / 'work_materials'
            materials_dir.mkdir()
            output_dir = root / 'output'
            output_dir.mkdir()
            with (
                mock.patch.object(server, 'parse_profile', return_value=profile),
                mock.patch.object(server, '_profile_path', return_value=profile_path),
                mock.patch.object(server, '_experiences_dir', return_value=exp_dir),
                mock.patch.object(server, '_work_materials_dir', return_value=materials_dir),
                mock.patch.object(server, '_output_dir', return_value=output_dir),
                mock.patch.object(server, '_find_xelatex', return_value='/usr/bin/xelatex'),
                mock.patch.object(server, '_binary_available', return_value=(True, '/usr/bin/xelatex')),
                mock.patch.object(server, '_check_output_writable', return_value=(True, '/tmp/output')),
                mock.patch('tools.generate_resume._find_pdftoppm', return_value='/usr/bin/pdftoppm'),
                mock.patch('tools.agent_adapters.codex.CodexAdapter.health_check', return_value={'ok': True, 'name': 'codex', 'detail': 'Codex CLI 可用'}),
                mock.patch('tools.agent_adapters.claude_code.ClaudeCodeAdapter.health_check', return_value={'ok': False, 'name': 'claude', 'detail': '未找到 claude', 'error_code': 'AGENT_NOT_AVAILABLE'}),
                mock.patch.object(server, 'list_gallery_resumes', return_value=[{'dir_name': 'demo'}]),
            ):
                result = server.build_system_check()

        skill_check = next(c for c in result['checks'] if c['key'] == 'profile_skills')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(skill_check['status'], 'pass')
        self.assertTrue(skill_check['derived'])
        self.assertGreaterEqual(skill_check['derived_count'], 4)
        self.assertEqual(skill_check['action'], '查看/保存')

    def test_agent_run_preflight_blocks_required_failures(self):
        system_check = {
            'status': 'blocked',
            'checks': [
                {
                    'key': 'profile',
                    'label': '个人资料',
                    'status': 'fail',
                    'detail': '缺少基本信息',
                    'action': '完善资料',
                    'required': True,
                    'target_page': 'basic',
                },
                {
                    'key': 'pdftoppm',
                    'label': '视觉截图',
                    'status': 'warning',
                    'detail': '未找到 pdftoppm',
                    'action': '安装 poppler / pdftoppm',
                    'required': False,
                    'target_page': '',
                },
            ],
        }
        with mock.patch.object(server, 'build_system_check', return_value=system_check):
            result = server.build_agent_run_preflight_failure()

        self.assertIsNotNone(result)
        self.assertEqual(result['error_code'], 'SYSTEM_CHECK_BLOCKED')
        self.assertEqual(result['blocking_check']['key'], 'profile')
        self.assertEqual(result['blocking_check']['target_page'], 'basic')

    def test_agent_run_preflight_allows_optional_warnings(self):
        system_check = {
            'status': 'attention',
            'checks': [
                {
                    'key': 'profile',
                    'label': '个人资料',
                    'status': 'pass',
                    'detail': '已填写',
                    'action': '',
                    'required': True,
                    'target_page': '',
                },
                {
                    'key': 'pdftoppm',
                    'label': '视觉截图',
                    'status': 'warning',
                    'detail': '未找到 pdftoppm',
                    'action': '安装 poppler / pdftoppm',
                    'required': False,
                    'target_page': '',
                },
            ],
        }
        with mock.patch.object(server, 'build_system_check', return_value=system_check):
            result = server.build_agent_run_preflight_failure()

        self.assertIsNone(result)

    def test_agent_run_preflight_blocks_unhealthy_selected_agent(self):
        system_check = {
            'status': 'attention',
            'checks': [
                {
                    'key': 'profile',
                    'label': '个人资料',
                    'status': 'pass',
                    'detail': '已填写',
                    'action': '',
                    'required': True,
                    'target_page': '',
                },
                {
                    'key': 'local_agent',
                    'label': '本地 Agent',
                    'status': 'warning',
                    'detail': '可用：Claude Code；需处理：Codex service_tier 异常',
                    'action': '修复本地 Agent',
                    'required': True,
                    'target_page': '',
                },
            ],
        }
        def fake_selected_agent_health(agent):
            if agent == 'claude':
                return {
                    'ok': True,
                    'name': 'claude',
                    'detail': 'Claude Code 可用',
                }
            return {
                'ok': False,
                'name': 'codex',
                'detail': 'Codex CLI 已安装，但 service_tier="default" 会导致启动失败',
                'action': '修复本地 Agent',
                'error_code': 'CODEX_CONFIG_INVALID_SERVICE_TIER',
            }

        with (
            mock.patch.object(server, 'build_system_check', return_value=system_check),
            mock.patch.object(server, '_selected_agent_health', side_effect=fake_selected_agent_health),
        ):
            result = server.build_agent_run_preflight_failure({'agent': 'codex'})

        self.assertIsNotNone(result)
        self.assertEqual(result['error_code'], 'SELECTED_AGENT_UNHEALTHY')
        self.assertEqual(result['blocking_check']['key'], 'selected_agent')
        self.assertEqual(result['blocking_check']['agent'], 'codex')
        self.assertIn('service_tier', result['blocking_check']['detail'])
        self.assertEqual(result['blocking_check']['fallback_agent'], 'claude')
        self.assertEqual(result['blocking_check']['fallback_label'], 'Claude Code')
        selected_check = result['system_check']['checks'][-1]
        self.assertEqual(selected_check['key'], 'selected_agent')
        self.assertEqual(selected_check['status'], 'fail')

    def test_local_skill_suggestions_extract_from_local_materials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_path = root / 'profile.md'
            profile_path.write_text(
                server.render_profile({
                    'basic': {'name_zh': '姚尧', 'email': 'davidyaofin@163.com', 'phone': '18362821123'},
                    'education': [{'school': '香港大学'}],
                    'skills': {'tech': 'Python', 'software': '', 'languages': ''},
                }),
                encoding='utf-8',
            )
            exp_dir = root / 'experiences'
            exp_dir.mkdir()
            (exp_dir / '百度.md').write_text('使用 Python、SQL、Pandas、Function Call 和 Agent 搭建方案', encoding='utf-8')
            materials_dir = root / 'work_materials'
            company_dir = materials_dir / '项目'
            company_dir.mkdir(parents=True)
            (company_dir / 'notes.txt').write_text('Cursor Codex Claude Code Figma', encoding='utf-8')
            output_dir = root / 'output'
            resume_dir = output_dir / 'demo'
            resume_dir.mkdir(parents=True)
            (resume_dir / 'resume-zh_CN.tex').write_text('IELTS 8.0, CET6 631, LaTeX, TypeScript', encoding='utf-8')

            with (
                mock.patch.object(server, '_profile_path', return_value=profile_path),
                mock.patch.object(server, '_experiences_dir', return_value=exp_dir),
                mock.patch.object(server, '_work_materials_dir', return_value=materials_dir),
                mock.patch.object(server, '_output_dir', return_value=output_dir),
            ):
                result = server.build_local_skill_suggestions()

        self.assertTrue(result['success'])
        self.assertNotIn('Python', result['suggestions']['tech'])
        self.assertIn('SQL', result['suggestions']['tech'])
        self.assertIn('Pandas', result['suggestions']['tech'])
        self.assertIn('Function Call', result['suggestions']['tech'])
        self.assertIn('Cursor', result['suggestions']['software'])
        self.assertIn('Claude Code', result['suggestions']['software'])
        self.assertIn('英语 - IELTS 8.0, CET6 631', result['suggestions']['languages'])

    def test_system_check_attention_when_only_optional_items_are_missing(self):
        profile = self._profile()
        profile['projects'] = []
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(server, 'parse_profile', return_value=profile),
                mock.patch.object(server, '_experiences_dir', return_value=Path(tmp)),
                mock.patch.object(server, '_find_xelatex', return_value='/usr/bin/xelatex'),
                mock.patch.object(server, '_binary_available', return_value=(True, '/usr/bin/xelatex')),
                mock.patch.object(server, '_check_output_writable', return_value=(True, '/tmp/output')),
                mock.patch('tools.generate_resume._find_pdftoppm', return_value=None),
                mock.patch('tools.agent_adapters.codex.CodexAdapter.health_check', return_value={'ok': True, 'name': 'codex', 'detail': 'Codex CLI 可用'}),
                mock.patch('tools.agent_adapters.claude_code.ClaudeCodeAdapter.health_check', return_value={'ok': False, 'name': 'claude', 'detail': '未找到 claude', 'error_code': 'AGENT_NOT_AVAILABLE'}),
                mock.patch.object(server, 'list_gallery_resumes', return_value=[]),
            ):
                result = server.build_system_check()

        self.assertEqual(result['status'], 'attention')
        warning_keys = {c['key'] for c in result['checks'] if c['status'] == 'warning'}
        self.assertIn('content_library', warning_keys)
        self.assertIn('pdftoppm', warning_keys)
        self.assertIn('gallery', warning_keys)

    def test_system_check_passes_when_one_agent_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            exp_dir = Path(tmp)
            (exp_dir / '百度.md').write_text('experience', encoding='utf-8')
            with (
                mock.patch.object(server, 'parse_profile', return_value=self._profile()),
                mock.patch.object(server, '_experiences_dir', return_value=exp_dir),
                mock.patch.object(server, '_find_xelatex', return_value='/usr/bin/xelatex'),
                mock.patch.object(server, '_binary_available', return_value=(True, '/usr/bin/xelatex')),
                mock.patch.object(server, '_check_output_writable', return_value=(True, '/tmp/output')),
                mock.patch('tools.generate_resume._find_pdftoppm', return_value='/usr/bin/pdftoppm'),
                mock.patch('tools.agent_adapters.codex.CodexAdapter.health_check', return_value={
                    'ok': False,
                    'name': 'codex',
                    'detail': 'Codex service_tier="default" 会导致启动失败；请改为 fast 或 flex',
                    'action': '修复 Codex 配置',
                    'error_code': 'CODEX_CONFIG_INVALID_SERVICE_TIER',
                    'config_path': '/tmp/codex/config.toml',
                }),
                mock.patch('tools.agent_adapters.claude_code.ClaudeCodeAdapter.health_check', return_value={
                    'ok': True,
                    'name': 'claude',
                    'detail': 'Claude Code CLI 可用',
                }),
                mock.patch.object(server, 'list_gallery_resumes', return_value=[{'dir_name': 'demo'}]),
            ):
                result = server.build_system_check()

        local_agent = next(c for c in result['checks'] if c['key'] == 'local_agent')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(local_agent['status'], 'pass')
        self.assertEqual(local_agent['action'], '')
        self.assertIn('service_tier', local_agent['detail'])
        self.assertIn('不影响自动选择', local_agent['detail'])
        codex_health = next(item for item in result['agent_health'] if item['name'] == 'codex')
        claude_health = next(item for item in result['agent_health'] if item['name'] == 'claude')
        self.assertFalse(codex_health['ok'])
        self.assertEqual(codex_health['error_code'], 'CODEX_CONFIG_INVALID_SERVICE_TIER')
        self.assertEqual(codex_health['config_path'], '/tmp/codex/config.toml')
        self.assertTrue(any('service_tier' in step for step in codex_health['recovery_steps']))
        self.assertTrue(claude_health['ok'])

    def test_agent_run_preflight_allows_auto_agent_when_fallback_is_healthy(self):
        system_check = {
            'status': 'ready',
            'checks': [
                {
                    'key': 'local_agent',
                    'label': '本地 Agent',
                    'status': 'pass',
                    'detail': '可用：Claude Code',
                    'action': '',
                    'required': True,
                    'target_page': '',
                },
            ],
        }
        with (
            mock.patch.object(server, 'build_system_check', return_value=system_check),
            mock.patch.object(server, '_selected_agent_health', return_value={
                'ok': True,
                'name': 'claude',
                'detail': '自动选择：Claude Code；Claude Code 可用',
                'selected_agent': 'auto',
            }),
        ):
            result = server.build_agent_run_preflight_failure({'agent': 'auto'})

        self.assertIsNone(result)

    def test_runtime_info_builds_local_delivery_payload(self):
        result = server.build_runtime_info(host='127.0.0.1:9123', port=9123)

        self.assertTrue(result['success'])
        self.assertEqual(result['app_url'], 'http://127.0.0.1:9123')
        self.assertEqual(result['launch_command'], 'python3 web/server.py --port 9123')
        self.assertEqual(result['health_url'], 'http://127.0.0.1:9123/api/system-check')
        self.assertEqual(result['agent_jobs_url'], 'http://127.0.0.1:9123/api/agent/jobs')
        self.assertEqual(len(result['steps']), 4)
        step_keys = [step['key'] for step in result['steps']]
        self.assertEqual(step_keys, ['start', 'check', 'generate', 'loop'])
        serialized = str(result)
        self.assertNotIn('jd_text', serialized.lower())
        self.assertNotIn('完整 JD 不应展示', serialized)
        self.assertNotIn('api_key', serialized.lower())

    def test_workbench_status_blocks_on_required_system_failure(self):
        system_check = {
            'status': 'blocked',
            'label': '需先修复',
            'summary': '存在会阻断生成的问题',
            'next_action': '完善资料',
            'readiness': 58,
            'checks': [
                {
                    'key': 'profile',
                    'label': '个人资料',
                    'status': 'fail',
                    'detail': '缺少基本信息',
                    'action': '完善资料',
                    'required': True,
                    'target_page': 'basic',
                },
            ],
        }
        with (
            mock.patch.object(server, 'build_system_check', return_value=system_check),
            mock.patch.object(server, 'list_gallery_resumes', return_value=[]),
            mock.patch('tools.agent_orchestrator.list_agent_jobs', return_value=(200, {'success': True, 'jobs': []})),
        ):
            result = server.build_workbench_status()

        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['action']['type'], 'system_check')
        self.assertEqual(result['action']['target_page'], 'basic')
        self.assertEqual(result['system']['readiness'], 58)
        self.assertEqual(result['system']['blockers'][0]['key'], 'profile')
        self.assertEqual(result['system']['blockers'][0]['detail'], '缺少基本信息')
        self.assertEqual(result['system']['warnings'], [])

    def test_workbench_status_prefers_running_agent_job(self):
        system_check = {
            'status': 'ready',
            'label': '系统就绪',
            'summary': 'ready',
            'next_action': '粘贴 JD 开始生成',
            'readiness': 100,
            'checks': [],
        }
        jobs = [{
            'job_id': 'agent_running',
            'status': 'running',
            'recoverable': True,
            'agent': 'codex',
            'company': '目标公司',
            'role': '商品研究',
        }]
        with (
            mock.patch.object(server, 'build_system_check', return_value=system_check),
            mock.patch.object(server, 'list_gallery_resumes', return_value=[]),
            mock.patch('tools.agent_orchestrator.list_agent_jobs', return_value=(200, {'success': True, 'jobs': jobs})),
        ):
            result = server.build_workbench_status()

        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['action']['type'], 'resume_agent_job')
        self.assertEqual(result['action']['job_id'], 'agent_running')
        self.assertEqual(result['agent']['running_job']['job_id'], 'agent_running')

    def test_workbench_status_summarizes_latest_resume_without_raw_context(self):
        system_check = {
            'status': 'ready',
            'label': '系统就绪',
            'summary': 'ready',
            'next_action': '粘贴 JD 开始生成',
            'readiness': 100,
            'checks': [],
        }
        resume = {
            'company': '目标公司',
            'role': '商品研究AI策略',
            'dir_name': '目标公司_商品研究AI策略_20260612',
            'date': '2026/06/12',
            'pdf_path': '目标公司_商品研究AI策略_20260612/resume-zh_CN.pdf',
            'language': 'zh',
            'version_count': 1,
            'jd_text': '完整 JD 不应展示',
            'interview_text': '完整面经不应展示',
            'quality_report': {
                'status': 'ready',
                'label': '可投递',
                'summary': 'PDF 单页、填充率理想且视觉 QA 通过',
                'next_action': '投递前人工复核',
                'fill_label': '98.0%',
                'page_count': 1,
                'version_count': 1,
            },
            'workflow_status': {
                'status': 'complete',
                'label': '闭环完成',
                'progress': 100,
                'next_action': '投递前人工复核',
            },
            'visual_review': {'page_count': 1},
        }
        with (
            mock.patch.object(server, 'build_system_check', return_value=system_check),
            mock.patch.object(server, 'list_gallery_resumes', return_value=[resume]),
            mock.patch('tools.agent_orchestrator.list_agent_jobs', return_value=(200, {'success': True, 'jobs': []})),
        ):
            result = server.build_workbench_status()

        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['action']['type'], 'quality_report')
        self.assertEqual(result['gallery']['latest_resume']['dir_name'], resume['dir_name'])
        self.assertEqual(result['gallery']['complete_count'], 1)
        serialized = str(result)
        self.assertNotIn('完整 JD 不应展示', serialized)
        self.assertNotIn('完整面经不应展示', serialized)

    def test_product_readiness_ready_when_product_loop_is_complete(self):
        runtime_info = {
            'success': True,
            'app_url': 'http://127.0.0.1:8765',
            'launch_command': 'python3 web/server.py --port 8765',
            'agent_jobs_url': 'http://127.0.0.1:8765/api/agent/jobs',
            'steps': [
                {'key': 'start'},
                {'key': 'check'},
                {'key': 'generate'},
                {'key': 'loop'},
            ],
        }
        system_check = {
            'status': 'ready',
            'label': '系统就绪',
            'summary': 'ready',
            'next_action': '粘贴 JD 开始生成',
            'readiness': 100,
            'checks': [
                {
                    'key': 'local_agent',
                    'label': '本地 Agent',
                    'status': 'pass',
                    'detail': '可用：Codex',
                    'action': '',
                    'required': True,
                },
            ],
        }
        resume = {
            'company': '蚂蚁',
            'role': '产品经理-商业',
            'dir_name': '蚂蚁_产品经理-商业_20260310',
            'date': '2026/03/10',
            'pdf_path': '蚂蚁_产品经理-商业_20260310/resume-zh_CN.pdf',
            'language': 'zh',
            'version_count': 1,
            'quality_report': {
                'status': 'ready',
                'label': '可投递',
                'summary': 'PDF 单页、填充率理想且视觉 QA 通过',
                'next_action': '投递前人工复核',
                'fill_label': '96.3%',
                'page_count': 1,
                'version_count': 1,
            },
            'workflow_status': {
                'status': 'complete',
                'label': '闭环完成',
                'progress': 100,
                'next_action': '投递前人工复核',
            },
            'visual_review': {
                'status': 'pass',
                'page_count': 1,
                'agent_feedback': {'prompt': '保持通过状态'},
            },
        }
        workbench_status = {
            'success': True,
            'status': 'ready',
            'label': '最近简历可投递',
            'summary': 'PDF 单页、填充率理想且视觉 QA 通过',
            'action': {'type': 'quality_report', 'label': '打开质量报告'},
            'gallery': {'latest_resume': resume},
        }
        delivery_manifest = {
            'quality': {'status': 'ready', 'label': '可投递'},
            'workflow': {'status': 'complete', 'progress': 100},
            'files': [
                '蚂蚁_产品经理-商业_20260310/resume-zh_CN.pdf',
                '蚂蚁_产品经理-商业_20260310/quality_report.json',
                '蚂蚁_产品经理-商业_20260310/workflow_status.json',
            ],
        }
        with (
            mock.patch.object(server, 'build_runtime_info', return_value=runtime_info),
            mock.patch.object(server, 'build_system_check', return_value=system_check),
            mock.patch.object(server, 'build_workbench_status', return_value=workbench_status),
            mock.patch.object(server, 'list_gallery_resumes', return_value=[resume]),
            mock.patch.object(server, '_resolve_gallery_output_dir', return_value=Path('/tmp/resume')),
            mock.patch.object(server, '_build_delivery_package', return_value=(b'zip', 'delivery.zip', delivery_manifest)),
        ):
            result = server.build_product_readiness(host='127.0.0.1:8765', port=8765)

        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['label'], '产品就绪')
        self.assertEqual(result['readiness'], 100)
        self.assertTrue(all(result['requirements'].values()))
        self.assertTrue(all(c['status'] == 'pass' for c in result['checks']))
        self.assertEqual(result['latest_resume']['dir_name'], resume['dir_name'])

    def test_product_readiness_blocks_when_required_product_path_is_missing(self):
        runtime_info = {
            'success': True,
            'app_url': 'http://127.0.0.1:8765',
            'launch_command': 'python3 web/server.py --port 8765',
            'agent_jobs_url': '',
            'steps': [{'key': 'start'}],
        }
        system_check = {
            'status': 'blocked',
            'label': '需先修复',
            'summary': 'blocked',
            'next_action': '配置 Agent',
            'readiness': 40,
            'checks': [
                {
                    'key': 'local_agent',
                    'label': '本地 Agent',
                    'status': 'fail',
                    'detail': '未找到 Codex 或 Claude Code',
                    'action': '安装或登录本地 Agent CLI',
                    'required': True,
                },
            ],
        }
        workbench_status = {
            'success': True,
            'status': 'blocked',
            'label': '先修复本地环境',
            'summary': 'blocked',
            'action': {'type': 'system_check', 'label': '配置 Agent'},
            'gallery': {'latest_resume': None},
        }
        with (
            mock.patch.object(server, 'build_runtime_info', return_value=runtime_info),
            mock.patch.object(server, 'build_system_check', return_value=system_check),
            mock.patch.object(server, 'build_workbench_status', return_value=workbench_status),
            mock.patch.object(server, 'list_gallery_resumes', return_value=[]),
        ):
            result = server.build_product_readiness(host='127.0.0.1:8765', port=8765)

        self.assertEqual(result['status'], 'blocked')
        self.assertLess(result['readiness'], 100)
        failed_required = {c['key'] for c in result['checks'] if c['required'] and c['status'] == 'fail'}
        self.assertIn('local_agent_backend', failed_required)
        self.assertIn('visual_feedback_loop', failed_required)
        self.assertFalse(result['requirements']['delivery_package'])

    def test_diagnostics_package_exports_safe_summaries(self):
        system_check = {
            'status': 'ready',
            'label': '系统就绪',
            'summary': 'ready',
            'next_action': '粘贴 JD 开始生成',
            'readiness': 100,
            'checks': [],
        }
        resume = {
            'company': '目标公司',
            'role': '商品研究AI策略',
            'dir_name': '目标公司_商品研究AI策略_20260612',
            'date': '2026/06/12',
            'pdf_path': '目标公司_商品研究AI策略_20260612/resume-zh_CN.pdf',
            'language': 'zh',
            'version_count': 1,
            'jd_text': '完整 JD 不应展示',
            'interview_text': '完整面经不应展示',
            'quality_report': {
                'status': 'ready',
                'label': '可投递',
                'summary': 'PDF 单页、填充率理想且视觉 QA 通过',
                'next_action': '投递前人工复核',
                'fill_label': '98.0%',
                'page_count': 1,
                'version_count': 1,
            },
            'workflow_status': {
                'status': 'complete',
                'label': '闭环完成',
                'progress': 100,
                'next_action': '投递前人工复核',
            },
            'visual_review': {'page_count': 1},
        }
        jobs = [{
            'job_id': 'agent_failed',
            'status': 'failed',
            'agent': 'codex',
            'company': '目标公司',
            'role': '商品研究',
            'error': '完整 JD 不应展示',
            'error_code': 'AGENT_FAILED',
            'task_summary': {
                'agent': 'codex',
                'company': '目标公司',
                'role': '商品研究',
                'language': 'zh',
                'has_interview': True,
                'has_selection_plan': False,
                'has_feedback': True,
            },
        }]
        with (
            mock.patch.object(server, 'build_system_check', return_value=system_check),
            mock.patch.object(server, 'list_gallery_resumes', return_value=[resume]),
            mock.patch('tools.agent_orchestrator.list_agent_jobs', return_value=(200, {'success': True, 'jobs': jobs})),
        ):
            content, filename, manifest = server._build_diagnostics_package(host='127.0.0.1:8765', port=8765)

        self.assertTrue(filename.endswith('.zip'))
        self.assertEqual(manifest['package_type'], 'resume_generator_pro_diagnostics')
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / filename
            zip_path.write_bytes(content)
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                joined = '\n'.join(names)
                self.assertIn('diagnostics.json', joined)
                self.assertIn('system_check.json', joined)
                self.assertIn('workbench_status.json', joined)
                self.assertIn('product_readiness.json', joined)
                self.assertIn('agent_jobs.json', joined)
                self.assertIn('gallery_resumes.json', joined)
                self.assertFalse(any(name.endswith('generation_context.json') for name in names))
                all_text = '\n'.join(
                    zf.read(name).decode('utf-8', errors='ignore')
                    for name in names
                    if name.endswith(('.json', '.txt'))
                )
                self.assertFalse(any(name.endswith('/stdout.log') for name in names))
                self.assertFalse(any(name.endswith('/stderr.log') for name in names))
        self.assertIn('完整 JD 不应展示', resume['jd_text'])
        self.assertNotIn('完整 JD 不应展示', all_text)
        self.assertNotIn('完整面经不应展示', all_text)
        self.assertIn('stdout.log', all_text)
        self.assertIn('stderr.log', all_text)


if __name__ == '__main__':
    unittest.main()
