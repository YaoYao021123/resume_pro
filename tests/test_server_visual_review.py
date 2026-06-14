import json
import sys
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web.server import (  # noqa: E402
    _build_resume_quality_report,
    _build_resume_workflow_status,
    _build_delivery_package,
    _build_visual_review_agent_feedback,
    _create_manual_version_snapshot,
    _create_version_snapshot,
    _enrich_agent_job_payload,
    _get_version_count,
    _measure_resume_metrics,
    _normalize_fill_ratio_value,
    _output_dir,
    _with_visual_review_screenshot_path,
)


class ServerVisualReviewTests(unittest.TestCase):
    def test_normalize_fill_ratio_accepts_ratio_or_percent(self):
        self.assertEqual(_normalize_fill_ratio_value(0.98), 0.98)
        self.assertEqual(_normalize_fill_ratio_value(98.0), 0.98)
        self.assertEqual(_normalize_fill_ratio_value(''), 0.0)

    def test_measure_resume_metrics_fills_missing_cached_values(self):
        with tempfile.TemporaryDirectory(dir=_output_dir()) as tmp:
            out_dir = Path(tmp)
            (out_dir / 'resume-zh_CN.tex').write_text('resume body', encoding='utf-8')

            with mock.patch('web.server._find_xelatex', return_value='xelatex'), \
                 mock.patch('tools.page_fill_check.check_page_fill', return_value={
                     'ratio': 0.963,
                     'page_count': 1,
                     'status': 'good',
                     'message': '页面填充率 96.3%，在理想范围内',
                 }) as check_page_fill:
                metrics = _measure_resume_metrics(
                    out_dir,
                    'zh',
                    context={'fill_ratio': 0.0},
                    existing_review={'page_count': 0},
                )

            self.assertEqual(metrics['fill_ratio'], 0.963)
            self.assertEqual(metrics['page_count'], 1)
            check_page_fill.assert_called_once()

    def test_measure_resume_metrics_uses_cached_values_when_complete(self):
        with tempfile.TemporaryDirectory(dir=_output_dir()) as tmp:
            out_dir = Path(tmp)

            with mock.patch('tools.page_fill_check.check_page_fill') as check_page_fill:
                metrics = _measure_resume_metrics(
                    out_dir,
                    'zh',
                    context={'fill_ratio': 0.98, 'page_count': 1},
                )

            self.assertEqual(metrics['fill_ratio'], 0.98)
            self.assertEqual(metrics['page_count'], 1)
            check_page_fill.assert_not_called()

    def test_screenshot_path_is_relative_to_output_dir(self):
        with tempfile.TemporaryDirectory(dir=_output_dir()) as tmp:
            out_dir = Path(tmp)
            review = {'screenshots': ['visual_review/page-1.png']}

            enriched = _with_visual_review_screenshot_path(out_dir, review)

            self.assertEqual(
                enriched['screenshot_path'],
                f'{out_dir.name}/visual_review/page-1.png',
            )
            self.assertIn('agent_feedback', enriched)
            self.assertIn('prompt', enriched['agent_feedback'])

    def test_agent_feedback_for_passing_review_keeps_scope_stable(self):
        feedback = _build_visual_review_agent_feedback({
            'status': 'pass',
            'fill_ratio': 0.98,
            'page_count': 1,
            'issues': [],
        })

        self.assertIn('保持通过状态', feedback['summary'])
        self.assertIn('当前视觉 QA 已通过', feedback['prompt'])

    def test_agent_feedback_for_underfilled_review_requests_more_content(self):
        feedback = _build_visual_review_agent_feedback({
            'status': 'warning',
            'fill_ratio': 0.90,
            'page_count': 1,
            'issues': ['underfilled'],
        })

        self.assertIn('补足页面', feedback['summary'])
        self.assertIn('页面偏空', feedback['prompt'])

    def test_agent_feedback_for_overflow_review_requests_compression(self):
        feedback = _build_visual_review_agent_feedback({
            'status': 'warning',
            'fill_ratio': 1.02,
            'page_count': 2,
            'issues': ['overflow', 'page_count_not_one'],
        })

        self.assertIn('控制到单页', feedback['summary'])
        self.assertIn('压缩溢出', feedback['summary'])

    def test_quality_report_marks_ready_resume_as_deliverable(self):
        report = _build_resume_quality_report(
            pdf_exists=True,
            visual_review={
                'status': 'pass',
                'fill_ratio': 0.98,
                'page_count': 1,
                'screenshots': ['visual_review/page-1.png'],
                'agent_feedback': {'prompt': 'keep stable'},
            },
            version_count=2,
        )

        self.assertEqual(report['status'], 'ready')
        self.assertEqual(report['label'], '可投递')
        self.assertEqual(report['next_action'], '投递前人工复核')
        self.assertTrue(all(c['status'] == 'pass' for c in report['checks']))

    def test_quality_report_marks_missing_visual_review_as_review_needed(self):
        report = _build_resume_quality_report(
            pdf_exists=True,
            visual_review={},
            version_count=0,
        )

        self.assertEqual(report['status'], 'review')
        self.assertEqual(report['label'], '需复核')
        self.assertEqual(report['next_action'], '运行视觉 QA')
        self.assertIn('pending', {c['status'] for c in report['checks']})

    def test_quality_report_marks_overflow_as_fix_needed(self):
        report = _build_resume_quality_report(
            pdf_exists=True,
            visual_review={
                'status': 'warning',
                'fill_ratio': 1.02,
                'page_count': 2,
                'issues': ['overflow', 'page_count_not_one'],
                'agent_feedback': {'prompt': 'compress'},
            },
            version_count=1,
        )

        self.assertEqual(report['status'], 'fix')
        self.assertEqual(report['label'], '需修复')
        self.assertEqual(report['next_action'], '按 QA 反馈重生成')
        self.assertIn('fail', {c['status'] for c in report['checks']})

    def test_workflow_status_tracks_ready_resume_loop(self):
        review = {
            'status': 'pass',
            'fill_ratio': 0.98,
            'page_count': 1,
            'screenshots': ['visual_review/page-1.png'],
            'agent_feedback': {'prompt': 'keep stable'},
        }
        report = _build_resume_quality_report(
            pdf_exists=True,
            visual_review=review,
            version_count=0,
        )

        workflow = _build_resume_workflow_status(
            pdf_exists=True,
            visual_review=review,
            quality_report=report,
            version_count=0,
            context={'engine': 'agent'},
        )

        self.assertEqual(workflow['status'], 'ready')
        self.assertEqual(workflow['label'], '可投递')
        self.assertGreaterEqual(workflow['progress'], 80)
        self.assertEqual(workflow['active_step']['key'], 'version')

    def test_version_snapshot_completes_ready_workflow(self):
        with tempfile.TemporaryDirectory(dir=_output_dir()) as tmp:
            out_dir = Path(tmp)
            (out_dir / 'resume-zh_CN.tex').write_text('resume body', encoding='utf-8')

            version = _create_version_snapshot(out_dir, fill_rate=98.0, pages=1, language='zh')
            review = {
                'status': 'pass',
                'fill_ratio': 0.98,
                'page_count': 1,
                'screenshots': ['visual_review/page-1.png'],
                'agent_feedback': {'prompt': 'keep stable'},
            }
            version_count = _get_version_count(out_dir)
            report = _build_resume_quality_report(
                pdf_exists=True,
                visual_review=review,
                version_count=version_count,
            )
            workflow = _build_resume_workflow_status(
                pdf_exists=True,
                visual_review=review,
                quality_report=report,
                version_count=version_count,
            )

            self.assertEqual(version, 1)
            self.assertEqual(version_count, 1)
            self.assertEqual(workflow['status'], 'complete')
            self.assertEqual(len(list((out_dir / 'versions').glob('v1_*.tex'))), 1)

    def test_manual_version_snapshot_refreshes_quality_payload(self):
        with tempfile.TemporaryDirectory(dir=_output_dir()) as tmp:
            out_dir = Path(tmp)
            (out_dir / 'resume-zh_CN.tex').write_text('resume body', encoding='utf-8')
            (out_dir / 'resume-zh_CN.pdf').write_bytes(b'%PDF-1.4\n')
            (out_dir / 'generation_context.json').write_text(
                '{"language":"zh","engine":"agent","fill_ratio":0.98}',
                encoding='utf-8',
            )
            review = {
                'status': 'pass',
                'fill_ratio': 0.98,
                'page_count': 1,
                'screenshots': ['visual_review/page-1.png'],
                'agent_feedback': {'prompt': 'keep stable'},
            }

            payload = _create_manual_version_snapshot(
                out_dir,
                note='质量报告确认版',
                visual_review=review,
            )

            self.assertTrue(payload['success'])
            self.assertEqual(payload['version'], 1)
            self.assertEqual(payload['version_count'], 1)
            self.assertEqual(payload['quality_report']['status'], 'ready')
            self.assertEqual(payload['workflow_status']['status'], 'complete')
            versions_path = out_dir / 'versions' / 'versions.json'
            versions = json.loads(versions_path.read_text(encoding='utf-8'))
            self.assertEqual(versions[0]['note'], '质量报告确认版')

    def test_workflow_status_marks_overflow_as_blocked(self):
        review = {
            'status': 'warning',
            'fill_ratio': 1.02,
            'page_count': 2,
            'issues': ['overflow', 'page_count_not_one'],
            'agent_feedback': {'prompt': 'compress'},
        }
        report = _build_resume_quality_report(
            pdf_exists=True,
            visual_review=review,
            version_count=1,
        )

        workflow = _build_resume_workflow_status(
            pdf_exists=True,
            visual_review=review,
            quality_report=report,
            version_count=1,
        )

        self.assertEqual(workflow['status'], 'blocked')
        self.assertEqual(workflow['active_step']['key'], 'visual_review')
        self.assertIn('修复', workflow['label'])

    def test_workflow_status_guides_missing_visual_review(self):
        report = _build_resume_quality_report(
            pdf_exists=True,
            visual_review={},
            version_count=0,
        )

        workflow = _build_resume_workflow_status(
            pdf_exists=True,
            visual_review={},
            quality_report=report,
            version_count=0,
        )

        self.assertEqual(workflow['status'], 'in_progress')
        self.assertEqual(workflow['active_step']['key'], 'visual_review')
        self.assertEqual(workflow['next_action'], '运行视觉 QA')

    def test_enrich_agent_job_payload_adds_quality_and_workflow(self):
        with tempfile.TemporaryDirectory(dir=_output_dir()) as tmp:
            out_dir = Path(tmp)
            (out_dir / 'resume-zh_CN.pdf').write_bytes(b'%PDF-1.4\n')
            (out_dir / 'generation_context.json').write_text(
                '{"language":"zh","engine":"agent","fill_ratio":0.98}',
                encoding='utf-8',
            )
            payload = {
                'status': 'completed',
                'task_summary': {
                    'agent': 'claude',
                    'company': '测试公司',
                    'role': '商品研究',
                    'language': 'zh',
                    'has_interview': True,
                    'has_selection_plan': True,
                    'has_feedback': False,
                },
                'result': {
                    'success': True,
                    'output_dir': out_dir.name,
                    'language': 'zh',
                    'visual_review': {
                        'status': 'pass',
                        'fill_ratio': 0.98,
                        'page_count': 1,
                        'screenshots': ['visual_review/page-1.png'],
                        'agent_feedback': {'prompt': 'keep stable'},
                    },
                },
            }

            enriched = _enrich_agent_job_payload(payload)

            result = enriched['result']
            self.assertEqual(result['quality_report']['status'], 'ready')
            self.assertIn(result['workflow_status']['status'], {'ready', 'complete'})
            self.assertEqual(result['workflow_status']['steps'][0]['key'], 'generate')
            self.assertEqual(enriched['user_status']['headline'], 'Agent 已生成结果')
            self.assertTrue(enriched['user_status']['artifacts']['has_pdf'])
            self.assertIn('测试公司', enriched['user_status']['input_summary'])
            self.assertNotIn('jd', json.dumps(enriched['user_status'], ensure_ascii=False).lower())

    def test_enrich_agent_job_payload_adds_failed_user_status_without_raw_input(self):
        payload = {
            'status': 'failed',
            'step': 'failed',
            'progress': 100,
            'error': '完整 JD 不应返回：LaTeX 编译失败',
            'task_summary': {
                'agent': 'codex',
                'company': '测试公司',
                'role': 'AI 产品经理',
                'language': 'zh',
                'has_interview': True,
                'has_selection_plan': False,
                'has_feedback': True,
            },
        }

        enriched = _enrich_agent_job_payload(payload)

        status = enriched['user_status']
        self.assertEqual(status['headline'], 'Agent 任务失败')
        self.assertEqual(status['action_kind'], 'retry')
        self.assertIn('测试公司', status['input_summary'])
        self.assertNotIn('完整 JD 不应返回', json.dumps(status, ensure_ascii=False))
        self.assertEqual(status['phases'][1]['status'], 'fail')

    def test_delivery_package_includes_artifacts_without_raw_context(self):
        with tempfile.TemporaryDirectory(dir=_output_dir()) as tmp:
            out_dir = Path(tmp)
            (out_dir / 'resume-zh_CN.pdf').write_bytes(b'%PDF-1.4\n')
            (out_dir / 'resume-zh_CN.tex').write_text('resume body', encoding='utf-8')
            (out_dir / 'resume.cls').write_text('class body', encoding='utf-8')
            (out_dir / 'generation_log.md').write_text('log body', encoding='utf-8')
            (out_dir / 'generation_context.json').write_text(
                json.dumps({
                    'company': '目标公司',
                    'role': '商品研究',
                    'language': 'zh',
                    'engine': 'agent',
                    'fill_ratio': 0.98,
                    'jd_text': '完整 JD 不应展示',
                    'interview_text': '面经正文不应展示',
                }, ensure_ascii=False),
                encoding='utf-8',
            )
            review_dir = out_dir / 'visual_review'
            review_dir.mkdir()
            (review_dir / 'visual_review.json').write_text(
                json.dumps({
                    'status': 'pass',
                    'fill_ratio': 0.98,
                    'page_count': 1,
                    'screenshots': ['visual_review/page-1.png'],
                    'agent_feedback': {'prompt': 'keep stable'},
                }, ensure_ascii=False),
                encoding='utf-8',
            )
            (review_dir / 'page-1.png').write_bytes(b'png')
            versions_dir = out_dir / 'versions'
            versions_dir.mkdir()
            (versions_dir / 'versions.json').write_text('[]', encoding='utf-8')

            content, filename, manifest = _build_delivery_package(out_dir)

            self.assertTrue(filename.endswith('_delivery.zip'))
            self.assertEqual(manifest['resume']['company'], '目标公司')
            self.assertEqual(manifest['quality']['status'], 'ready')
            with tempfile.NamedTemporaryFile(suffix='.zip') as handle:
                handle.write(content)
                handle.flush()
                with zipfile.ZipFile(handle.name) as zf:
                    names = zf.namelist()
                    joined_names = '\n'.join(names)
                    self.assertIn('delivery_manifest.json', joined_names)
                    self.assertIn('quality_report.json', joined_names)
                    self.assertIn('workflow_status.json', joined_names)
                    self.assertIn('resume-zh_CN.pdf', joined_names)
                    self.assertIn('resume-zh_CN.tex', joined_names)
                    self.assertIn('visual_review/visual_review.json', joined_names)
                    self.assertIn('visual_review/page-1.png', joined_names)
                    self.assertIn('versions/versions.json', joined_names)
                    self.assertNotIn('generation_context.json', joined_names)
                    manifest_name = next(name for name in names if name.endswith('/delivery_manifest.json'))
                    manifest_text = zf.read(manifest_name).decode('utf-8')
                    self.assertNotIn('完整 JD 不应展示', manifest_text)
                    self.assertNotIn('面经正文不应展示', manifest_text)

    def test_delivery_package_falls_back_to_directory_metadata(self):
        with tempfile.TemporaryDirectory(prefix='蚂蚁_产品经理-商业_20260310_', dir=_output_dir()) as tmp:
            out_dir = Path(tmp)
            (out_dir / 'resume-zh_CN.pdf').write_bytes(b'%PDF-1.4\n')
            (out_dir / 'resume-zh_CN.tex').write_text('resume body', encoding='utf-8')
            (out_dir / 'generation_context.json').write_text(
                json.dumps({
                    'language': 'zh',
                    'fill_ratio': 0.98,
                }, ensure_ascii=False),
                encoding='utf-8',
            )
            review_dir = out_dir / 'visual_review'
            review_dir.mkdir()
            (review_dir / 'visual_review.json').write_text(
                json.dumps({
                    'status': 'pass',
                    'fill_ratio': 0.98,
                    'page_count': 1,
                    'screenshots': ['visual_review/page-1.png'],
                    'agent_feedback': {'prompt': 'keep stable'},
                }, ensure_ascii=False),
                encoding='utf-8',
            )
            (review_dir / 'page-1.png').write_bytes(b'png')

            _, _, manifest = _build_delivery_package(out_dir)

            self.assertEqual(manifest['resume']['company'], '蚂蚁')
            self.assertEqual(manifest['resume']['role'], '产品经理-商业')
            self.assertEqual(manifest['resume']['generated_at'], '2026/03/10')


if __name__ == '__main__':
    unittest.main()
