# LaTeX Editor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an Overleaf-like LaTeX editor page to the Web UI, allowing users to edit `.tex` source and compile/preview PDF from gallery cards.

**Architecture:** New `#page-editor` in the single-page `web/index.html` with CodeMirror 6 (CDN) for editing + iframe PDF preview. Three new API endpoints in `web/server.py` for read/save/compile. Entry via "Edit" button on gallery tarot cards.

**Tech Stack:** CodeMirror 6 (esm.sh CDN), xelatex (subprocess), browser-native PDF iframe, Python stdlib HTTP server.

**Design doc:** `docs/plans/2026-03-05-latex-editor-design.md`

---

### Task 1: Backend — Editor API Endpoints

**Files:**
- Modify: `web/server.py` (route table ~line 893-954, new handler methods after ~line 1157)

**Step 1: Add route entries to `do_GET` and `do_POST`**

In `do_GET` (after the `/api/gallery/pdf/` elif, before `else`):

```python
elif path.startswith('/api/editor/tex'):
    query = urllib.parse.urlparse(self.path).query
    params = urllib.parse.parse_qs(query)
    dir_name = params.get('dir', [''])[0]
    self._get_editor_tex(dir_name)
```

In `do_POST` (after the `/api/generate` elif, before `else`):

```python
elif path == '/api/editor/save':
    self._save_editor_tex()
elif path == '/api/editor/compile':
    self._compile_editor_tex()
```

**Step 2: Implement `_get_editor_tex(dir_name)`**

```python
def _get_editor_tex(self, dir_name):
    """读取 output 目录中的 .tex 文件内容"""
    try:
        if not dir_name or '..' in dir_name or dir_name.startswith('/'):
            self._send_error_json('非法路径', 403)
            return
        tex_path = _output_dir() / dir_name / 'resume-zh_CN.tex'
        try:
            tex_path.resolve().relative_to(_output_dir().resolve())
        except ValueError:
            self._send_error_json('非法路径', 403)
            return
        if not tex_path.exists():
            self._send_error_json('文件不存在', 404)
            return
        content = tex_path.read_text(encoding='utf-8')
        self._send_json({
            'content': content,
            'filename': 'resume-zh_CN.tex',
            'dir_name': dir_name,
        })
    except Exception as e:
        self._send_error_json(str(e), 500)
```

**Step 3: Implement `_save_editor_tex()`**

```python
def _save_editor_tex(self):
    """保存编辑后的 .tex 内容"""
    try:
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        data = json.loads(body.decode('utf-8'))
        dir_name = data.get('dir', '')
        tex_content = data.get('content', '')
        if not dir_name or '..' in dir_name or dir_name.startswith('/'):
            self._send_error_json('非法路径', 403)
            return
        tex_path = _output_dir() / dir_name / 'resume-zh_CN.tex'
        try:
            tex_path.resolve().relative_to(_output_dir().resolve())
        except ValueError:
            self._send_error_json('非法路径', 403)
            return
        if not tex_path.parent.exists():
            self._send_error_json('目录不存在', 404)
            return
        tex_path.write_text(tex_content, encoding='utf-8')
        self._send_json({'success': True})
    except Exception as e:
        self._send_error_json(str(e), 500)
```

**Step 4: Implement `_compile_editor_tex()`**

```python
def _compile_editor_tex(self):
    """保存 + 编译 + 返回状态"""
    import subprocess
    try:
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        data = json.loads(body.decode('utf-8'))
        dir_name = data.get('dir', '')
        tex_content = data.get('content', '')
        if not dir_name or '..' in dir_name or dir_name.startswith('/'):
            self._send_error_json('非法路径', 403)
            return
        out_dir = _output_dir() / dir_name
        tex_path = out_dir / 'resume-zh_CN.tex'
        try:
            tex_path.resolve().relative_to(_output_dir().resolve())
        except ValueError:
            self._send_error_json('非法路径', 403)
            return
        if not out_dir.exists():
            self._send_error_json('目录不存在', 404)
            return

        # 1. Save
        tex_path.write_text(tex_content, encoding='utf-8')

        # 2. Find xelatex
        xelatex_candidates = [
            Path.home() / 'Library' / 'TinyTeX' / 'bin' / 'universal-darwin' / 'xelatex',
            Path.home() / '.TinyTeX' / 'bin' / 'x86_64-linux' / 'xelatex',
        ]
        xelatex_bin = 'xelatex'  # fallback to PATH
        for c in xelatex_candidates:
            if c.exists():
                xelatex_bin = str(c)
                break

        # 3. Compile
        result = subprocess.run(
            [xelatex_bin, '-interaction=nonstopmode', 'resume-zh_CN.tex'],
            cwd=str(out_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )

        pdf_path = out_dir / 'resume-zh_CN.pdf'
        log_path = out_dir / 'resume-zh_CN.log'

        # 4. Read log tail on failure
        log_tail = ''
        if log_path.exists():
            lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
            log_tail = '\n'.join(lines[-30:])

        if result.returncode != 0 or not pdf_path.exists():
            self._send_json({
                'success': False,
                'pages': 0,
                'fill_rate': 0,
                'errors': f'编译失败 (exit code {result.returncode})',
                'log_tail': log_tail,
            })
            return

        # 5. Get page count (macOS mdls or fallback)
        pages = 1
        try:
            mdls = subprocess.run(
                ['mdls', '-name', 'kMDItemNumberOfPages', str(pdf_path)],
                capture_output=True, text=True, timeout=10,
            )
            for line in mdls.stdout.splitlines():
                if 'kMDItemNumberOfPages' in line and '=' in line:
                    val = line.split('=')[1].strip()
                    if val != '(null)':
                        pages = int(val)
        except Exception:
            pass

        # 6. Get fill rate
        fill_rate = 0.0
        try:
            fill_check = str(PROJECT_ROOT / 'tools' / 'page_fill_check.py')
            env = os.environ.copy()
            # Ensure xelatex is in PATH for fill check
            xelatex_dir = str(Path(xelatex_bin).parent)
            env['PATH'] = xelatex_dir + ':' + env.get('PATH', '')
            fr_result = subprocess.run(
                ['python3', fill_check, str(out_dir)],
                capture_output=True, text=True, timeout=60, env=env,
            )
            # Parse fill rate from output like "填充率: 95.4%"
            import re as _re
            m = _re.search(r'填充率[：:]\s*([\d.]+)%', fr_result.stdout)
            if m:
                fill_rate = float(m.group(1))
        except Exception:
            pass

        self._send_json({
            'success': True,
            'pages': pages,
            'fill_rate': fill_rate,
            'errors': '',
            'log_tail': log_tail if pages > 1 else '',
        })

    except subprocess.TimeoutExpired:
        self._send_json({
            'success': False, 'pages': 0, 'fill_rate': 0,
            'errors': '编译超时（60秒）', 'log_tail': '',
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        self._send_error_json(str(e), 500)
```

**Step 5: Verify backend**

```bash
# Start server
cd /path/to/project && python3 web/server.py &

# Test GET tex
curl -s 'http://localhost:8765/api/editor/tex?dir=美团_产品经理_20260304' | python3 -m json.tool | head -5
# Expected: {"content": "% !TEX TS-program...", "filename": "resume-zh_CN.tex", ...}

# Test compile
curl -s -X POST http://localhost:8765/api/editor/compile \
  -H 'Content-Type: application/json' \
  -d '{"dir":"美团_产品经理_20260304","content":"...tex content..."}' | python3 -m json.tool
# Expected: {"success": true, "pages": 1, "fill_rate": 95.4, ...}
```

**Step 6: Commit**

```bash
git add web/server.py
git commit -m "feat(editor): add backend API for tex read/save/compile"
```

---

### Task 2: Frontend — Editor Page HTML + CSS

**Files:**
- Modify: `web/index.html` (add CSS, add `#page-editor` div)

**Step 1: Add editor CSS** (inside `<style>` block, after existing `.pdf-modal` styles)

```css
/* ─── LaTeX Editor ─── */
.editor-toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    background: var(--bg-secondary, #f5f5f5);
    border-bottom: 1px solid var(--border, #e0e0e0);
    flex-shrink: 0;
}
.editor-toolbar .editor-title {
    flex: 1;
    font-weight: 600;
    font-size: 15px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.editor-toolbar button {
    padding: 6px 16px;
    border-radius: 6px;
    border: 1px solid var(--border, #ccc);
    background: var(--bg-primary, #fff);
    cursor: pointer;
    font-size: 13px;
    transition: all 0.15s;
}
.editor-toolbar button:hover { background: var(--bg-hover, #eee); }
.editor-toolbar .btn-compile {
    background: var(--accent, #5046e5);
    color: #fff;
    border-color: var(--accent, #5046e5);
    font-weight: 600;
}
.editor-toolbar .btn-compile:hover { opacity: 0.9; }
.editor-toolbar .btn-compile:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}
.editor-split {
    display: flex;
    flex: 1;
    overflow: hidden;
}
.editor-pane-code {
    width: 50%;
    display: flex;
    flex-direction: column;
    border-right: 2px solid var(--border, #e0e0e0);
    overflow: hidden;
}
.editor-pane-code .cm-editor {
    flex: 1;
    overflow: auto;
}
.editor-pane-pdf {
    width: 50%;
    display: flex;
    flex-direction: column;
    background: #525659;
}
.editor-pane-pdf iframe {
    flex: 1;
    border: none;
    width: 100%;
    height: 100%;
}
.editor-statusbar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 6px 16px;
    background: var(--bg-secondary, #f5f5f5);
    border-top: 1px solid var(--border, #e0e0e0);
    font-size: 12px;
    color: var(--text-secondary, #666);
    flex-shrink: 0;
}
.editor-statusbar .status-icon { font-size: 14px; }
.editor-statusbar .status-error { color: #e53935; }
.editor-statusbar .status-ok { color: #43a047; }
#page-editor {
    display: none;
    flex-direction: column;
    height: calc(100vh - 0px);
    overflow: hidden;
}
#page-editor.active {
    display: flex !important;
}
```

**Step 2: Add editor page HTML** (after `#page-generate` div, before `</main>`)

```html
<div class="page" id="page-editor">
    <div class="editor-toolbar">
        <button onclick="closeEditor()" title="返回画廊">← 返回</button>
        <span class="editor-title" id="editor-title">LaTeX 编辑器</span>
        <span style="color:var(--text-secondary);font-size:12px">Ctrl+Enter 编译</span>
        <button class="btn-compile" id="btn-compile" onclick="compileTeX()">▶ Compile</button>
        <button id="btn-editor-download" onclick="downloadEditorPdf()">下载 PDF</button>
    </div>
    <div class="editor-split">
        <div class="editor-pane-code">
            <div id="cm-editor-mount"></div>
        </div>
        <div class="editor-pane-pdf">
            <iframe id="editor-pdf-iframe" src="about:blank"></iframe>
        </div>
    </div>
    <div class="editor-statusbar">
        <span id="editor-status-icon" class="status-icon">⬤</span>
        <span id="editor-status-text">就绪</span>
        <span id="editor-status-fill" style="margin-left:auto"></span>
    </div>
</div>
```

**Step 3: Verify** — Open browser, check that `#page-editor` exists in DOM but is hidden. No visual regressions on other pages.

**Step 4: Commit**

```bash
git add web/index.html
git commit -m "feat(editor): add editor page HTML structure and CSS"
```

---

### Task 3: Frontend — CodeMirror 6 Integration

**Files:**
- Modify: `web/index.html` (add `<script type="module">` block for CM6 + editor JS)

**Step 1: Add CodeMirror 6 module loader** (at end of `<body>`, as `<script type="module">`)

```html
<script type="module">
import {EditorView, basicSetup} from 'https://esm.sh/@codemirror/basic-setup@0.20.0'
import {keymap} from 'https://esm.sh/@codemirror/view@6.35.0'
import {StreamLanguage} from 'https://esm.sh/@codemirror/language@6.10.0'
import {stex} from 'https://esm.sh/@codemirror/legacy-modes@6.4.0/mode/stex'

let editorView = null;
let currentEditorDir = null;

window._cmCreateEditor = function(content, mountEl) {
    if (editorView) { editorView.destroy(); editorView = null; }
    editorView = new EditorView({
        doc: content,
        extensions: [
            basicSetup,
            StreamLanguage.define(stex),
            keymap.of([{
                key: 'Ctrl-Enter',
                mac: 'Cmd-Enter',
                run: () => { window.compileTeX(); return true; },
            }]),
            EditorView.theme({
                '&': { height: '100%', fontSize: '13px' },
                '.cm-scroller': { overflow: 'auto', fontFamily: 'Menlo, Monaco, Consolas, monospace' },
                '.cm-gutters': { background: '#f8f8f8', borderRight: '1px solid #e0e0e0' },
            }),
        ],
        parent: mountEl,
    });
};

window._cmGetContent = function() {
    return editorView ? editorView.state.doc.toString() : '';
};

window._cmDestroy = function() {
    if (editorView) { editorView.destroy(); editorView = null; }
};
</script>
```

**Step 2: Verify** — Open browser dev console, type `window._cmCreateEditor`, confirm function exists (not undefined). CDN loads may take a few seconds on first load.

**Step 3: Commit**

```bash
git add web/index.html
git commit -m "feat(editor): integrate CodeMirror 6 via esm.sh CDN"
```

---

### Task 4: Frontend — Editor Logic (open/compile/close)

**Files:**
- Modify: `web/index.html` (add JS functions inside existing `<script>` block)

**Step 1: Add editor JS functions** (inside the main `<script>` block, after gallery functions)

```javascript
/* ─── LaTeX Editor ─── */
let currentEditorDir = null;

async function openEditor(dirName) {
    currentEditorDir = dirName;
    // Load tex content
    const res = await fetch('/api/editor/tex?dir=' + encodeURIComponent(dirName));
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }

    // Set title
    const title = dirName.replace(/_/g, ' ');
    document.getElementById('editor-title').textContent = title;

    // Navigate to editor page
    goTo('editor');

    // Init CodeMirror (wait for module to be ready)
    const mountEl = document.getElementById('cm-editor-mount');
    mountEl.innerHTML = '';
    if (window._cmCreateEditor) {
        window._cmCreateEditor(data.content, mountEl);
    } else {
        // Fallback: retry after CDN loads
        setTimeout(() => {
            if (window._cmCreateEditor) window._cmCreateEditor(data.content, mountEl);
            else { mountEl.textContent = data.content; showToast('编辑器加载中，请稍后', 'error'); }
        }, 2000);
    }

    // Load PDF preview
    const pdfUrl = '/api/gallery/pdf/' + encodeURIComponent(dirName) + '/resume-zh_CN.pdf';
    document.getElementById('editor-pdf-iframe').src = pdfUrl;

    // Reset status
    setEditorStatus('ready', '就绪');
}

async function compileTeX() {
    if (!currentEditorDir) return;
    const content = window._cmGetContent ? window._cmGetContent() : '';
    if (!content.trim()) { showToast('内容为空', 'error'); return; }

    const btn = document.getElementById('btn-compile');
    btn.disabled = true;
    btn.textContent = '⏳ Compiling...';
    setEditorStatus('compiling', '编译中...');

    try {
        const res = await fetch('/api/editor/compile', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dir: currentEditorDir, content }),
        });
        const data = await res.json();

        if (data.success) {
            // Refresh PDF (cache bust)
            const pdfUrl = '/api/gallery/pdf/' + encodeURIComponent(currentEditorDir)
                + '/resume-zh_CN.pdf?t=' + Date.now();
            document.getElementById('editor-pdf-iframe').src = pdfUrl;

            const fillText = data.fill_rate ? ` | 填充率: ${data.fill_rate.toFixed(1)}%` : '';
            const pageText = data.pages ? ` | ${data.pages} 页` : '';
            setEditorStatus('ok', '编译成功' + pageText + fillText);
        } else {
            setEditorStatus('error', data.errors || '编译失败');
            if (data.log_tail) {
                console.error('LaTeX log:\n' + data.log_tail);
            }
        }
    } catch (e) {
        setEditorStatus('error', '请求失败: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '▶ Compile';
    }
}

function closeEditor() {
    if (window._cmDestroy) window._cmDestroy();
    document.getElementById('editor-pdf-iframe').src = 'about:blank';
    currentEditorDir = null;
    goTo('home');
    setTimeout(() => {
        const gal = document.getElementById('home-gallery-section');
        if (gal) gal.scrollIntoView({ behavior: 'smooth' });
    }, 300);
}

function downloadEditorPdf() {
    if (!currentEditorDir) return;
    const url = '/api/gallery/pdf/' + encodeURIComponent(currentEditorDir) + '/resume-zh_CN.pdf';
    const a = document.createElement('a');
    a.href = url;
    a.download = 'resume-zh_CN.pdf';
    a.click();
}

function setEditorStatus(type, text) {
    const icon = document.getElementById('editor-status-icon');
    const txt = document.getElementById('editor-status-text');
    icon.className = 'status-icon';
    if (type === 'ok') { icon.textContent = '✅'; icon.classList.add('status-ok'); }
    else if (type === 'error') { icon.textContent = '❌'; icon.classList.add('status-error'); }
    else if (type === 'compiling') { icon.textContent = '⏳'; }
    else { icon.textContent = '⬤'; }
    txt.textContent = text;
}
```

**Step 2: Update `goTo()` function** — Add `editor` page support (ensure it doesn't reset editor state when navigating).

In the `goTo()` function, add this before the generic page show logic:
```javascript
// Do NOT load gallery when navigating to editor
if (page === 'editor') {
    // Editor is managed by openEditor(), just show the page
}
```

**Step 3: Verify** — Browser console: `openEditor('美团_产品经理_20260304')` should load the editor with tex content on left, PDF on right.

**Step 4: Commit**

```bash
git add web/index.html
git commit -m "feat(editor): add open/compile/close editor logic"
```

---

### Task 5: Frontend — Gallery "Edit" Button

**Files:**
- Modify: `web/index.html` (inside `loadGallery()` function where tarot card buttons are rendered)

**Step 1: Add "Edit" button to gallery cards**

Find the card front button area in `loadGallery()` (where "查看" and "下载" buttons are rendered). Add an "编辑" button between them:

```javascript
// Existing: view button
// NEW: edit button
`<button class="tarot-btn" onclick="openEditor('${r.dir_name}')" title="编辑 LaTeX">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
        <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
    </svg>
    编辑
</button>`
// Existing: download button
```

**Step 2: Verify** — Open gallery, confirm each card shows [查看] [编辑] [下载] [删除]. Click "编辑" should open editor page.

**Step 3: Commit**

```bash
git add web/index.html
git commit -m "feat(editor): add edit button to gallery tarot cards"
```

---

### Task 6: Integration Test & Polish

**Step 1: Full flow test**

1. Start server: `python3 web/server.py`
2. Open gallery → click a card → flip → click "编辑"
3. Verify: editor opens with tex on left, PDF on right
4. Edit a bullet text in the tex
5. Press Ctrl+Enter (or click Compile)
6. Verify: status bar shows "编译中..." → then "✅ 编译成功 | 1 页 | 填充率: XX%"
7. Verify: PDF on right refreshes to show the change
8. Click "下载 PDF" → verify download works
9. Click "← 返回" → verify returns to gallery

**Step 2: Error handling test**

1. In editor, delete `\end{document}` line
2. Click Compile
3. Verify: status bar shows ❌ with error message

**Step 3: Edge case fixes** — Fix any issues found during testing (CSS overflow, iframe sizing, CDN load timing, etc.)

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat(editor): polish and integration testing complete"
```
