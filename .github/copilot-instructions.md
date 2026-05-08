# Copilot Instructions for `resume_generator_pro`

## Build, test, and lint commands

This repo is Python + XeLaTeX, with no centralized lint target in `Makefile`/`pyproject`.

- Start Web UI:
  - `python3 web/server.py`
  - Optional port: `python3 web/server.py --port 8765`
- Generate from CLI:
  - `python3 tools/generate_resume.py --person alice 'JD text'`
  - Legacy/default person also works without `--person`
- Run page-fill check on a generated output directory:
  - `python3 tools/page_fill_check.py output/<person_id>/<company>_<role>_<YYYYMMDD>`
- Validate Python syntax (lightweight lint substitute used in this repo):
  - `python3 -m py_compile tools/*.py web/server.py`
- Run tests (stdlib `unittest`):
  - Full suite (current tests): `python3 -m unittest discover -s tests -p 'test_*.py' -v`
  - Single test file: `python3 tests/test_import_resume_parser.py`
  - Single test case: `python3 tests/test_import_resume_parser.py ImportResumeParserTests.test_parse_resume_text_extracts_basic_fields -v`

LaTeX toolchain check:
- `xelatex --version`

## High-level architecture

The system has four core layers:

1. Data/person layer (`tools/person_manager.py`)
- Owns multi-person registry in `data/persons.json` and active person selection.
- Resolves per-person paths (`profile.md`, `experiences/`, `work_materials/`, `output/`).
- Supports legacy single-person layout when `persons.json` does not exist.

2. Generation engine (`tools/generate_resume.py`)
- End-to-end pipeline: profile/experience loading -> JD keyword extraction -> relevance matching -> bullet rewriting -> LaTeX rendering -> XeLaTeX compile -> optional single-page tuning.
- Enforces hard business rules via `STRICT_AI_RULES` and selection helpers (experience classification, caps, ordering, dedupe).
- Writes generation artifacts under output directories, including context/log files (`generation_context.json`, `generation_log.md`).

3. Web/API layer (`web/server.py`)
- Python stdlib HTTP server (`http.server`), no external web framework.
- Exposes APIs for person/profile/experience management, generation, gallery, import-resume flow, and LaTeX editor compile/versioning.
- Reuses the same generation engine and person manager utilities; does not maintain a separate business pipeline.

4. Layout/quality layer (`latex_src/resume/` + `tools/page_fill_check.py`)
- Template source lives in `latex_src/resume/` and is copied into output folders per run.
- `tools/page_fill_check.py` injects measurement snippets into `.tex`, recompiles with XeLaTeX, parses `.aux` for fill ratio, reports underfill/overflow guidance, then cleans injected code.

## Key repository conventions

These conventions are specific and should be preserved in edits:

- XeLaTeX is required for Chinese resumes; do not switch to `pdflatex`.
- Experience classification is strict:
  - Intern/work experience and research experience must stay in their intended sections.
  - One experience must not appear in multiple sections.
- Selection caps are enforced:
  - Total selected experiences <= 5 (code backfills to minimum 3).
  - Awards <= 3, with de-duplication logic for similar scholarship-like awards.
- Bullet rewriting is constrained:
  - Intern/work experience: 2-3 bullets per entry, max 4.
  - Project/research experience: 1-2 bullets per entry.
  - Keep factual/quantified outcomes from source data.
  - Do not fabricate metrics, skills, or achievements.
  - Avoid trailing sentence punctuation in bullets in generated resume content.
- Work material precedence:
  - Prefer non-empty files under `data/{person_id}/work_materials/{company}/` over summarized experience text when available.
- Setup guards before generation:
  - `profile.md` placeholders must be filled.
  - `experiences/` must contain at least one valid entry (not `_template.md`/`README.md`).
- Output isolation is by person:
  - Multi-person mode writes to `output/{person_id}/...`.
  - Legacy mode falls back to `output/...`.

## AI config alignment in this repo

When adapting or extending AI behavior, align with existing project instruction sources:

- Primary workflow/rules source: `CLAUDE.md`
- Skill workflow source: `skills/resume-gen/SKILL.md`
- Claude registration: `.claude/settings.json` (loads `skills/resume-gen`)
- Claude agent spec: `.claude/agents/resume-generator.md`

Keep these sources semantically consistent with `tools/generate_resume.py` business rules. If behavior changes, update both code and the relevant instruction docs together.
