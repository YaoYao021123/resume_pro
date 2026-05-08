# Parser Encoding + English Resume + Vercel Guide Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix garbled imported resume text and add explicit `zh/en` resume generation across CLI/Web/import/editor flows, with a practical Vercel deployment guide.

**Architecture:** Keep the existing `web/server.py` + `tools/generate_resume.py` pipeline, but introduce one shared language resolver and filename resolver to remove hardcoded `resume-zh_CN.*` assumptions. Add robust text decoding for upload parsing, then thread `language` through generation/import/editor/gallery APIs and front-end requests while preserving default Chinese behavior.

**Tech Stack:** Python 3.11, stdlib `unittest`, existing HTTP server (`http.server`), XeLaTeX toolchain, HTML/vanilla JS front-end.

---

## File Structure (lock before tasks)

- Create: `tools/language_utils.py` (single source for `normalize_language` + filename resolver)
- Modify: `web/server.py` (upload decode hardening, language-aware import/generate/editor/gallery paths)
- Modify: `tools/generate_resume.py` (CLI + generation pipeline language support)
- Modify: `tools/page_fill_check.py` (accept explicit tex filename/language)
- Modify: `web/index.html` (language selectors + API payload propagation)
- Modify: `tests/test_import_resume_parser.py` (encoding regression + import language tests)
- Create: `tests/test_language_pipeline.py` (language resolver + API behavior + filename routing)
- Modify: `README.md` (English feature usage + Vercel hybrid deployment guide)
- Modify: `CLAUDE.md` (补充 zh/en 文件名与语言参数契约，避免内部指令与代码漂移)
- Modify: `skills/resume-gen/SKILL.md`（同步语言参数与输出文件命名规则）

---

## Chunk 1: Core contracts and parser correctness

### Task 0: Baseline verification before changes

**Files:**
- Modify: (none, verification only)

- [ ] **Step 1: Run baseline syntax check**

Run: `python3 -m py_compile tools/*.py web/server.py`  
Expected: exit code 0

- [ ] **Step 2: Run baseline tests**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v`  
Expected: capture baseline pass/fail status before edits

### Task 1: Add shared language resolver and file-name contract

**Files:**
- Create: `tools/language_utils.py`
- Test: `tests/test_language_pipeline.py`

- [ ] **Step 1: Write failing tests for language normalization and filename mapping**

```python
class LanguageUtilsTests(unittest.TestCase):
    def test_normalize_language_defaults_to_zh(self):
        self.assertEqual(normalize_language(None), "zh")
        self.assertEqual(normalize_language(""), "zh")

    def test_normalize_language_rejects_invalid_with_contract_message(self):
        with self.assertRaisesRegex(ValueError, r"invalid language: jp; allowed: zh,en"):
            normalize_language("jp")

    def test_resolve_resume_filenames_for_en(self):
        tex, pdf = resolve_resume_filenames("en")
        self.assertEqual(tex, "resume-en.tex")
        self.assertEqual(pdf, "resume-en.pdf")
```

- [ ] **Step 2: Run only new tests and verify failures**

Run: `python3 -m unittest tests.test_language_pipeline -v`  
Expected: FAIL with missing module/functions

- [ ] **Step 3: Implement `normalize_language()` and `resolve_resume_filenames()` in `tools/language_utils.py`**

- [ ] **Step 4: Re-run tests to verify pass**

Run: `python3 -m unittest tests.test_language_pipeline -v`  
Expected: PASS for resolver contracts

- [ ] **Step 5: Commit**

```bash
git add tools/language_utils.py tests/test_language_pipeline.py
git commit -m "feat: add shared language and filename resolvers

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 2: Fix upload text decoding garble regression

**Files:**
- Modify: `web/server.py` (`_decode_text_bytes*` logic)
- Modify: `tests/test_import_resume_parser.py`

- [ ] **Step 1: Add failing tests for UTF-16/UTF-32 text uploads**

```python
def test_extract_text_from_utf16_txt():
    content = "张同学\nalex@example.com".encode("utf-16")
    text = server.extract_text_from_upload("resume.txt", content)
    self.assertIn("张同学", text)

def test_extract_text_from_utf32_txt():
    content = "教育背景\n示例大学".encode("utf-32")
    text = server.extract_text_from_upload("resume.md", content)
    self.assertIn("示例大学", text)

def test_extract_text_handles_utf8_sig_bom():
    content = "张同学\n技能".encode("utf-8-sig")
    text = server.extract_text_from_upload("resume.txt", content)
    self.assertIn("张同学", text)

def test_extract_text_regression_for_gb18030():
    content = "实习经历\n示例公司".encode("gb18030")
    text = server.extract_text_from_upload("resume.txt", content)
    self.assertIn("示例公司", text)

def test_extract_text_fallback_to_latin1_replace_when_all_candidates_fail():
    content = b"\xff\xfe\xfa\xfb\x00\x81"  # intentionally malformed mixed bytes
    text = server.extract_text_from_upload("resume.txt", content)
    self.assertTrue(isinstance(text, str))
    self.assertGreater(len(text), 0)
```

- [ ] **Step 2: Run parser tests to verify failure**

Run: `python3 tests/test_import_resume_parser.py -v`  
Expected: FAIL on garbled decoding assertions

- [ ] **Step 3: Implement best-effort decoding with BOM + scoring fallback**

- [ ] **Step 4: Re-run parser tests**

Run: `python3 tests/test_import_resume_parser.py -v`  
Expected: PASS with non-garbled extracted content

- [ ] **Step 5: Commit**

```bash
git add web/server.py tests/test_import_resume_parser.py
git commit -m "fix: harden upload decoding to avoid garbled resume text

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Chunk 2: End-to-end language support in generation/import/editor

### Task 3: Add language support to CLI and generation engine

**Files:**
- Modify: `tools/generate_resume.py`
- Modify: `tests/test_language_pipeline.py`

- [ ] **Step 1: Add failing tests for `language=en` output filenames**

```python
def test_generate_resume_en_outputs_resume_en_files():
    result = generate_resume("JD text", person_id="default", language="en")
    self.assertTrue(result["pdf_path"].endswith("/resume-en.pdf"))

def test_cli_invalid_language_exits_nonzero():
    # run subprocess: python3 tools/generate_resume.py --language jp "JD"
    # assert returncode != 0 and stderr/stdout contains invalid language contract
    ...

def test_generation_context_persists_language():
    result = generate_resume("JD text", person_id="default", language="en")
    ctx = json.loads((Path("output") / result["output_dir"] / "generation_context.json").read_text(encoding="utf-8"))
    self.assertEqual(ctx["language"], "en")
```

- [ ] **Step 2: Run targeted tests to verify failure**

Run: `python3 -m unittest tests.test_language_pipeline -v`  
Expected: FAIL because engine still hardcodes `resume-zh_CN.*`

- [ ] **Step 3: Implement language-aware tex/pdf selection in generate pipeline and CLI `--language` parsing**

- [ ] **Step 4: Re-run targeted tests**

Run: `python3 -m unittest tests.test_language_pipeline -v`  
Expected: PASS for CLI/engine filename routing

- [ ] **Step 5: Commit**

```bash
git add tools/generate_resume.py tests/test_language_pipeline.py
git commit -m "feat: support explicit zh/en generation in CLI and engine

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 4: Add language support to import-resume APIs and rendering

**Files:**
- Modify: `web/server.py`
- Modify: `tests/test_import_resume_parser.py`

- [ ] **Step 1: Add failing tests for import draft + compile using `language=en`**

```python
def test_create_import_draft_dir_en_scaffolds_english_template(self):
    dir_name = server.create_import_draft_dir("Test", "PM", language="en")
    out_dir = server._output_dir() / dir_name
    self.assertTrue((out_dir / "resume-en.tex").exists())

def test_render_imported_resume_tex_en_uses_english_sections(self):
    tex = server.render_imported_resume_tex(structured, language="en")
    self.assertIn(r"\section{Education}", tex)

def test_confirm_compile_rejects_language_mismatch_400(self):
    # create draft with zh, confirm with en => 400
    ...

def test_legacy_import_dir_infers_language_and_backfills_context(self):
    # no context.language + existing resume-en.tex -> infer en and persist
    ...
```

- [ ] **Step 2: Run parser/import tests to verify failure**

Run: `python3 tests/test_import_resume_parser.py -v`  
Expected: FAIL (functions do not accept/use language yet)

- [ ] **Step 3: Implement language-aware import draft creation/render/compile and legacy fallback policy**

- [ ] **Step 4: Re-run parser/import tests**

Run: `python3 tests/test_import_resume_parser.py -v`  
Expected: PASS for en+zh import paths

- [ ] **Step 5: Commit**

```bash
git add web/server.py tests/test_import_resume_parser.py
git commit -m "feat: add zh/en support for import-resume creation and compile

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 5: Cover editor/gallery/page-fill language paths

**Files:**
- Modify: `web/server.py`
- Modify: `tools/page_fill_check.py`
- Modify: `tests/test_language_pipeline.py`

- [ ] **Step 1: Add failing tests for editor compile/synctex/gallery path resolution by language**

```python
def test_editor_compile_reads_language_specific_tex():
    # directory with context.language=en should compile resume-en.tex
    # assert subprocess called with resume-en.tex
    self.assertIn("resume-en.tex", called_args)

def test_legacy_dir_language_inference_prefers_existing_en_tex():
    # both resume-en.tex and resume-zh_CN.tex exist -> deterministic preference en
    self.assertEqual(inferred, "en")

def test_editor_synctex_and_gallery_use_language_specific_pdf_tex():
    # verify returned filenames for en context
    self.assertIn("resume-en.pdf", response_json["pdf_path"])

def test_page_fill_check_accepts_tex_filename():
    result = check_page_fill(output_dir, tex_filename="resume-en.tex")
    self.assertIn("ratio", result)

def test_editor_regenerate_uses_language_specific_tex():
    # context.language=en should rewrite resume-en.tex (not zh file)
    self.assertIn("resume-en.tex", rewritten_target)

def test_version_snapshot_uses_language_specific_files():
    # snapshot metadata should reference resume-en.tex/pdf for en context
    self.assertEqual(snapshot["tex_file"], "resume-en.tex")
    self.assertEqual(snapshot["pdf_file"], "resume-en.pdf")

def test_generate_api_invalid_language_returns_400():
    resp = client.post("/api/generate", json={"jd_text": "x", "language": "jp"})
    self.assertEqual(resp.status_code, 400)
    self.assertIn("invalid language", resp.json().get("error", ""))
```

- [ ] **Step 2: Run language pipeline tests and verify failure**

Run: `python3 -m unittest tests.test_language_pipeline -v`  
Expected: FAIL on editor/gallery language routing

- [ ] **Step 3: Implement resolver usage for editor endpoints and gallery metadata**

- [ ] **Step 4: Add `tex_filename` support in `tools/page_fill_check.py` and wire callers**

- [ ] **Step 5: Re-run language pipeline tests**

Run: `python3 -m unittest tests.test_language_pipeline -v`  
Expected: PASS for routing and fill-check compatibility

- [ ] **Step 6: Commit**

```bash
git add web/server.py tools/page_fill_check.py tests/test_language_pipeline.py
git commit -m "feat: make editor gallery and fill-check language-aware

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 6: Update front-end payloads for language selection

**Files:**
- Modify: `web/index.html`
- Modify: `tests/test_language_pipeline.py` (request payload assertions as needed)

- [ ] **Step 1: Add failing test/assertion for generate/import requests carrying language**
  - 至少覆盖：
    - `/api/generate` payload 包含 `language`
    - `/api/import-resume/create-empty` payload 包含 `language`
    - `/api/import-resume/confirm-compile` payload 包含 `language`

- [ ] **Step 2: Implement UI language selectors and propagate `language` in fetch payloads**

- [ ] **Step 3: Manually sanity-check UI wiring**

Run: `python3 web/server.py --port 8765` and verify request payload includes `language`

- [ ] **Step 4: Commit**

```bash
git add web/index.html tests/test_language_pipeline.py
git commit -m "feat: add language selector and API propagation in web UI

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Chunk 3: Documentation, verification, and final git delivery

### Task 7: Document English usage and Vercel hybrid deployment

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `skills/resume-gen/SKILL.md`

- [ ] **Step 1: Add English resume usage docs**
- [ ] **Step 2: Add Vercel deployment guide (BFF proxy + separate XeLaTeX backend container)**
- [ ] **Step 3: Add required env vars and minimal `vercel.json`/API route guidance**
- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md skills/resume-gen/SKILL.md
git commit -m "docs: add english resume usage and vercel deployment guide

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 8: Final verification and direct commit to `main`

**Files:**
- Modify: (none, verification only)

- [ ] **Step 1: Run syntax validation**

Run: `python3 -m py_compile tools/*.py web/server.py`
Expected: no output, exit code 0

- [ ] **Step 2: Run test suite for project tests**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v`
Expected: PASS

- [ ] **Step 3: Run auth/billing backend tests (if present in current tree)**

Run: `python3 -m unittest discover -s backend/auth_billing_service/tests -p 'test_*.py' -v`
Expected: PASS or skip if directory absent in this worktree

- [ ] **Step 4: Stage all required changes and commit on `main`**

```bash
git add tools/language_utils.py tools/generate_resume.py tools/page_fill_check.py web/server.py web/index.html tests/test_import_resume_parser.py tests/test_language_pipeline.py README.md docs/superpowers/specs/2026-03-24-parser-encoding-english-vercel-design.md docs/superpowers/plans/2026-03-24-parser-encoding-english-vercel-implementation.md
git commit -m "feat: fix import garble and add zh/en resume pipeline with vercel guide

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

- [ ] **Step 5: Confirm branch and log**

Run: `git --no-pager branch --show-current && git --no-pager log --oneline --max-count=3`
Expected: `main` and latest commit includes this feature
