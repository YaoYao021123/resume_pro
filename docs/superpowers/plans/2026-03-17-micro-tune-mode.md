# Micro-tune Mode Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generate-page “微调模式” that lets users select a historical resume package and generate a new resume with minimal-change AI tuning against a new JD.

**Architecture:** Extend existing generation flow instead of creating a second pipeline. Backend adds two APIs (`candidates/query` and `generate/micro-tune`) with strict path/error contracts; engine adds optional seed constraints to prompt and replacement auditing; frontend adds a modal picker + read-only preview and submits the same generate inputs plus `seed_dir`.

**Tech Stack:** Python stdlib HTTP server (`web/server.py`), existing generator (`tools/generate_resume.py`), single-page frontend (`web/index.html`), xelatex compile pipeline, JSON contracts.

**Spec:** `docs/superpowers/specs/2026-03-17-micro-tune-mode-design.md`

---

## Chunk 1: End-to-end 微调模式（后端 + 引擎 + 前端 + 验证）

- [ ] **Step 0: Confirm artifact placement before coding**
  
  Ensure:
  - spec remains at `docs/superpowers/specs/2026-03-17-micro-tune-mode-design.md`
  - implementation plan remains at `docs/superpowers/plans/2026-03-17-micro-tune-mode.md`
  - no overwriting between `specs/` and `plans/`

### Task 1: Backend candidate query API + similarity ranking

**Files:**
- Modify: `web/server.py` (`do_POST` routing block, helper area around `list_gallery_resumes`, new candidate query handler)
- Create: `tools/micro_tune_scoring.py` (candidate scoring and label mapping helpers only)
- Create: `tools/tests/test_micro_tune_scoring.py` (stdlib `unittest` for score boundaries/labels)
- Reuse: `web/server.py:list_gallery_resumes()` as raw candidate source

- [ ] **Step 1: Write failing scoring tests first (TDD)**
  
  In `tools/tests/test_micro_tune_scoring.py`, add tests that fail before implementation:
  - score ordering (high overlap > low overlap)
  - label threshold mapping (`>=75 高`, `50-74 中`, `<50 低`)

- [ ] **Step 2: Add route entry in `do_POST`**
  
  Add:
  - `elif path == '/api/micro-tune/candidates/query': self._query_micro_tune_candidates()`

- [ ] **Step 3: Implement similarity helper functions**
  
  In `tools/micro_tune_scoring.py`, implement deterministic formula:
  - `keyword_overlap_score` (0-60): overlap(new_jd+interview, historical jd+interview)
  - `role_company_score` (0-30): overlap(new_jd tokens, historical role/company tokens)
  - `recency_score` (0-10): based on `generated_at` age
  - total `similarity_score` = sum, clamp 0..100
  - label thresholds: `>=75 高`, `>=50 中`, `<50 低`

- [ ] **Step 4: Implement `_query_micro_tune_candidates()`**
  
  Behavior:
  - parse JSON body
  - validate `jd` required, `interview` is string
  - load gallery entries
  - compute `similarity_score` and `similarity_label` via `tools/micro_tune_scoring.py`
  - include preview fields: `pdf_path`, `jd_text`, `interview_text`, `interview_notes`
  - add `is_incomplete` when history context missing key fields
  - sort by score desc, then date desc
  - ensure response includes required fields per candidate:
    - `dir`, `company`, `role`, `generated_at`, `similarity_score`, `similarity_label`, `pdf_path`

- [ ] **Step 5: Add unified error response helper**
  
  Add `_send_error_code(code, message, status, details=None)` and map candidate API errors:
  - invalid JSON → `INVALID_JSON` (400)
  - missing `jd` → `JD_REQUIRED` (400)
  - non-string `interview` → `INVALID_INTERVIEW` (400)
  - internal failures → `CANDIDATES_QUERY_FAILED` (500)

- [ ] **Step 6: Verify syntax**
  
  Run:
  - `python3 -m py_compile web/server.py tools/generate_resume.py`
  
  Expected:
  - no traceback, exit code 0

- [ ] **Step 7: Run scoring tests to green**
  
  Add deterministic tests:
  - high-overlap candidate yields higher score than low-overlap
  - label thresholds map correctly (`>=75 高`, `50-74 中`, `<50 低`)
  
  Run:
  - `python3 -m unittest tools.tests.test_micro_tune_scoring -v`
  
  Expected:
  - all tests pass


### Task 2: Backend micro-tune generate API with secure seed_dir contract

**Files:**
- Modify: `web/server.py` (`do_POST`, new `_generate_micro_tuned_resume`, path security helpers)
- Create: `tools/micro_tune_seed.py` (seed context/summary loader and entry-id generation)
- Create: `tools/tests/test_micro_tune_seed.py` (stdlib `unittest` for seed parsing, entry_id, truncation)
- Reuse: `web/server.py:_generate_resume`, `tools.generate_resume.generate_resume`

- [ ] **Step 1: Write failing seed/security tests first (TDD)**
  
  In `tools/tests/test_micro_tune_seed.py`, add failing tests for:
  - deterministic entry_id generation
  - truncation limits (3000/2000/120)
  - path validation rejection for traversal/symlink escape

- [ ] **Step 2: Add route entry in `do_POST`**
  
  Add:
  - `elif path == '/api/generate/micro-tune': self._generate_micro_tuned_resume()`

- [ ] **Step 3: Add canonical path validation for `seed_dir`**
  
  Enforce:
  - reject `..`, `/`, leading `.`
  - resolve real path and ensure it is under active person output root (`output/{person_id}` in multi-person mode or legacy output root)
  - reject symlink escape

- [ ] **Step 4: Implement `_generate_micro_tuned_resume()`**
  
  Request contract:
  - `jd` required
  - `seed_dir` required
  - `interview` string (optional)
  - `company/role` optional
  - `prefer_ai` boolean
  
  Flow:
  - load seed bundle via `tools/micro_tune_seed.py`:
    - `seed_context` from `generation_context.json`
    - `seed_resume_summary` from `resume-zh_CN.tex` (or empty summary fallback)
    - enforce limits during load:
      - `seed_context.jd_text` max 3000 chars
      - `seed_context.interview_text` max 2000 chars
      - per entry keep max 2 bullet samples, each <= 120 chars
  - validate required seed fields (`jd_text`)
  - resolve company/role fallback priority: request > seed_context > 未知公司/未知岗位
  - call `generate_resume(..., seed_context=..., seed_resume_summary=..., micro_tune_constraints={mode:'minimal_change', max_replacements:2, preserve_style:True, preserve_structure:True})`
  - return standard generate payload

- [ ] **Step 5: Implement micro-tune error mapping**
  
  Use fixed outward codes:
  - `INVALID_JSON`, `JD_REQUIRED`, `SEED_DIR_REQUIRED`, `INVALID_SEED_DIR`, `SEED_NOT_FOUND`, `INVALID_PREFER_AI`, `SEED_CONTEXT_MISSING`, `MICRO_TUNE_SCOPE_EXCEEDED`, `MICRO_TUNE_FAILED`
  
  Include provider/internal reason only in `error.details.cause`.
  For `MICRO_TUNE_SCOPE_EXCEEDED`, include:
  - `error.details.required_replacements`
  - `error.details.max_replacements` (always 2)
  - `error.details.suggestion` = `use_full_generate`

- [ ] **Step 6: API smoke checks (manual)**
  
  Run server:
  - `python3 web/server.py`
  
  In another shell:
  - candidates: `curl -s -X POST http://localhost:8765/api/micro-tune/candidates/query -H 'Content-Type: application/json' -d '{"jd":"产品经理JD","interview":""}'`
  - generate invalid seed: `curl -s -X POST http://localhost:8765/api/generate/micro-tune -H 'Content-Type: application/json' -d '{"jd":"x","seed_dir":"not-exist"}'`
  
  Expected:
  - first returns JSON with `candidates` array; first item has keys `dir`, `company`, `role`, `similarity_score`, `pdf_path`
  - second returns:
    - `error.code = "SEED_NOT_FOUND"`
    - `error.message` non-empty
    - HTTP 404

- [ ] **Step 7: Run seed/security tests to green**
  
  Add tests for:
  - entry_id deterministic generation
  - seed text truncation limits (3000/2000/120)
  - invalid seed path rejection (`..`, symlink escape)
  
  Run:
  - `python3 -m unittest tools.tests.test_micro_tune_seed -v`
  
  Expected:
  - all tests pass


### Task 3: Generator engine seed constraints + replacement audit

**Files:**
- Modify: `tools/generate_resume.py`
  - `generate_resume(...)` signature and call path
  - `_build_ai_prompt(...)` (seed injection block)
  - plan parsing and enforcement section after AI response
  - `_write_generation_context(...)` payload extension
- Create: `tools/tests/test_micro_tune_generation.py` (stdlib `unittest` for replacement audit guards)
- Reuse: `tools/micro_tune_seed.py` for deterministic `entry_id`

- [ ] **Step 1: Write failing generator guard tests first (TDD)**
  
  Create `tools/tests/test_micro_tune_generation.py` for:
  - replacement_count mismatch rejection
  - unknown entry_id rejection
  - replacement_count > 2 rejection marker

- [ ] **Step 2: Extend function signatures**
  
  Add optional params:
  - `seed_context: dict | None = None`
  - `seed_resume_summary: dict | None = None`
  - `micro_tune_constraints: dict | None = None`

- [ ] **Step 3: Inject micro-tune prompt block**
  
  In `_build_ai_prompt`, when seed fields present:
  - append section with seed summary
  - append strict constraints: `mode=minimal_change`, preserve structure/style, `max_replacements=2`
  - require replacement metadata in AI plan (retained/replaced ids/count)

- [ ] **Step 4: Parse and validate replacement metadata**
  
  Enforce before applying selected experiences:
  - `replacement_count == len(seed_replaced_entries)`
  - `replacement_count + len(seed_retained_entries) <= len(seed entries provided to AI)` (sanity)
  - all referenced entry IDs exist in seed summary
  - if `replacement_count > 2`, raise explicit runtime error marker for API to map to `MICRO_TUNE_SCOPE_EXCEEDED`
  - if count mismatch / unknown `entry_id`, raise explicit runtime error marker for API to map to `MICRO_TUNE_FAILED`

- [ ] **Step 5: Implement retain-score gate (spec §6.1)**
  
  Implement deterministic replacement guard before final apply:
  - `core_demands` source: `ai_plan.jd_understanding.core_demands` (fallback to seed/new JD keyword extraction if missing)
  - tokenization: lowercase, split by CJK/latin token regex, drop stopwords and single-char latin tokens
  - compute `retain_score` per seed entry with weights:
    - core-demand match: 50%
    - key tech/object preservation: 30%
    - quantified-result relevance: 20%
  - enforce thresholds:
    - `retain_score >= 65` => must retain
    - `45-64` => optional retain/replace
    - `<45` => replace preferred
  - if AI plan attempts replacing must-retain entries, fail as `MICRO_TUNE_SCOPE_EXCEEDED`

- [ ] **Step 6: Persist micro-tune audit fields to `generation_context.json`**
  
  Add:
  - `micro_tune.seed_dir`
  - `micro_tune.replacement_count`
  - `micro_tune.seed_replaced_entries`
  - `micro_tune.seed_retained_entries`

- [ ] **Step 7: Implement unique output directory naming**
  
  In generation path used by micro-tune:
  - base format: `{company}_{role}_{YYYYMMDD_HHMMSS}`
  - on collision: append `_v2`, `_v3`, ... with atomic existence checks
  - guarantee no overwrite of existing directories

- [ ] **Step 8: Run generator tests + syntax**
  
  Run:
  - `python3 -m unittest tools.tests.test_micro_tune_generation -v`
  - `python3 -m py_compile tools/generate_resume.py`
  
  Expected:
  - exit code 0


### Task 4: Generate page UI — micro-tune mode, picker modal, read-only preview

**Files:**
- Modify: `web/index.html`
  - generate page markup near JD/面经 section and generate button
  - JS state + modal open/close + candidate fetch + preview render
  - `startGenerate()` payload branch

- [ ] **Step 1: Add “微调模式” controls in generate page**
  
  Add:
  - toggle checkbox/switch
  - “选择历史简历” button
  - selected seed summary chip

- [ ] **Step 2: Add candidate picker modal markup**
  
  Modal includes:
  - left: candidate list cards with score and incomplete badge
  - right: read-only preview (PDF iframe + 历史JD + 历史面经/笔记)
  - confirm/cancel buttons

- [ ] **Step 3: Implement JS data flow**
  
  Add functions:
  - `openMicroTuneModal()`
  - `loadMicroTuneCandidates()`
  - `selectMicroTuneCandidate(dir)`
  - `renderMicroTunePreview(candidate)`
  - `confirmMicroTuneSeed()`

- [ ] **Step 4: Extend `startGenerate()` request branch**
  
  Branch:
  - normal mode → existing `/api/generate`
  - micro-tune mode with selected seed → `/api/generate/micro-tune`
  
  Keep existing success UI behavior (open editor / open PDF / gallery refresh).

- [ ] **Step 5: Frontend sanity check**
  
  Manual checks in browser:
  - toggle micro mode on/off
  - no seed selected should block submit with clear toast
  - select seed shows preview and submit hits micro endpoint
  
  Expected network behavior:
  - micro mode OFF: request URL is `/api/generate`
  - micro mode ON + seed selected: request URL is `/api/generate/micro-tune`
  - micro payload includes `jd`, `interview`, `seed_dir`, `company`, `role`, `prefer_ai`


### Task 5: End-to-end verification and regression

**Files:**
- Verify modified files:
  - `web/server.py`
  - `tools/generate_resume.py`
  - `web/index.html`

- [ ] **Step 1: Run syntax baseline for touched Python files**
  
  Run:
  - `python3 -m py_compile web/server.py tools/generate_resume.py tools/micro_tune_scoring.py tools/micro_tune_seed.py`
  
  Expected:
  - all pass

- [ ] **Step 2: Run micro-tune happy path**
  
  Preconditions:
  - at least one valid historical output directory exists
  
  Actions:
  - open web UI
  - fill new JD/面经
  - enable 微调模式
  - pick historical resume and generate
  
  Expected:
  - success payload
  - new output directory created
  - original historical directory unchanged

- [ ] **Step 3: Run key failure-path checks**
  
  Validate:
  - invalid `seed_dir` → 403/`INVALID_SEED_DIR`
  - missing seed context in selected dir → 400/`SEED_CONTEXT_MISSING`
  - replacement over-limit path (forced by debug seed/mock) → 409/`MICRO_TUNE_SCOPE_EXCEEDED`
  - candidate request with non-string interview → 400/`INVALID_INTERVIEW`
  - output-dir collision case on same second timestamp → no overwrite, creates `_v2` suffix
  - candidate list is full-visibility (includes incomplete seeds with `is_incomplete=true`) and sorted by `similarity_score desc`, tie-break `generated_at desc`
  - preview source priority for interview panel is `interview_notes` first, then `interview_text`

- [ ] **Step 4: Regression on existing flows**
  
  Confirm unchanged:
  - normal `/api/generate` flow
  - editor `/api/editor/regenerate`
  - gallery list & PDF open

- [ ] **Step 5: Verify persisted micro-tune audit fields**
  
  After one successful micro-tune run, inspect new output dir:
  - `generation_context.json` contains `micro_tune.seed_dir`
  - `generation_context.json` contains `micro_tune.replacement_count`
  - `generation_context.json` contains `micro_tune.seed_replaced_entries` and `seed_retained_entries`
  
  Expected:
  - fields exist and are consistent with API result / replacement constraints

- [ ] **Step 6: Validate Rule 6 + page quality constraints**
  
  For one micro-tune output:
  - inspect `resume-zh_CN.tex` bullets: each bullet matches `短标题：具体成果` (no ending punctuation)
  - run fill check and ensure single-page constraints remain valid
  
  Run:
  - `python3 tools/page_fill_check.py output/<new_dir>`
  
  Expected:
  - no crash; fill ratio emitted; page count stays 1 after tune chain

- [ ] **Step 7: Commit in focused slices**
  
  Suggested commit sequence:
  1) backend candidate API
  2) micro-tune generate API + security
  3) generator seed constraints
  4) frontend UI
  5) tests + output naming safety
  6) final verification fixes

---

## Execution Notes

- Use `@superpowers/test-driven-development` before each behavior change.
- Use `@superpowers/systematic-debugging` if any endpoint or generation stage fails unexpectedly.
- Before claiming completion, run `@superpowers/verification-before-completion`.
