"""Tests for pure functions in tools/generate_resume.py.

All tests here exercise functions that are pure or near-pure (no filesystem,
no network, no subprocess).  They validate the core business rules:
experience classification, bullet sanitization, award filtering, keyword
extraction, and selection caps.
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.generate_resume import (
    tex_escape,
    _to_year_month,
    _to_year_month_range,
    _localize_date_text,
    _classify_experience,
    _time_sort_key,
    _sanitize_bullet,
    _normalize_quotes,
    _auto_add_title,
    _bullet_quality_score,
    _select_best_bullets,
    _render_bullet_latex,
    _filter_awards,
    _award_group_key,
    _award_score,
    _filter_projects,
    _score_project,
    _apply_experience_selection_rules,
    _keywords_set,
    _merge_ai_keywords,
    _normalize_work_material_name,
    _truncate,
    _extract_json_text,
    _is_token_limit_error,
    _is_json_format_unsupported,
    _is_thinking_unsupported,
    _api_key_fingerprint,
    _mask_api_key,
    _redact_text_with_ai_config,
    extract_jd_keywords,
    match_experiences,
)


# ═══════════════════════════════════════════════════════════════════
# tex_escape
# ═══════════════════════════════════════════════════════════════════

class TexEscapeTests(unittest.TestCase):

    def test_ampersand(self):
        self.assertEqual(tex_escape('A&B'), r'A\&B')

    def test_percent(self):
        self.assertEqual(tex_escape('100%'), r'100\%')

    def test_dollar(self):
        self.assertEqual(tex_escape('$100'), r'\$100')

    def test_hash(self):
        self.assertEqual(tex_escape('#1'), r'\#1')

    def test_underscore(self):
        self.assertEqual(tex_escape('a_b'), r'a\_b')

    def test_braces(self):
        self.assertEqual(tex_escape('{x}'), r'\{x\}')

    def test_tilde(self):
        self.assertIn('textasciitilde', tex_escape('~'))

    def test_caret(self):
        self.assertIn('textasciicircum', tex_escape('^'))

    def test_empty_string(self):
        self.assertEqual(tex_escape(''), '')

    def test_no_special_chars(self):
        self.assertEqual(tex_escape('hello world'), 'hello world')

    def test_multiple_specials(self):
        result = tex_escape('A & B % C')
        self.assertIn(r'\&', result)
        self.assertIn(r'\%', result)


# ═══════════════════════════════════════════════════════════════════
# _to_year_month
# ═══════════════════════════════════════════════════════════════════

class ToYearMonthTests(unittest.TestCase):

    def test_slash_format(self):
        self.assertEqual(_to_year_month('2024/06'), '2024/06')

    def test_dash_format(self):
        self.assertEqual(_to_year_month('2024-06'), '2024/06')

    def test_chinese_year_format(self):
        self.assertEqual(_to_year_month('2024年06月'), '2024/06')

    def test_present_variants(self):
        self.assertEqual(_to_year_month('至今'), '至今')
        self.assertEqual(_to_year_month('Present'), '至今')
        self.assertEqual(_to_year_month('current'), '至今')

    def test_empty_string(self):
        self.assertEqual(_to_year_month(''), '')
        self.assertEqual(_to_year_month(None), '')

    def test_month_clamping_high(self):
        self.assertEqual(_to_year_month('2024/13'), '2024/12')

    def test_month_clamping_low(self):
        self.assertEqual(_to_year_month('2024/0'), '2024/01')

    def test_with_day(self):
        self.assertEqual(_to_year_month('2024/06/15'), '2024/06')


# ═══════════════════════════════════════════════════════════════════
# _to_year_month_range
# ═══════════════════════════════════════════════════════════════════

class ToYearMonthRangeTests(unittest.TestCase):

    def test_double_dash(self):
        self.assertEqual(_to_year_month_range('2024/01 -- 2024/06'), '2024/01 -- 2024/06')

    def test_em_dash(self):
        self.assertEqual(_to_year_month_range('2024/01—2024/06'), '2024/01 -- 2024/06')

    def test_en_dash(self):
        self.assertEqual(_to_year_month_range('2024/01–2024/06'), '2024/01 -- 2024/06')

    def test_single_date(self):
        self.assertEqual(_to_year_month_range('2024/06'), '2024/06')

    def test_empty(self):
        self.assertEqual(_to_year_month_range(''), '')
        self.assertEqual(_to_year_month_range(None), '')

    def test_present_end(self):
        result = _to_year_month_range('2024/01 -- 至今')
        self.assertIn('至今', result)


# ═══════════════════════════════════════════════════════════════════
# _localize_date_text
# ═══════════════════════════════════════════════════════════════════

class LocalizeDateTextTests(unittest.TestCase):

    def test_en_replaces_zhijin(self):
        self.assertEqual(_localize_date_text('2024/01 -- 至今', 'en'), '2024/01 -- Present')

    def test_zh_keeps_zhijin(self):
        self.assertEqual(_localize_date_text('2024/01 -- 至今', 'zh'), '2024/01 -- 至今')

    def test_none_input(self):
        self.assertEqual(_localize_date_text(None, 'zh'), '')


# ═══════════════════════════════════════════════════════════════════
# _classify_experience
# ═══════════════════════════════════════════════════════════════════

class ClassifyExperienceTests(unittest.TestCase):

    def test_research_by_filename_prefix(self):
        exp = {'filename': '研究_产业结构.md', 'tags': '经济学'}
        self.assertEqual(_classify_experience(exp), 'research')

    def test_research_by_both_tags(self):
        exp = {'filename': '01_大学课题.md', 'tags': '研究, 学术, 经济学'}
        self.assertEqual(_classify_experience(exp), 'research')

    def test_intern_default(self):
        exp = {'filename': '01_百度.md', 'tags': '数据分析, Python'}
        self.assertEqual(_classify_experience(exp), 'intern')

    def test_only_研究_tag_without_学术_is_intern(self):
        exp = {'filename': '01_公司.md', 'tags': '研究, 数据分析'}
        self.assertEqual(_classify_experience(exp), 'intern')

    def test_empty_exp(self):
        self.assertEqual(_classify_experience({}), 'intern')


# ═══════════════════════════════════════════════════════════════════
# _time_sort_key
# ═══════════════════════════════════════════════════════════════════

class TimeSortKeyTests(unittest.TestCase):

    def test_standard(self):
        self.assertEqual(_time_sort_key('2024/06'), (2024, 6))

    def test_dash_format(self):
        self.assertEqual(_time_sort_key('2024-06'), (2024, 6))

    def test_empty(self):
        self.assertEqual(_time_sort_key(''), (0, 0))
        self.assertEqual(_time_sort_key(None), (0, 0))


# ═══════════════════════════════════════════════════════════════════
# _sanitize_bullet / _normalize_quotes
# ═══════════════════════════════════════════════════════════════════

class SanitizeBulletTests(unittest.TestCase):

    def test_strips_markers(self):
        self.assertEqual(_sanitize_bullet('- Hello World'), 'Hello World')
        self.assertEqual(_sanitize_bullet('* Test'), 'Test')
        self.assertEqual(_sanitize_bullet('1. Item'), 'Item')
        self.assertEqual(_sanitize_bullet('• Bullet'), 'Bullet')

    def test_removes_trailing_chinese_period(self):
        self.assertFalse(_sanitize_bullet('分析数据。').endswith('。'))

    def test_removes_trailing_english_period(self):
        self.assertFalse(_sanitize_bullet('analyze data.').endswith('.'))

    def test_removes_trailing_semicolons(self):
        result = _sanitize_bullet('test；')
        self.assertFalse(result.endswith('；'))

    def test_normalizes_whitespace(self):
        self.assertEqual(_sanitize_bullet('hello   world'), 'hello world')

    def test_empty(self):
        self.assertEqual(_sanitize_bullet(''), '')
        self.assertEqual(_sanitize_bullet(None), '')


class NormalizeQuotesTests(unittest.TestCase):

    def test_german_lower_quote(self):
        result = _normalize_quotes('\u201ehello\u201d')
        self.assertNotIn('\u201e', result)  # „ should be gone
        self.assertIn('\u201c', result)  # replaced with left curly "

    def test_alternating_open_close(self):
        result = _normalize_quotes('说"你好"吧')
        self.assertIn('\u201c', result)  # left
        self.assertIn('\u201d', result)  # right

    def test_fullwidth_replaced(self):
        result = _normalize_quotes('＂test＂')
        self.assertNotIn('＂', result)


# ═══════════════════════════════════════════════════════════════════
# _auto_add_title
# ═══════════════════════════════════════════════════════════════════

class AutoAddTitleTests(unittest.TestCase):

    def test_already_has_title(self):
        bullet = 'AI产品方案设计：基于SDK实现功能'
        result = _auto_add_title(bullet)
        self.assertEqual(result, bullet)

    def test_derives_from_comma_split(self):
        bullet = '竞品分析，研究15家公司的AI产品并输出报告'
        result = _auto_add_title(bullet)
        self.assertIn('：', result)

    def test_derives_from_known_verb(self):
        bullet = '独立搭建六维研究框架，完成分析报告'
        result = _auto_add_title(bullet)
        self.assertIn('：', result)
        # Title should be 4 CJK chars (verb + noun)
        title = result.split('：')[0]
        self.assertEqual(len(title), 4)

    def test_empty_bullet(self):
        self.assertEqual(_auto_add_title(''), '')


# ═══════════════════════════════════════════════════════════════════
# _bullet_quality_score
# ═══════════════════════════════════════════════════════════════════

class BulletQualityScoreTests(unittest.TestCase):

    def test_high_score_with_title_numbers_tech(self):
        bullet = 'AI产品策略规划：分析H1 Top50客户数据支撑版本迭代，使用Python处理10万条记录'
        score = _bullet_quality_score(bullet)
        self.assertGreater(score, 5)

    def test_negative_for_generic_title(self):
        bullet = '方案设计：制定了相关方案'
        score = _bullet_quality_score(bullet)
        # Generic title penalty
        self.assertLess(score, 2)

    def test_penalty_for_负责_opening(self):
        bullet = '负责数据分析工作，处理日常数据需求'
        score_bad = _bullet_quality_score(bullet)
        bullet2 = '数据分析与建模：搭建分析模型覆盖100+用户场景'
        score_good = _bullet_quality_score(bullet2)
        self.assertGreater(score_good, score_bad)

    def test_empty_bullet_very_low(self):
        self.assertEqual(_bullet_quality_score(''), -99)


# ═══════════════════════════════════════════════════════════════════
# _select_best_bullets
# ═══════════════════════════════════════════════════════════════════

class SelectBestBulletsTests(unittest.TestCase):

    def test_ai_preferred_with_bonus(self):
        ai = ['AI产品方案设计：使用SDK实现Agent功能并推动100个用户采纳']
        fb = ['负责日常工作']
        result = _select_best_bullets(ai, fb, min_count=1, max_count=2)
        self.assertEqual(result[0], _sanitize_bullet(ai[0]))

    def test_fills_to_min_count(self):
        ai = []
        fb = ['数据分析报告撰写', '日常运营支持']
        result = _select_best_bullets(ai, fb, min_count=2, max_count=4)
        self.assertGreaterEqual(len(result), 2)

    def test_caps_at_max_count(self):
        ai = ['a' * 20, 'b' * 20, 'c' * 20, 'd' * 20, 'e' * 20]
        fb = ['f' * 20]
        result = _select_best_bullets(ai, fb, min_count=1, max_count=3)
        self.assertLessEqual(len(result), 3)

    def test_empty_inputs(self):
        result = _select_best_bullets([], [], min_count=2, max_count=4)
        self.assertEqual(result, [])

    def test_deduplication(self):
        bullet = '数据清洗与分析'
        result = _select_best_bullets([bullet], [bullet], min_count=1, max_count=4)
        self.assertEqual(len(result), 1)


# ═══════════════════════════════════════════════════════════════════
# _render_bullet_latex
# ═══════════════════════════════════════════════════════════════════

class RenderBulletLatexTests(unittest.TestCase):

    def test_with_title(self):
        result = _render_bullet_latex('AI产品方案设计：基于SDK实现功能')
        self.assertIn(r'\item', result)
        self.assertIn(r'\textbf{', result)

    def test_without_title(self):
        result = _render_bullet_latex('short')
        self.assertIn(r'\item', result)

    def test_escapes_special_chars(self):
        result = _render_bullet_latex('A&B：100% done')
        self.assertIn(r'\&', result)
        self.assertIn(r'\%', result)


# ═══════════════════════════════════════════════════════════════════
# extract_jd_keywords
# ═══════════════════════════════════════════════════════════════════

class ExtractJdKeywordsTests(unittest.TestCase):

    def test_tech_keywords(self):
        jd = '要求熟悉 Python, SQL，了解 Pandas 和数据分析'
        result = extract_jd_keywords(jd)
        self.assertIn('python', result['tech'])
        self.assertIn('sql', result['tech'])

    def test_domain_keywords(self):
        jd = '负责搜索和推荐系统的产品运营'
        result = extract_jd_keywords(jd)
        self.assertTrue(any(k in result['domain'] for k in ['搜索', '推荐', '产品', '运营']))

    def test_company_role_from_prefix(self):
        jd = '公司：美团\n岗位：数据分析师\n职责：...'
        result = extract_jd_keywords(jd)
        self.assertEqual(result['company'], '美团')
        self.assertEqual(result['role'], '数据分析师')

    def test_company_role_from_separator(self):
        jd = '美团 - 数据分析师\n负责数据分析工作'
        result = extract_jd_keywords(jd)
        self.assertEqual(result['company'], '美团')
        self.assertEqual(result['role'], '数据分析师')

    def test_empty_jd(self):
        result = extract_jd_keywords('')
        self.assertEqual(result['tech'], [])
        self.assertEqual(result['domain'], [])
        self.assertEqual(result['company'], '')
        self.assertEqual(result['role'], '')

    def test_case_insensitive(self):
        result = extract_jd_keywords('PYTHON and SQL skills')
        self.assertIn('python', result['tech'])
        self.assertIn('sql', result['tech'])


# ═══════════════════════════════════════════════════════════════════
# _filter_awards
# ═══════════════════════════════════════════════════════════════════

class FilterAwardsTests(unittest.TestCase):

    def _make_profile(self, awards):
        return {'awards': awards}

    def test_max_3(self):
        awards = [
            {'name': f'奖项{i}', 'org': '机构', 'time': '2024'} for i in range(10)
        ]
        result = _filter_awards(self._make_profile(awards), {'tech': []})
        self.assertLessEqual(len(result), 3)

    def test_dedupes_scholarship_group(self):
        awards = [
            {'name': '一等奖学金', 'org': '大学', 'time': '2024'},
            {'name': '二等奖学金', 'org': '大学', 'time': '2023'},
        ]
        result = _filter_awards(self._make_profile(awards), {'tech': []})
        # Should keep only the higher-scored one
        self.assertEqual(len(result), 1)
        self.assertIn('一等', result[0]['name'])

    def test_jd_relevant_prioritized(self):
        awards = [
            {'name': '数学建模大赛一等奖', 'org': 'COMAP', 'time': '2024'},
            {'name': '公益志愿者奖', 'org': '学校', 'time': '2024'},
            {'name': '优秀学生奖', 'org': '学校', 'time': '2024'},
        ]
        jd_keywords = {'tech': ['数据分析', '建模'], 'domain': [], 'skill': []}
        result = _filter_awards(self._make_profile(awards), jd_keywords)
        # Math modeling should be first
        self.assertIn('数学建模', result[0]['name'])

    def test_empty_awards(self):
        result = _filter_awards({'awards': []}, {'tech': []})
        self.assertEqual(result, [])

    def test_internal_score_stripped(self):
        awards = [{'name': '奖项A', 'org': '机构', 'time': '2024'}]
        result = _filter_awards(self._make_profile(awards), {'tech': []})
        self.assertNotIn('_score', result[0])


# ═══════════════════════════════════════════════════════════════════
# _filter_projects
# ═══════════════════════════════════════════════════════════════════

class FilterProjectsTests(unittest.TestCase):

    def test_remaining_slots_zero(self):
        result = _filter_projects({'projects': [{'name': 'P1', 'desc': 'test'}]}, {'tech': ['python']}, remaining_slots=0)
        self.assertEqual(result, [])

    def test_max_2(self):
        projects = [{'name': f'P{i}', 'desc': 'python data analysis', 'tags': 'python', 'role': 'dev', 'time': '2024'} for i in range(5)]
        result = _filter_projects({'projects': projects}, {'tech': ['python']})
        self.assertLessEqual(len(result), 2)


# ═══════════════════════════════════════════════════════════════════
# _apply_experience_selection_rules
# ═══════════════════════════════════════════════════════════════════

class ApplyExperienceSelectionRulesTests(unittest.TestCase):

    def _make_exp(self, filename, tags='数据分析', time_start='2024/06', classification='intern'):
        prefix = '研究_' if classification == 'research' else ''
        tag_str = tags
        if classification == 'research':
            tag_str = '研究, 学术, ' + tags
        return {
            'filename': prefix + filename,
            'tags': tag_str,
            'company': filename.replace('.md', ''),
            'time_start': time_start,
            'time_end': '至今',
            'city': '北京',
            'department': '部门',
            'role': '实习生',
            'notes': '做了数据分析工作，处理了100条数据，产出报告',
            'work_items': [{'title': '数据分析', 'desc': '完成数据清洗与分析工作'}],
        }

    def test_total_max_5(self):
        exps = [self._make_exp(f'{i}_公司.md', time_start=f'202{i}/01') for i in range(8)]
        result = _apply_experience_selection_rules(exps, {'tech': ['数据分析'], 'domain': [], 'skill': []})
        self.assertLessEqual(len(result), 5)

    def test_intern_max_4(self):
        exps = [self._make_exp(f'{i}_公司.md', time_start=f'202{i}/01') for i in range(6)]
        result = _apply_experience_selection_rules(exps, {'tech': [], 'domain': [], 'skill': []})
        intern_count = sum(1 for e in result if _classify_experience(e) == 'intern')
        self.assertLessEqual(intern_count, 4)

    def test_research_max_2(self):
        exps = [self._make_exp(f'{i}_课题.md', classification='research', time_start=f'202{i}/01') for i in range(5)]
        result = _apply_experience_selection_rules(exps, {'tech': [], 'domain': [], 'skill': []})
        research_count = sum(1 for e in result if _classify_experience(e) == 'research')
        self.assertLessEqual(research_count, 2)

    def test_backfills_to_minimum_3(self):
        exps = [self._make_exp(f'{i}_公司.md', time_start=f'202{i}/01') for i in range(5)]
        result = _apply_experience_selection_rules(exps, {'tech': [], 'domain': [], 'skill': []})
        self.assertGreaterEqual(len(result), 3)

    def test_dedup_across_categories(self):
        exps = [self._make_exp('01_公司.md')]
        result = _apply_experience_selection_rules(exps, {'tech': [], 'domain': [], 'skill': []})
        filenames = [e['filename'] for e in result]
        self.assertEqual(len(filenames), len(set(filenames)))

    def test_time_sort_descending(self):
        exps = [
            self._make_exp('01_old.md', time_start='2022/01'),
            self._make_exp('02_new.md', time_start='2024/06'),
            self._make_exp('03_mid.md', time_start='2023/06'),
        ]
        result = _apply_experience_selection_rules(exps, {'tech': ['数据分析'], 'domain': [], 'skill': []})
        # Within each category, should be time-descending
        intern_results = [e for e in result if _classify_experience(e) == 'intern']
        for i in range(len(intern_results) - 1):
            t1 = _time_sort_key(intern_results[i].get('time_start', ''))
            t2 = _time_sort_key(intern_results[i + 1].get('time_start', ''))
            self.assertGreaterEqual(t1, t2)


# ═══════════════════════════════════════════════════════════════════
# Keyword helpers
# ═══════════════════════════════════════════════════════════════════

class KeywordsSetTests(unittest.TestCase):

    def test_flattens_and_lowercases(self):
        jd = {'tech': ['Python', 'SQL'], 'domain': ['金融'], 'skill': ['沟通']}
        result = _keywords_set(jd)
        self.assertIn('python', result)
        self.assertIn('sql', result)
        self.assertIn('金融', result)

    def test_empty(self):
        self.assertEqual(_keywords_set({}), set())


class MergeAiKeywordsTests(unittest.TestCase):

    def test_merges_hard_skills_to_tech(self):
        base = {'tech': ['python'], 'domain': [], 'skill': []}
        ai = {'hard_skills': ['sql'], 'functions': ['产品']}
        result = _merge_ai_keywords(base, ai)
        self.assertIn('sql', result['tech'])
        self.assertIn('产品', result['domain'])

    def test_no_duplicates(self):
        base = {'tech': ['python'], 'domain': [], 'skill': []}
        ai = {'hard_skills': ['python']}
        result = _merge_ai_keywords(base, ai)
        self.assertEqual(result['tech'].count('python'), 1)


# ═══════════════════════════════════════════════════════════════════
# Utility helpers
# ═══════════════════════════════════════════════════════════════════

class NormalizeWorkMaterialNameTests(unittest.TestCase):

    def test_strips_parens(self):
        self.assertEqual(_normalize_work_material_name('公司（北京）'), '公司')

    def test_lowercases(self):
        self.assertEqual(_normalize_work_material_name('ABC'), 'abc')

    def test_removes_whitespace(self):
        self.assertNotIn(' ', _normalize_work_material_name('a b c'))


class TruncateTests(unittest.TestCase):

    def test_within_limit_unchanged(self):
        text = 'hello'
        self.assertEqual(_truncate(text, 100), text)

    def test_over_limit_has_marker(self):
        text = 'x' * 200
        result = _truncate(text, 50, 'test')
        self.assertIn('截断', result)
        self.assertIn('test', result)

    def test_label_optional(self):
        text = 'x' * 200
        result = _truncate(text, 50)
        self.assertIn('截断', result)


class ExtractJsonTextTests(unittest.TestCase):

    def test_plain_json(self):
        result = _extract_json_text('{"key": "value"}')
        self.assertEqual(result, {'key': 'value'})

    def test_markdown_fenced(self):
        result = _extract_json_text('```json\n{"key": "value"}\n```')
        self.assertEqual(result, {'key': 'value'})

    def test_embedded_in_text(self):
        result = _extract_json_text('Here is the result: {"key": "value"} done.')
        self.assertEqual(result, {'key': 'value'})

    def test_empty_raises(self):
        with self.assertRaises(RuntimeError):
            _extract_json_text('')

    def test_invalid_json_raises(self):
        with self.assertRaises(RuntimeError):
            _extract_json_text('not json at all')


# ═══════════════════════════════════════════════════════════════════
# Error detection helpers
# ═══════════════════════════════════════════════════════════════════

class ErrorDetectionTests(unittest.TestCase):

    def test_token_limit_error(self):
        self.assertTrue(_is_token_limit_error('max message tokens exceeded'))
        self.assertTrue(_is_token_limit_error('context length too long'))
        self.assertFalse(_is_token_limit_error('normal error'))

    def test_json_format_unsupported(self):
        self.assertTrue(_is_json_format_unsupported('json_object is not supported'))
        self.assertTrue(_is_json_format_unsupported('response_format is not valid'))
        self.assertFalse(_is_json_format_unsupported('normal error'))

    def test_thinking_unsupported(self):
        self.assertTrue(_is_thinking_unsupported('thinking is not supported'))
        self.assertTrue(_is_thinking_unsupported('thinking: unknown parameter'))
        self.assertFalse(_is_thinking_unsupported('normal error'))


# ═══════════════════════════════════════════════════════════════════
# API key helpers
# ═══════════════════════════════════════════════════════════════════

class ApiKeyHelpersTests(unittest.TestCase):

    def test_fingerprint_length_12(self):
        result = _api_key_fingerprint('sk-test-key-12345')
        self.assertEqual(len(result), 12)

    def test_fingerprint_none(self):
        self.assertIsNone(_api_key_fingerprint(None))
        self.assertIsNone(_api_key_fingerprint(''))

    def test_mask_normal_key(self):
        result = _mask_api_key('sk-1234567890abcdef')
        self.assertTrue(result.startswith('sk-1'))
        self.assertTrue(result.endswith('cdef'))
        self.assertIn('*', result)

    def test_mask_short_key(self):
        result = _mask_api_key('short')
        self.assertEqual(result, '*****')

    def test_mask_none(self):
        self.assertIsNone(_mask_api_key(None))

    def test_redact_replaces_key(self):
        config = {'api_key': 'sk-secret-key-value'}
        text = 'Error with key sk-secret-key-value failed'
        result = _redact_text_with_ai_config(text, config)
        self.assertNotIn('sk-secret-key-value', result)
        self.assertIn('REDACTED', result)


# ═══════════════════════════════════════════════════════════════════
# match_experiences
# ═══════════════════════════════════════════════════════════════════

class MatchExperiencesTests(unittest.TestCase):

    def _make_exp(self, name, tags):
        return {'filename': name, 'tags': tags, 'company': name, 'notes': '', 'work_items': []}

    def test_scores_by_keyword_overlap(self):
        exps = [
            self._make_exp('A.md', 'python, sql, 数据分析'),
            self._make_exp('B.md', '销售, 客户管理'),
        ]
        jd = {'tech': ['python', 'sql'], 'domain': ['数据分析'], 'skill': []}
        result = match_experiences(exps, jd, max_count=2)
        self.assertEqual(result[0]['filename'], 'A.md')

    def test_fills_to_2_minimum(self):
        exps = [
            self._make_exp('A.md', 'unrelated'),
            self._make_exp('B.md', 'unrelated'),
        ]
        result = match_experiences(exps, {'tech': ['python'], 'domain': [], 'skill': []}, max_count=5)
        self.assertGreaterEqual(len(result), 2)


# ═══════════════════════════════════════════════════════════════════
# _award_group_key / _award_score
# ═══════════════════════════════════════════════════════════════════

class AwardGroupKeyTests(unittest.TestCase):

    def test_scholarship_groups(self):
        key1 = _award_group_key('一等奖学金')
        key2 = _award_group_key('二等奖学金')
        self.assertEqual(key1, key2)

    def test_non_scholarship_unchanged(self):
        self.assertEqual(_award_group_key('数学建模大赛'), '数学建模大赛')


class AwardScoreTests(unittest.TestCase):

    def test_high_value_tokens_boost(self):
        award = {'name': '全国数学建模大赛一等奖', 'org': 'COMAP'}
        score = _award_score(award, {'tech': [], 'domain': [], 'skill': []}, {})
        self.assertGreater(score, 0)

    def test_low_value_tokens_penalize(self):
        award = {'name': '公益三等奖', 'org': '学校'}
        score = _award_score(award, {'tech': [], 'domain': [], 'skill': []}, {})
        self.assertLess(score, 0)

    def test_preferred_order_boosts(self):
        award = {'name': '特定奖', 'org': '机构'}
        score_preferred = _award_score(award, {'tech': [], 'domain': [], 'skill': []}, {'特定奖': 0})
        score_normal = _award_score(award, {'tech': [], 'domain': [], 'skill': []}, {})
        self.assertGreater(score_preferred, score_normal)


if __name__ == '__main__':
    unittest.main()
