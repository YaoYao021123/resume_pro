"""Tests for tools/language_utils.py, tools/gen_log.py, tools/model_config.py,
tools/migrate_to_multi_person.py, and tools/ext_db.py."""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════════════
# language_utils tests
# ═══════════════════════════════════════════════════════════════════

from tools.language_utils import normalize_language, resolve_resume_filenames, infer_language_from_output_dir


class NormalizeLanguageTests(unittest.TestCase):

    def test_none_returns_zh(self):
        self.assertEqual(normalize_language(None), 'zh')

    def test_empty_returns_zh(self):
        self.assertEqual(normalize_language(''), 'zh')

    def test_en_lowercase(self):
        self.assertEqual(normalize_language('en'), 'en')

    def test_EN_uppercase(self):
        self.assertEqual(normalize_language('EN'), 'en')

    def test_zh_passthrough(self):
        self.assertEqual(normalize_language('zh'), 'zh')

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            normalize_language('fr')


class ResolveResumeFilenamesTests(unittest.TestCase):

    def test_zh(self):
        tex, pdf = resolve_resume_filenames('zh')
        self.assertEqual(tex, 'resume-zh_CN.tex')
        self.assertEqual(pdf, 'resume-zh_CN.pdf')

    def test_en(self):
        tex, pdf = resolve_resume_filenames('en')
        self.assertEqual(tex, 'resume-en.tex')
        self.assertEqual(pdf, 'resume-en.pdf')

    def test_none_defaults_zh(self):
        tex, pdf = resolve_resume_filenames(None)
        self.assertIn('zh_CN', tex)


class InferLanguageFromOutputDirTests(unittest.TestCase):

    def test_context_json(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / 'generation_context.json').write_text('{"language": "en"}')
            self.assertEqual(infer_language_from_output_dir(d), 'en')

    def test_en_tex_exists(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / 'resume-en.tex').write_text('hello')
            self.assertEqual(infer_language_from_output_dir(d), 'en')

    def test_zh_tex_exists(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / 'resume-zh_CN.tex').write_text('hello')
            self.assertEqual(infer_language_from_output_dir(d), 'zh')

    def test_fallback_zh(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(infer_language_from_output_dir(Path(td)), 'zh')

    def test_malformed_json_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / 'generation_context.json').write_text('{bad json')
            # Should not raise, should fall back
            result = infer_language_from_output_dir(d)
            self.assertEqual(result, 'zh')


# ═══════════════════════════════════════════════════════════════════
# gen_log tests
# ═══════════════════════════════════════════════════════════════════

from tools import gen_log


class GenLogTests(unittest.TestCase):

    def setUp(self):
        gen_log.clear()

    def test_emit_returns_incrementing_seq(self):
        s1 = gen_log.emit('step', 'hello')
        s2 = gen_log.emit('step', 'world')
        self.assertEqual(s2, s1 + 1)

    def test_emit_with_data(self):
        gen_log.emit('step', 'test', data={'key': 'val'})
        entries = gen_log.get_all()
        self.assertEqual(entries[-1]['data'], {'key': 'val'})

    def test_get_entries_since_filters(self):
        s1 = gen_log.emit('a', '1')
        s2 = gen_log.emit('b', '2')
        s3 = gen_log.emit('c', '3')
        result = gen_log.get_entries_since(s1)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['text'], '2')

    def test_get_all(self):
        gen_log.emit('a', '1')
        gen_log.emit('b', '2')
        self.assertEqual(len(gen_log.get_all()), 2)

    def test_clear_resets(self):
        gen_log.emit('a', '1')
        gen_log.clear()
        self.assertEqual(len(gen_log.get_all()), 0)
        s = gen_log.emit('b', '2')
        self.assertEqual(s, 1)  # seq reset to 0, then +1

    def test_thread_safety(self):
        """Concurrent emits should not lose entries."""
        gen_log.clear()
        n = 100

        def worker():
            for _ in range(n):
                gen_log.emit('thread', 'x')

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(gen_log.get_all()), 4 * n)


# ═══════════════════════════════════════════════════════════════════
# model_config tests (pure functions)
# ═══════════════════════════════════════════════════════════════════

from tools.model_config import (
    _strip_wrapping_quotes,
    _parse_env_file,
    _env_flag,
    _quote_env_value,
    get_provider_presets,
)


class StripWrappingQuotesTests(unittest.TestCase):

    def test_single_quotes(self):
        self.assertEqual(_strip_wrapping_quotes("'hello'"), 'hello')

    def test_double_quotes(self):
        self.assertEqual(_strip_wrapping_quotes('"hello"'), 'hello')

    def test_mismatched_unchanged(self):
        self.assertEqual(_strip_wrapping_quotes("'hello\""), "'hello\"")

    def test_no_quotes(self):
        self.assertEqual(_strip_wrapping_quotes('hello'), 'hello')

    def test_short_string(self):
        self.assertEqual(_strip_wrapping_quotes('a'), 'a')

    def test_empty_quotes(self):
        self.assertEqual(_strip_wrapping_quotes('""'), '')


class ParseEnvFileTests(unittest.TestCase):

    def test_basic_key_value(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write('KEY=value\nOTHER="quoted"\n')
            f.flush()
            result = _parse_env_file(Path(f.name))
        os.unlink(f.name)
        self.assertEqual(result['KEY'], 'value')
        self.assertEqual(result['OTHER'], 'quoted')

    def test_comments_and_empty_lines_skipped(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write('# comment\n\nKEY=val\n')
            f.flush()
            result = _parse_env_file(Path(f.name))
        os.unlink(f.name)
        self.assertEqual(len(result), 1)
        self.assertEqual(result['KEY'], 'val')

    def test_nonexistent_returns_empty(self):
        self.assertEqual(_parse_env_file(Path('/nonexistent.env')), {})


class EnvFlagTests(unittest.TestCase):

    def test_truthy_values(self):
        for val in ('1', 'true', 'yes', 'on', 'TRUE', 'Yes', 'ON'):
            with mock.patch.dict(os.environ, {'TEST_FLAG': val}):
                self.assertTrue(_env_flag('TEST_FLAG'), f'Expected truthy for {val}')

    def test_falsy_values(self):
        for val in ('0', 'false', 'no', 'off', '', 'maybe'):
            with mock.patch.dict(os.environ, {'TEST_FLAG': val}):
                self.assertFalse(_env_flag('TEST_FLAG'), f'Expected falsy for {val}')

    def test_missing_key(self):
        env = os.environ.copy()
        env.pop('TEST_FLAG_MISSING', None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(_env_flag('TEST_FLAG_MISSING'))


class QuoteEnvValueTests(unittest.TestCase):

    def test_empty_string(self):
        self.assertEqual(_quote_env_value(''), '""')

    def test_spaces_trigger_quoting(self):
        result = _quote_env_value('hello world')
        self.assertTrue(result.startswith('"'))

    def test_hash_trigger_quoting(self):
        result = _quote_env_value('val#comment')
        self.assertTrue(result.startswith('"'))

    def test_simple_value_no_quoting(self):
        self.assertEqual(_quote_env_value('simple'), 'simple')


class GetProviderPresetsTests(unittest.TestCase):

    def test_returns_all_providers(self):
        presets = get_provider_presets()
        ids = {p['id'] for p in presets}
        self.assertIn('openai', ids)
        self.assertIn('gemini', ids)
        self.assertIn('anthropic', ids)
        self.assertIn('other', ids)
        self.assertGreaterEqual(len(presets), 10)


# ═══════════════════════════════════════════════════════════════════
# migrate_to_multi_person tests
# ═══════════════════════════════════════════════════════════════════

from tools.migrate_to_multi_person import _extract_name_from_profile


class ExtractNameFromProfileTests(unittest.TestCase):

    def test_valid_name(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write('# Profile\n姓名（中文）：张三\n')
            f.flush()
            result = _extract_name_from_profile(Path(f.name))
        os.unlink(f.name)
        self.assertEqual(result, '张三')

    def test_placeholder_returns_default(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write('姓名（中文）：[YOUR_NAME]\n')
            f.flush()
            result = _extract_name_from_profile(Path(f.name))
        os.unlink(f.name)
        self.assertEqual(result, '默认')

    def test_missing_file_returns_default(self):
        result = _extract_name_from_profile(Path('/nonexistent/profile.md'))
        self.assertEqual(result, '默认')


# ═══════════════════════════════════════════════════════════════════
# ext_db tests (with in-memory SQLite)
# ═══════════════════════════════════════════════════════════════════

class ExtDbTests(unittest.TestCase):
    """Test ext_db functions with a temporary database file."""

    def setUp(self):
        self._tmpdb = tempfile.mktemp(suffix='.db')
        # Patch DB_PATH before calling any ext_db function
        import tools.ext_db as ext_db_mod
        self._orig_db_path = ext_db_mod.DB_PATH
        ext_db_mod.DB_PATH = Path(self._tmpdb)
        ext_db_mod.init_db()

    def tearDown(self):
        import tools.ext_db as ext_db_mod
        ext_db_mod.DB_PATH = self._orig_db_path
        try:
            os.unlink(self._tmpdb)
        except OSError:
            pass

    def test_init_db_creates_tables(self):
        conn = sqlite3.connect(self._tmpdb)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        conn.close()
        table_names = {t[0] for t in tables}
        self.assertIn('fill_history', table_names)
        self.assertIn('corrections', table_names)
        self.assertIn('field_mappings', table_names)

    def test_log_fill_returns_id(self):
        from tools.ext_db import log_fill
        fill_id = log_fill('https://example.com', 'workday', 5)
        self.assertIsInstance(fill_id, int)
        self.assertGreater(fill_id, 0)

    def test_log_correction_updates_count(self):
        from tools.ext_db import log_fill, log_correction, get_fill_history
        fill_id = log_fill('https://example.com', 'workday', 5)
        log_correction(fill_id, 'name', 'Name', 'old', 'new', 'workday')
        log_correction(fill_id, 'email', 'Email', 'old', 'new', 'workday')
        history = get_fill_history(1)
        self.assertEqual(history[0]['fields_corrected'], 2)

    def test_field_mapping_upsert(self):
        from tools.ext_db import update_field_mapping, get_field_mappings
        update_field_mapping('workday', '#name', 'Name', 'name_zh', 0.8)
        update_field_mapping('workday', '#name', 'Name', 'name_zh', 0.9)
        mappings = get_field_mappings('workday')
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]['use_count'], 2)
        self.assertAlmostEqual(mappings[0]['confidence'], 0.9)

    def test_corrections_summary_empty(self):
        from tools.ext_db import get_corrections_summary
        result = get_corrections_summary()
        self.assertIn('recent_corrections', result)
        self.assertIn('field_stats', result)
        self.assertIn('fill_stats', result)
        self.assertEqual(len(result['recent_corrections']), 0)


if __name__ == '__main__':
    unittest.main()
