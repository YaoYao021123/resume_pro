"""Tests for tools/page_fill_check.py — pure functions only (no xelatex needed)."""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.page_fill_check import (
    inject_measurement,
    remove_measurement,
    parse_fill_ratio,
    generate_advice,
    FILL_MEASURE_SNIPPET,
)


class InjectMeasurementTests(unittest.TestCase):

    def _write_tex(self, content: str) -> Path:
        path = Path(tempfile.mktemp(suffix='.tex'))
        path.write_text(content, encoding='utf-8')
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_inserts_after_begin_document(self):
        tex = self._write_tex(r'\documentclass{article}\begin{document}Hello\end{document}')
        result = inject_measurement(tex)
        self.assertTrue(result)
        content = tex.read_text()
        self.assertIn('PAGE FILL MEASUREMENT', content)
        # Ensure it's after \begin{document}
        idx_begin = content.find(r'\begin{document}')
        idx_measure = content.find('PAGE FILL MEASUREMENT')
        self.assertGreater(idx_measure, idx_begin)

    def test_skip_if_already_present(self):
        tex = self._write_tex(r'\begin{document}' + FILL_MEASURE_SNIPPET + r'\end{document}')
        result = inject_measurement(tex)
        self.assertFalse(result)

    def test_missing_begin_document_raises(self):
        tex = self._write_tex(r'\documentclass{article}Hello')
        with self.assertRaises(ValueError):
            inject_measurement(tex)


class RemoveMeasurementTests(unittest.TestCase):

    def _write_tex(self, content: str) -> Path:
        path = Path(tempfile.mktemp(suffix='.tex'))
        path.write_text(content, encoding='utf-8')
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_cleans_injected_code(self):
        original = r'\begin{document}Hello\end{document}'
        tex = self._write_tex(r'\begin{document}' + FILL_MEASURE_SNIPPET + r'Hello\end{document}')
        result = remove_measurement(tex)
        self.assertTrue(result)
        content = tex.read_text()
        self.assertNotIn('PAGE FILL MEASUREMENT', content)

    def test_noop_when_not_present(self):
        tex = self._write_tex(r'\begin{document}Hello\end{document}')
        result = remove_measurement(tex)
        self.assertFalse(result)


class ParseFillRatioTests(unittest.TestCase):

    def _write_aux(self, content: str) -> Path:
        path = Path(tempfile.mktemp(suffix='.aux'))
        path.write_text(content, encoding='utf-8')
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_single_page(self):
        aux = self._write_aux(r'\newlabel{pagefill}{{600.0pt}{700.0pt}}')
        result = parse_fill_ratio(aux)
        self.assertAlmostEqual(result['total_pt'], 600.0)
        self.assertAlmostEqual(result['goal_pt'], 700.0)
        self.assertAlmostEqual(result['ratio'], 600.0 / 700.0, places=4)
        self.assertEqual(result['page_count'], 1)

    def test_multi_page(self):
        aux = self._write_aux(
            r'\newlabel{pagefill}{{200.0pt}{700.0pt}}' '\n'
            r'\@abspage@last{2}'
        )
        result = parse_fill_ratio(aux)
        # effective_total = (2-1)*700 + 200 = 900
        self.assertAlmostEqual(result['total_pt'], 900.0)
        self.assertAlmostEqual(result['ratio'], 900.0 / 700.0, places=4)
        self.assertEqual(result['page_count'], 2)

    def test_missing_aux_raises(self):
        with self.assertRaises(FileNotFoundError):
            parse_fill_ratio(Path('/nonexistent/path.aux'))

    def test_missing_pagefill_label_raises(self):
        aux = self._write_aux(r'\relax')
        with self.assertRaises(ValueError):
            parse_fill_ratio(aux)

    def test_zero_goal_returns_zero_ratio(self):
        aux = self._write_aux(r'\newlabel{pagefill}{{100.0pt}{0.0pt}}')
        result = parse_fill_ratio(aux)
        self.assertEqual(result['ratio'], 0)

    def test_remaining_mm_calculated(self):
        aux = self._write_aux(r'\newlabel{pagefill}{{600.0pt}{700.0pt}}')
        result = parse_fill_ratio(aux)
        expected_remaining = (700.0 - 600.0) * 0.3528
        self.assertAlmostEqual(result['remaining_mm'], expected_remaining, places=2)


class GenerateAdviceTests(unittest.TestCase):

    def test_overflow_returns_error(self):
        result = generate_advice(1.05, -10)
        self.assertEqual(result['status'], 'overflow')
        self.assertEqual(result['level'], 'error')
        self.assertTrue(len(result['suggestions']) > 0)

    def test_perfect_99_returns_success(self):
        result = generate_advice(0.995, 1.0)
        self.assertEqual(result['status'], 'perfect')
        self.assertEqual(result['level'], 'success')

    def test_good_96_returns_success(self):
        result = generate_advice(0.96, 10.0)
        self.assertEqual(result['status'], 'good')
        self.assertEqual(result['level'], 'success')

    def test_underfill_heavy_80mm(self):
        result = generate_advice(0.70, 85.0)
        self.assertEqual(result['status'], 'underfill')
        self.assertEqual(result['level'], 'warning')
        # Should suggest adding an experience
        self.assertTrue(any('增加一段经历' in s for s in result['suggestions']))

    def test_underfill_medium_50mm(self):
        result = generate_advice(0.85, 50.0)
        self.assertEqual(result['status'], 'underfill')
        self.assertTrue(any('增加 1 条 bullet' in s for s in result['suggestions']))

    def test_underfill_light_15mm(self):
        result = generate_advice(0.92, 15.0)
        self.assertEqual(result['status'], 'underfill')
        self.assertTrue(any('展开' in s for s in result['suggestions']))

    def test_exactly_1_0_is_not_overflow(self):
        # ratio == 1.0 exactly should NOT be overflow (threshold is >1.0)
        result = generate_advice(1.0, 0)
        self.assertNotEqual(result['status'], 'overflow')

    def test_boundary_at_0_95(self):
        result = generate_advice(0.95, 12.0)
        self.assertEqual(result['status'], 'good')


if __name__ == '__main__':
    unittest.main()
