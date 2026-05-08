const state = {
  serverUrl: 'http://localhost:8765',
  currentDir: '',
  parsed: null,
  versions: [],
  floatWindowId: null,
};

const $ = (id) => document.getElementById(id);

function escapeHtml(s = '') {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ─── Toast feedback ─────────────────────────────────
let _toastTimer = null;
function showToast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.classList.remove('show'); }, 1200);
}

function setStatus(msg, isError = false) {
  const el = $('statusText');
  el.textContent = msg;
  el.style.color = isError ? '#b24a3e' : '#5c5b57';
}

// ─── LaTeX helpers ──────────────────────────────────
function cleanLatex(text) {
  if (!text) return '';
  return text
    .replace(/\\textbf\{([^}]*)\}/g, '$1')
    .replace(/\\textit\{([^}]*)\}/g, '$1')
    .replace(/\\quad/g, ' ')
    .replace(/\\normalsize/g, '')
    .replace(/\\textperiodcentered/g, '·')
    .replace(/\\%/g, '%').replace(/\\&/g, '&').replace(/\\_/g, '_').replace(/\\#/g, '#')
    .replace(/\\\\/g, '').replace(/\s+/g, ' ').trim();
}

function parseDateParts(raw) {
  const s = String(raw || '').trim();
  if (!s) return null;
  if (s === '至今') return { special: '至今' };
  const m = s.match(/(\d{4})[\/\-.年](\d{1,2})(?:[\/\-.月](\d{1,2}))?/);
  if (!m) return null;
  const month = String(Math.max(1, Math.min(12, Number(m[2]) || 1))).padStart(2, '0');
  const day = m[3] ? String(Math.max(1, Math.min(31, Number(m[3]) || 1))).padStart(2, '0') : '01';
  return { year: m[1], month, day };
}

function formatDateByMode(raw, mode) {
  const p = parseDateParts(raw);
  if (!p) return String(raw || '').trim();
  if (p.special) return p.special;
  if (mode === 'ym_slash') return `${p.year}/${p.month}`;
  if (mode === 'ym_dash') return `${p.year}-${p.month}`;
  if (mode === 'ymd_dash') return `${p.year}-${p.month}-${p.day}`;
  return `${p.year}/${p.month}/${p.day}`;
}

function splitDateRange(raw) {
  const s = String(raw || '').trim();
  if (!s) return { start: '', end: '' };
  const m = s.match(/^(.*?)(?:\s*(?:--|—|–)\s*|\s+-\s+)(.+)$/);
  if (!m) return { start: s, end: '' };
  return { start: m[1].trim(), end: m[2].trim() };
}

// ─── Clipboard ──────────────────────────────────────
async function copyText(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
}

async function copyAndToast(text, msg) {
  if (!text) return;
  await copyText(text);
  showToast(msg || '已复制');
}

// ─── Fill into active page input ────────────────────
async function fillIntoPage(text) {
  if (!text) return;
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!tab) { showToast('未找到活动标签页'); return; }
    // Skip if the active tab is this popup window itself
    if (tab.url?.startsWith('chrome-extension://')) {
      // In floating mode, find the previous non-extension tab
      const tabs = await chrome.tabs.query({ lastFocusedWindow: false, active: true });
      const target = tabs.find(t => !t.url?.startsWith('chrome-extension://'));
      if (!target) { showToast('请先点击目标网页的输入框'); return; }
      await chrome.tabs.sendMessage(target.id, { action: 'insertText', text });
    } else {
      await chrome.tabs.sendMessage(tab.id, { action: 'insertText', text });
    }
    showToast('已填入');
  } catch (e) {
    showToast('填入失败，请先点击目标输入框');
  }
}

// ─── API ────────────────────────────────────────────
async function fetchJSON(path, options = {}) {
  const resp = await fetch(`${state.serverUrl}${path}`, options);
  const data = await resp.json();
  if (data && data.error) throw new Error(data.error);
  return data;
}

// ─── LaTeX parser ───────────────────────────────────
function parseResumeTex(tex) {
  const name = cleanLatex((tex.match(/\\name\{([^}]*)\}/) || [])[1] || '');
  const email = cleanLatex((tex.match(/\\email\{([^}]*)\}/) || [])[1] || '');
  const phone = cleanLatex((tex.match(/\\phone\{([^}]*)\}/) || [])[1] || '');
  const sections = [];
  const sectionRe = /\\section\{([^}]*)\}([\s\S]*?)(?=\\section\{|\\end\{Form\}|\\end\{document\})/g;
  let sec;
  while ((sec = sectionRe.exec(tex))) {
    const title = cleanLatex(sec[1]);
    const body = sec[2] || '';
    const lines = body.split('\n');
    let currentEntry = '', currentDates = '';
    const items = [];
    lines.forEach((lineRaw) => {
      const line = lineRaw.trim();
      const dated = line.match(/^\\datedsubsection\{(.+?)\}\{(.+?)\}$/);
      if (dated) { currentEntry = cleanLatex(dated[1]); currentDates = cleanLatex(dated[2]); return; }
      const item = line.match(/^\\item\s+(.*)$/);
      if (item) items.push({ entry: currentEntry, dates: currentDates, text: cleanLatex(item[1]) });
    });
    if (items.length) sections.push({ title, items });
  }
  return { basic: { name, email, phone }, sections };
}

// ─── Local storage helpers ──────────────────────────
function metaStorageKey(dir) { return `quick-resume-meta:${dir}`; }
function loadLocalMeta(dir) {
  return new Promise((resolve) => {
    chrome.storage.local.get([metaStorageKey(dir)], (res) => resolve(res[metaStorageKey(dir)] || {}));
  });
}
function saveLocalMeta(dir, data) {
  return new Promise((resolve) => {
    chrome.storage.local.set({ [metaStorageKey(dir)]: data }, () => resolve(true));
  });
}

// ─── Extra rows ─────────────────────────────────────
function collectExtraRows() {
  return [...document.querySelectorAll('#extraList .extra-row')].map((row) => ({
    key: (row.querySelector('[data-k]')?.value || '').trim(),
    value: (row.querySelector('[data-v]')?.value || '').trim(),
  })).filter((item) => item.key || item.value);
}

function addExtraRow(item = { key: '', value: '' }) {
  const row = document.createElement('div');
  row.className = 'extra-row';
  row.innerHTML = `
    <input class="input" data-k placeholder="字段名" value="${escapeHtml(item.key || '')}">
    <input class="input" data-v placeholder="内容" value="${escapeHtml(item.value || '')}">
    <button class="btn btn-sm">删</button>
  `;
  row.querySelector('button').onclick = () => row.remove();
  $('extraList').appendChild(row);
}

// ─── Text builders ──────────────────────────────────
function buildSectionItemText(item, includeBullet = true) {
  item = item || {};
  const lines = [];
  const meta = [item.entry, item.dates].filter(Boolean).join(' · ');
  if (meta) lines.push(meta);
  if (item.text) lines.push(includeBullet ? `• ${item.text}` : item.text);
  return lines.join('\n').trim();
}

function buildSectionText(sec) {
  return [sec.title, ...(sec.items || []).map((item) => buildSectionItemText(item))]
    .filter(Boolean).join('\n').trim();
}

// ─── Render sections (no draft textarea) ────────────
function renderSections(sections) {
  const container = $('resumeSections');
  if (!sections.length) {
    container.innerHTML = '<div class="hint">未解析到可复制条目</div>';
    return;
  }
  container.innerHTML = sections.map((sec, idx) => `
    <div class="section-block" data-section-index="${idx}">
      <div class="section-header">
        <div class="section-name copyable" data-copy-title="${idx}" title="点击复制标题">${escapeHtml(sec.title || `Section ${idx + 1}`)}</div>
        <div class="section-actions">
          <button class="btn btn-sm" data-copy-sec="${idx}" title="复制整节内容">复制</button>
          <button class="btn btn-sm" data-fill-sec="${idx}" title="填入当前文本框">填入</button>
        </div>
      </div>
      ${sec.items.map((item, itemIdx) => `
        <div class="item-row copyable-row" data-copy-row="${idx}:${itemIdx}" title="点击复制整条内容">
          ${item.entry ? `<div class="item-entry copyable" data-copy-entry="${idx}:${itemIdx}" title="点击复制主题">${escapeHtml(item.entry)}</div>` : ''}
          ${item.dates ? `<div class="item-date copyable" data-copy-date-range="${idx}:${itemIdx}" title="点击复制日期范围">${escapeHtml(item.dates)}</div>` : ''}
          ${item.dates ? `
            <div class="item-date-tools">
              <div class="item-date-line"><span class="item-date-label">开始</span><div class="item-date-btns"><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="start" data-copy-date-mode="ym_slash">年/月</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="start" data-copy-date-mode="ym_dash">年-月</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="start" data-copy-date-mode="ymd_slash">年/月/日</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="start" data-copy-date-mode="ymd_dash">年-月-日</button></div></div>
              <div class="item-date-line"><span class="item-date-label">结束</span><div class="item-date-btns"><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="end" data-copy-date-mode="ym_slash">年/月</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="end" data-copy-date-mode="ym_dash">年-月</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="end" data-copy-date-mode="ymd_slash">年/月/日</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="end" data-copy-date-mode="ymd_dash">年-月-日</button></div></div>
            </div>
          ` : ''}
          <div class="item-text copyable" data-copy-text="${idx}:${itemIdx}" title="点击复制内容">• ${escapeHtml(item.text)}</div>
          <div class="item-row-actions">
            <button class="btn btn-sm" data-copy-item="${idx}:${itemIdx}" title="复制整个经历信息">复制</button>
            <button class="btn btn-sm" data-fill-item="${idx}:${itemIdx}" title="填入当前文本框">填入</button>
          </div>
        </div>
      `).join('')}
    </div>
  `).join('');

  // ── Bind copy events ──
  container.querySelectorAll('[data-copy-title]').forEach((el) => {
    el.onclick = () => copyAndToast(sections[Number(el.dataset.copyTitle)]?.title || '', '标题已复制');
  });
  container.querySelectorAll('[data-copy-sec]').forEach((btn) => {
    btn.onclick = () => copyAndToast(buildSectionText(sections[Number(btn.dataset.copySec)]), '本节已复制');
  });
  container.querySelectorAll('[data-copy-entry]').forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); const [s,i] = el.dataset.copyEntry.split(':').map(Number); copyAndToast(sections[s]?.items?.[i]?.entry || '', '已复制'); };
  });
  container.querySelectorAll('[data-copy-date-range]').forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); const [s,i] = el.dataset.copyDateRange.split(':').map(Number); copyAndToast(sections[s]?.items?.[i]?.dates || '', '日期已复制'); };
  });
  container.querySelectorAll('[data-copy-text]').forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); const [s,i] = el.dataset.copyText.split(':').map(Number); copyAndToast(sections[s]?.items?.[i]?.text || '', '已复制'); };
  });
  container.querySelectorAll('[data-copy-item]').forEach((btn) => {
    btn.onclick = (e) => { e.stopPropagation(); const [s,i] = btn.dataset.copyItem.split(':').map(Number); copyAndToast(buildSectionItemText(sections[s]?.items?.[i]), '已复制'); };
  });
  container.querySelectorAll('[data-copy-row]').forEach((row) => {
    row.onclick = (e) => {
      if (e.target.closest('button, .item-entry, .item-date, .item-text, .item-date-tools')) return;
      const [s,i] = row.dataset.copyRow.split(':').map(Number);
      copyAndToast(buildSectionItemText(sections[s]?.items?.[i]), '已复制');
    };
  });
  container.querySelectorAll('.btn-date-copy').forEach((btn) => {
    btn.onclick = async () => {
      const sec = sections[Number(btn.dataset.copyDateSec)];
      const item = sec?.items?.[Number(btn.dataset.copyDateItem)];
      if (!item?.dates) return;
      const range = splitDateRange(item.dates);
      const raw = btn.dataset.copyDatePart === 'end' ? range.end : range.start;
      const text = formatDateByMode(raw, btn.dataset.copyDateMode);
      if (!text) { showToast('该日期为空'); return; }
      copyAndToast(text, '日期已复制');
    };
  });

  // ── Bind fill events (fill into active page input) ──
  container.querySelectorAll('[data-fill-sec]').forEach((btn) => {
    btn.onclick = () => fillIntoPage(buildSectionText(sections[Number(btn.dataset.fillSec)]));
  });
  container.querySelectorAll('[data-fill-item]').forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      const [s,i] = btn.dataset.fillItem.split(':').map(Number);
      fillIntoPage(buildSectionItemText(sections[s]?.items?.[i]));
    };
  });
}

// ─── Version loader ─────────────────────────────────
async function loadVersions() {
  const sel = $('versionSelect');
  try {
    const data = await fetchJSON('/api/gallery');
    const items = data.resumes || [];
    state.versions = items;
    sel.innerHTML = '<option value="">选择简历版本...</option>' +
      items.slice(0, 50).map((item) =>
        `<option value="${escapeHtml(item.dir_name)}">${escapeHtml(item.company || '')} ${escapeHtml(item.role || '')} (${escapeHtml(item.date || '')})</option>`
      ).join('');
    chrome.storage.local.get(['last_version'], (res) => {
      if (res.last_version && [...sel.options].some((o) => o.value === res.last_version)) {
        sel.value = res.last_version;
        openResume(res.last_version);
      }
    });
  } catch (e) {
    sel.innerHTML = '<option value="">加载失败</option>';
  }
}

async function openResume(dirName) {
  if (!dirName) { $('editorView').classList.add('hidden'); state.currentDir = ''; return; }
  setStatus(`加载中...`);
  try {
    const data = await fetchJSON(`/api/editor/tex?dir=${encodeURIComponent(dirName)}`);
    state.currentDir = dirName;
    state.parsed = parseResumeTex(data.content || '');
    chrome.storage.local.set({ last_version: dirName });

    const local = await loadLocalMeta(dirName);
    const basic = local.basic || state.parsed.basic;
    $('basicName').value = basic.name || '';
    $('basicEmail').value = basic.email || '';
    $('basicPhone').value = basic.phone || '';

    $('extraList').innerHTML = '';
    (local.extra || []).forEach(addExtraRow);
    if (!$('extraList').children.length) addExtraRow();

    renderSections(state.parsed.sections || []);
    $('editorView').classList.remove('hidden');
    setStatus('已加载');
  } catch (e) {
    setStatus(`加载失败：${e.message}`, true);
  }
}

// ─── Meta save ──────────────────────────────────────
function collectBasicMeta() {
  return { name: $('basicName').value.trim(), email: $('basicEmail').value.trim(), phone: $('basicPhone').value.trim() };
}

async function saveCurrentMeta() {
  if (!state.currentDir) return;
  const data = { basic: collectBasicMeta(), extra: collectExtraRows() };
  await saveLocalMeta(state.currentDir, data);
}

function buildCopyAllText() {
  if (!state.parsed) return '';
  const basic = collectBasicMeta();
  const extra = collectExtraRows();
  const lines = [`姓名：${basic.name}`, `邮箱：${basic.email}`, `电话：${basic.phone}`];
  if (extra.length) { lines.push('', '自定义信息'); extra.forEach((it) => lines.push(`${it.key}：${it.value}`)); }
  lines.push('');
  (state.parsed.sections || []).forEach((sec) => {
    lines.push(sec.title);
    sec.items.forEach((it) => lines.push(`• ${it.text}`));
    lines.push('');
  });
  return lines.join('\n').trim();
}

// ─── Floating window ────────────────────────────────
function isFloatingWindow() {
  return window.location.search.includes('float=1');
}

async function openAsFloat() {
  const w = await chrome.windows.create({
    url: chrome.runtime.getURL('popup/popup.html?float=1'),
    type: 'popup',
    width: 560,
    height: 720,
    top: 50,
    left: screen.availWidth - 580,
  });
  state.floatWindowId = w.id;
  window.close(); // close the popup
}

// ─── Init ───────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // Floating window mode
  if (isFloatingWindow()) {
    document.body.style.minHeight = 'auto';
    document.body.style.maxHeight = 'none';
    document.body.style.width = '100%';
    $('floatControls').classList.remove('hidden');
    $('opacitySlider').oninput = (e) => {
      const v = e.target.value;
      document.body.style.opacity = v / 100;
      $('opacityVal').textContent = v + '%';
    };
  }

  chrome.storage.local.get(['server_url'], async (res) => {
    state.serverUrl = res.server_url || 'http://localhost:8765';
    $('serverUrl').value = state.serverUrl;
    try {
      await fetchJSON('/api/ext/profile');
      setStatus('已连接');
    } catch {
      setStatus('未连接，请先启动 web/server.py', true);
    }
    await loadVersions();
  });

  $('saveServerBtn').onclick = () => {
    state.serverUrl = $('serverUrl').value.trim() || 'http://localhost:8765';
    chrome.storage.local.set({ server_url: state.serverUrl }, () => showToast('地址已保存'));
  };

  $('refreshBtn').onclick = async () => { await loadVersions(); showToast('已刷新'); };
  $('versionSelect').onchange = (e) => openResume(e.target.value);
  $('floatBtn').onclick = openAsFloat;

  $('addExtraBtn').onclick = () => addExtraRow();
  $('saveMetaBtn').onclick = async () => { await saveCurrentMeta(); showToast('已保存'); };
  $('copyExtraBtn').onclick = async () => {
    const text = collectExtraRows().map((it) => `${it.key}：${it.value}`).join('\n');
    copyAndToast(text, '已复制');
  };
  $('copyAllBtn').onclick = () => copyAndToast(buildCopyAllText(), '已复制全部');

  ['basicName', 'basicEmail', 'basicPhone'].forEach((id) => {
    $(id).addEventListener('input', () => { if (state.currentDir) saveCurrentMeta(); });
  });
});
