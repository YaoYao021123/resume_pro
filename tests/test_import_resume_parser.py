import io
import shutil
import sys
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web import server
from tools.generate_resume import generate_latex


class ImportResumeParserTests(unittest.TestCase):
    def test_parse_resume_text_extracts_basic_fields(self):
        text = """
张同学
alex.sample@example.com
(+86) 138-0000-0000

教育背景
示例大学 管理科学与工程 硕士 2023/09 - 2025/06

实习经历
星云科技 产品经理实习生 2024/06 - 2024/09
- 拉新漏斗优化，注册转化率提升18%
"""
        parsed = server.parse_resume_text_to_structured(text)
        self.assertEqual(parsed['basic']['name_zh'], '张同学')
        self.assertEqual(parsed['basic']['email'], 'alex.sample@example.com')
        self.assertIn('138', parsed['basic']['phone'])
        self.assertGreaterEqual(len(parsed['education']), 1)
        self.assertGreaterEqual(len(parsed['experiences']), 1)

    def test_parse_resume_text_keeps_unmapped_in_pending(self):
        text = """
Alex Zhang
alex.sample@example.com
Some custom achievements line that parser cannot map directly
"""
        parsed = server.parse_resume_text_to_structured(text)
        pending = parsed.get('pending_text', '')
        self.assertIn('custom achievements', pending)

    def test_extract_text_from_docx_bytes(self):
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body>'
            '<w:p><w:r><w:t>张同学</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>alex.sample@example.com</w:t></w:r></w:p>'
            '</w:body>'
            '</w:document>'
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('word/document.xml', document_xml)

        text = server.extract_text_from_upload('resume.docx', buf.getvalue())
        self.assertIn('张同学', text)
        self.assertIn('alex.sample@example.com', text)

    def test_extract_text_from_upload_utf16_txt(self):
        raw = "张同学\nalex.sample@example.com\n产品经理".encode('utf-16')
        text = server.extract_text_from_upload('resume.txt', raw)
        self.assertIn('张同学', text)
        self.assertIn('alex.sample@example.com', text)

    def test_extract_text_from_upload_utf32_txt(self):
        raw = "王同学\nwang@example.com\nData Analyst".encode('utf-32')
        text = server.extract_text_from_upload('resume.txt', raw)
        self.assertIn('王同学', text)
        self.assertIn('wang@example.com', text)

    def test_extract_text_from_upload_gb18030_txt(self):
        raw = "李同学\nli@example.com\n市场分析".encode('gb18030')
        text = server.extract_text_from_upload('resume.txt', raw)
        self.assertIn('李同学', text)
        self.assertIn('li@example.com', text)

    def test_create_import_draft_dir_scaffolds_template(self):
        dir_name = server.create_import_draft_dir('测试公司', '产品经理')
        out_dir = server._output_dir() / dir_name
        try:
            self.assertTrue(out_dir.exists())
            self.assertTrue((out_dir / 'resume-zh_CN.tex').exists())
            self.assertTrue((out_dir / 'resume.cls').exists())
        finally:
            if out_dir.exists():
                shutil.rmtree(out_dir)

    def test_create_import_draft_dir_scaffolds_en_template(self):
        dir_name = server.create_import_draft_dir('TestCo', 'Analyst', language='en')
        out_dir = server._output_dir() / dir_name
        try:
            self.assertTrue(out_dir.exists())
            self.assertTrue((out_dir / 'resume-en.tex').exists())
            self.assertFalse((out_dir / 'resume-zh_CN.tex').exists())
            self.assertTrue((out_dir / 'resume.cls').exists())
        finally:
            if out_dir.exists():
                shutil.rmtree(out_dir)

    def test_render_imported_resume_tex_includes_basic_info(self):
        structured = {
            'basic': {
                'name_zh': '张同学',
                'name_en': 'Alex Zhang',
                'email': 'alex.sample@example.com',
                'phone': '(+86) 138-0000-0000',
            },
            'education': [],
            'experiences': [],
            'awards': [],
            'skills': {'tech': 'Python, SQL', 'software': '', 'languages': '英语'},
        }
        tex = server.render_imported_resume_tex(structured)
        self.assertIn('\\name{张同学 Alex Zhang}', tex)
        self.assertIn('alex.sample@example.com', tex)
        self.assertIn('\\section{教育背景}', tex)

    def test_render_imported_resume_tex_en_includes_english_sections(self):
        structured = {
            'basic': {
                'name_zh': '',
                'name_en': 'Alex Zhang',
                'email': 'alex.sample@example.com',
                'phone': '(+1) 555-0100',
            },
            'education': [],
            'experiences': [],
            'awards': [],
            'skills': {'tech': 'Python, SQL', 'software': 'Tableau', 'languages': 'English, Chinese'},
        }
        tex = server.render_imported_resume_tex(structured, language='en')
        self.assertIn('\\name{Alex Zhang}', tex)
        self.assertIn('\\section{Education}', tex)
        self.assertIn('\\section{Experience}', tex)
        self.assertIn('\\section{Skills}', tex)

    def test_generate_latex_en_uses_english_sections(self):
        profile = {
            'name_zh': '张同学',
            'name_en': 'Alex Zhang',
            'email': 'alex.sample@example.com',
            'phone': '(+1) 555-0100',
            'github': '',
            'linkedin': '',
            'education': [{
                'school': 'Example University',
                'degree': 'Master',
                'major': 'Data Science',
                'department': 'Engineering',
                'time': '2023/09 -- 2025/06',
                'gpa': '3.9/4.0',
                'rank': '',
                'courses': 'Machine Learning, Databases',
            }],
            'awards': [],
            'skills_tech': 'Python, SQL',
            'skills_software': 'Tableau',
            'skills_lang': 'English, Chinese',
            'projects': [],
            'publications': [],
        }
        experiences = [{
            'company': 'Acme Inc.',
            'city': 'Shanghai',
            'department': 'Data',
            'role': 'Analyst Intern',
            'time_start': '2024/06',
            'time_end': '2024/09',
            'tags': 'data, analysis',
            'work_items': [{'title': 'Funnel Analysis', 'desc': 'Improved conversion by 18%'}],
            'selected_bullets': ['Optimized funnel conversion by 18% across two product flows'],
            'filename': '01_acme.md',
        }]
        jd_keywords = {'tech': ['python'], 'domain': ['analysis'], 'skill': []}
        tex = generate_latex(profile, experiences, jd_keywords, language='en')
        self.assertIn('\\section{Education}', tex)
        self.assertIn('\\section{Experience}', tex)
        self.assertIn('\\section{Skills}', tex)
        self.assertNotIn('zh_CN-Adobefonts_external', tex)


if __name__ == '__main__':
    unittest.main()
