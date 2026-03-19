const state = {
  serverUrl: 'http://localhost:8765',
  currentDir: '',
  parsed: null,
};

const $ = (id) => document.getElementById(id);

function escapeHtml(s = '') {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function setStatus(msg, isError = false) {
  const el = $('statusText');
  el.textContent = msg;
  el.style.color = isError ? '#b24a3e' : '#8a847b';
}

function cleanLatex(text) {
  if (!text) return '';
  return text
    .replace(/\\textbf\{([^}]*)\}/g, '$1')
    .replace(/\\textit\{([^}]*)\}/g, '$1')
    .replace(/\\quad/g, ' ')
    .replace(/\\normalsize/g, '')
    .replace(/\\textperiodcentered/g, '·')
    .replace(/\\%/g, '%')
    .replace(/\\&/g, '&')
    .replace(/\\_/g, '_')
    .replace(/\\#/g, '#')
    .replace(/\\\\/g, '')
    .replace(/\s+/g, ' ')
    .trim();
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

async function copyText(text) {
  if (!text) return;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  ta.remove();
}

async function readTextFile(file) {
  if (!file) return '';
  return await file.text();
}

async function fetchJSON(path, options = {}) {
  const resp = await fetch(`${state.serverUrl}${path}`, options);
  const data = await resp.json();
  if (data && data.error) throw new Error(data.error);
  return data;
}

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
    let currentEntry = '';
    let currentDates = '';
    const items = [];
    lines.forEach((lineRaw) => {
      const line = lineRaw.trim();
      const dated = line.match(/^\\datedsubsection\{(.+?)\}\{(.+?)\}$/);
      if (dated) {
        currentEntry = cleanLatex(dated[1]);
        currentDates = cleanLatex(dated[2]);
        return;
      }
      const item = line.match(/^\\item\s+(.*)$/);
      if (item) {
        items.push({ entry: currentEntry, dates: currentDates, text: cleanLatex(item[1]) });
      }
    });
    if (items.length) sections.push({ title, items });
  }
  return { basic: { name, email, phone }, sections };
}

function metaStorageKey(dir) {
  return `quick-resume-meta:${dir}`;
}

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

function collectSectionDrafts() {
  return [...document.querySelectorAll('#resumeSections [data-section-draft]')].reduce((acc, textarea) => {
    const value = (textarea.value || '').trim();
    if (value) acc[textarea.dataset.sectionDraft] = value;
    return acc;
  }, {});
}

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
    .filter(Boolean)
    .join('\n')
    .trim();
}

function appendToSectionDraft(sectionIndex, text, replace = false) {
  const textarea = document.querySelector(`[data-section-draft="${sectionIndex}"]`);
  if (!textarea || !text) return false;
  const current = (textarea.value || '').trim();
  const next = replace ? text.trim() : [current, text.trim()].filter(Boolean).join('\n');
  textarea.value = next.replace(/\n{3,}/g, '\n\n').trim();
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  return true;
}

async function copyWithStatus(text, status) {
  if (!text) return;
  await copyText(text);
  setStatus(status || '已复制');
}

function renderSections(sections, sectionDrafts = {}) {
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
          <button class="btn btn-sm" data-copy-sec="${idx}">复制本节</button>
          <button class="btn btn-sm" data-fill-sec="${idx}">填入文本框</button>
        </div>
      </div>
      <textarea class="input textarea section-draft-box" data-section-draft="${idx}" placeholder="本节对应文本框：点击“填入文本框”可把对应内容块汇总到这里">${escapeHtml(sectionDrafts[idx] || '')}</textarea>
      <div class="section-draft-toolbar">
        <button class="btn btn-sm" data-copy-draft="${idx}">复制文本框</button>
        <button class="btn btn-sm" data-clear-draft="${idx}">清空文本框</button>
      </div>
      ${sec.items.map((item, itemIdx) => `
        <div class="item-row copyable-row" data-copy-row="${idx}:${itemIdx}" title="点击复制整条内容">
          <div class="item-row-actions">
            <button class="btn btn-sm" data-copy-item="${idx}:${itemIdx}">复制条目</button>
            <button class="btn btn-sm" data-fill-item="${idx}:${itemIdx}">填入文本框</button>
          </div>
          ${item.entry ? `<div class="item-entry copyable" data-copy-entry="${idx}:${itemIdx}" title="点击复制主题">${escapeHtml(item.entry)}</div>` : ''}
          ${item.dates ? `<div class="item-date copyable" data-copy-date-range="${idx}:${itemIdx}" title="点击复制日期范围">${escapeHtml(item.dates)}</div>` : ''}
          ${item.dates ? `
            <div class="item-date-tools">
              <div class="item-date-line"><span class="item-date-label">开始</span><div class="item-date-btns"><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="start" data-copy-date-mode="ym_slash">年/月</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="start" data-copy-date-mode="ym_dash">年-月</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="start" data-copy-date-mode="ymd_slash">年/月/日</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="start" data-copy-date-mode="ymd_dash">年-月-日</button></div></div>
              <div class="item-date-line"><span class="item-date-label">结束</span><div class="item-date-btns"><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="end" data-copy-date-mode="ym_slash">年/月</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="end" data-copy-date-mode="ym_dash">年-月</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="end" data-copy-date-mode="ymd_slash">年/月/日</button><button class="btn btn-sm btn-date-copy" data-copy-date-sec="${idx}" data-copy-date-item="${itemIdx}" data-copy-date-part="end" data-copy-date-mode="ymd_dash">年-月-日</button></div></div>
            </div>
          ` : ''}
          <div class="item-text copyable" data-copy-text="${idx}:${itemIdx}" title="点击复制内容">• ${escapeHtml(item.text)}</div>
        </div>
      `).join('')}
    </div>
  `).join('');

  [...container.querySelectorAll('[data-copy-title]')].forEach((el) => {
    el.onclick = async () => {
      const sec = sections[Number(el.dataset.copyTitle)];
      await copyWithStatus(sec?.title || '', '标题已复制');
    };
  });

  [...container.querySelectorAll('[data-copy-sec]')].forEach((btn) => {
    btn.onclick = async () => {
      const sec = sections[Number(btn.dataset.copySec)];
      await copyWithStatus(buildSectionText(sec), '本节已复制');
    };
  });

  [...container.querySelectorAll('[data-fill-sec]')].forEach((btn) => {
    btn.onclick = () => {
      const sec = sections[Number(btn.dataset.fillSec)];
      if (appendToSectionDraft(btn.dataset.fillSec, buildSectionText(sec), true)) {
        setStatus('本节已填入对应文本框');
      }
    };
  });

  [...container.querySelectorAll('[data-copy-draft]')].forEach((btn) => {
    btn.onclick = async () => {
      const textarea = container.querySelector(`[data-section-draft="${btn.dataset.copyDraft}"]`);
      await copyWithStatus(textarea?.value?.trim() || '', '文本框内容已复制');
    };
  });

  [...container.querySelectorAll('[data-clear-draft]')].forEach((btn) => {
    btn.onclick = () => {
      const textarea = container.querySelector(`[data-section-draft="${btn.dataset.clearDraft}"]`);
      if (!textarea) return;
      textarea.value = '';
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      setStatus('文本框已清空');
    };
  });

  [...container.querySelectorAll('[data-copy-entry]')].forEach((el) => {
    el.onclick = async (event) => {
      event.stopPropagation();
      const [secIdx, itemIdx] = el.dataset.copyEntry.split(':').map(Number);
      await copyWithStatus(sections[secIdx]?.items?.[itemIdx]?.entry || '', '主题已复制');
    };
  });

  [...container.querySelectorAll('[data-copy-date-range]')].forEach((el) => {
    el.onclick = async (event) => {
      event.stopPropagation();
      const [secIdx, itemIdx] = el.dataset.copyDateRange.split(':').map(Number);
      await copyWithStatus(sections[secIdx]?.items?.[itemIdx]?.dates || '', '日期范围已复制');
    };
  });

  [...container.querySelectorAll('[data-copy-text]')].forEach((el) => {
    el.onclick = async (event) => {
      event.stopPropagation();
      const [secIdx, itemIdx] = el.dataset.copyText.split(':').map(Number);
      await copyWithStatus(sections[secIdx]?.items?.[itemIdx]?.text || '', '内容已复制');
    };
  });

  [...container.querySelectorAll('[data-copy-item]')].forEach((btn) => {
    btn.onclick = async (event) => {
      event.stopPropagation();
      const [secIdx, itemIdx] = btn.dataset.copyItem.split(':').map(Number);
      await copyWithStatus(buildSectionItemText(sections[secIdx]?.items?.[itemIdx]), '条目已复制');
    };
  });

  [...container.querySelectorAll('[data-fill-item]')].forEach((btn) => {
    btn.onclick = (event) => {
      event.stopPropagation();
      const [secIdx, itemIdx] = btn.dataset.fillItem.split(':').map(Number);
      const text = buildSectionItemText(sections[secIdx]?.items?.[itemIdx]);
      if (appendToSectionDraft(secIdx, text)) {
        setStatus('内容块已填入对应文本框');
      }
    };
  });

  [...container.querySelectorAll('[data-copy-row]')].forEach((row) => {
    row.onclick = async (event) => {
      if (event.target.closest('button, textarea, .item-entry, .item-date, .item-text, .item-date-tools')) return;
      const [secIdx, itemIdx] = row.dataset.copyRow.split(':').map(Number);
      await copyWithStatus(buildSectionItemText(sections[secIdx]?.items?.[itemIdx]), '条目已复制');
    };
  });

  [...container.querySelectorAll('.btn-date-copy')].forEach((btn) => {
    btn.onclick = async () => {
      const sec = sections[Number(btn.dataset.copyDateSec)];
      const item = sec?.items?.[Number(btn.dataset.copyDateItem)];
      if (!item || !item.dates) return;
      const range = splitDateRange(item.dates);
      const raw = btn.dataset.copyDatePart === 'end' ? range.end : range.start;
      const text = formatDateByMode(raw, btn.dataset.copyDateMode);
      if (!text) {
        setStatus('该日期为空', true);
        return;
      }
      await copyText(text);
      setStatus('日期已复制');
    };
  });

  [...container.querySelectorAll('[data-section-draft]')].forEach((textarea) => {
    textarea.addEventListener('input', () => {
      if (state.currentDir) saveCurrentMeta();
    });
  });
}

async function loadDraft() {
  try {
    const data = await fetchJSON('/api/ext/draft');
    $('jdText').value = data.jd || '';
    $('interviewText').value = data.interview || '';
  } catch {
    // ignore
  }
}

async function loadHistory() {
  const list = $('historyList');
  list.innerHTML = '<div class="hint">加载中...</div>';
  try {
    const data = await fetchJSON('/api/gallery');
    const items = data.resumes || [];
    if (!items.length) {
      list.innerHTML = '<div class="hint">暂无历史简历</div>';
      return;
    }
    list.innerHTML = items.slice(0, 30).map((item) => `
      <div class="history-item">
        <div>
          <div class="history-title">${escapeHtml(item.company || '')} ${escapeHtml(item.role || '')}</div>
          <div class="history-meta">${escapeHtml(item.date || '')} · ${escapeHtml(item.dir_name)}</div>
        </div>
        <button class="btn btn-sm" data-open="${escapeHtml(item.dir_name)}">打开</button>
      </div>
    `).join('');

    [...list.querySelectorAll('[data-open]')].forEach((btn) => {
      btn.onclick = () => openResume(btn.dataset.open);
    });
  } catch (e) {
    list.innerHTML = `<div class="hint">加载失败：${e.message}</div>`;
  }
}

async function openResume(dirName) {
  setStatus(`打开中：${dirName}`);
  try {
    const data = await fetchJSON(`/api/editor/tex?dir=${encodeURIComponent(dirName)}`);
    state.currentDir = dirName;
    state.parsed = parseResumeTex(data.content || '');

    const local = await loadLocalMeta(dirName);
    const basic = local.basic || state.parsed.basic;
    $('basicName').value = basic.name || '';
    $('basicEmail').value = basic.email || '';
    $('basicPhone').value = basic.phone || '';

    $('extraList').innerHTML = '';
    (local.extra || []).forEach(addExtraRow);
    if (!$('extraList').children.length) addExtraRow();

    renderSections(state.parsed.sections || [], local.sectionDrafts || {});
    $('editorTitle').textContent = dirName;
    $('mainPanels').classList.add('hidden');
    $('editorView').classList.remove('hidden');
    setStatus('已进入简历窗口');
  } catch (e) {
    setStatus(`打开失败：${e.message}`, true);
  }
}

function collectBasicMeta() {
  return {
    name: $('basicName').value.trim(),
    email: $('basicEmail').value.trim(),
    phone: $('basicPhone').value.trim(),
  };
}

async function saveCurrentMeta() {
  if (!state.currentDir) return;
  const data = { basic: collectBasicMeta(), extra: collectExtraRows(), sectionDrafts: collectSectionDrafts() };
  await saveLocalMeta(state.currentDir, data);
  setStatus('已保存快速填写信息');
}

function buildCopyAllText() {
  if (!state.parsed) return '';
  const basic = collectBasicMeta();
  const extra = collectExtraRows();
  const lines = [];
  lines.push(`姓名：${basic.name}`);
  lines.push(`邮箱：${basic.email}`);
  lines.push(`电话：${basic.phone}`);
  if (extra.length) {
    lines.push('');
    lines.push('自定义信息');
    extra.forEach((it) => lines.push(`${it.key}：${it.value}`));
  }
  lines.push('');
  (state.parsed.sections || []).forEach((sec) => {
    lines.push(sec.title);
    sec.items.forEach((it) => lines.push(`• ${it.text}`));
    lines.push('');
  });
  return lines.join('\n').trim();
}

document.addEventListener('DOMContentLoaded', async () => {
  chrome.storage.local.get(['server_url'], async (res) => {
    state.serverUrl = res.server_url || 'http://localhost:8765';
    $('serverUrl').value = state.serverUrl;
    try {
      await fetchJSON('/api/ext/profile');
      setStatus('已连接主程序');
    } catch {
      setStatus('未连接主程序，请先启动 web/server.py', true);
    }
    await loadDraft();
    await loadHistory();
  });

  $('saveServerBtn').onclick = () => {
    state.serverUrl = $('serverUrl').value.trim() || 'http://localhost:8765';
    chrome.storage.local.set({ server_url: state.serverUrl }, () => setStatus('服务地址已保存'));
  };

  $('refreshBtn').onclick = async () => { await loadHistory(); setStatus('已刷新'); };

  $('jdFile').addEventListener('change', async (e) => {
    const text = await readTextFile(e.target.files?.[0]);
    if (text) $('jdText').value = text;
  });
  $('interviewFile').addEventListener('change', async (e) => {
    const text = await readTextFile(e.target.files?.[0]);
    if (text) $('interviewText').value = text;
  });

  $('saveDraftBtn').onclick = async () => {
    try {
      await fetchJSON('/api/ext/draft', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jd: $('jdText').value, interview: $('interviewText').value }),
      });
      setStatus('已传回主程序');
    } catch (e) {
      setStatus(`传回失败：${e.message}`, true);
    }
  };

  $('openMainBtn').onclick = async () => {
    $('saveDraftBtn').click();
    chrome.tabs.create({ url: `${state.serverUrl}/#generate` });
  };

  $('backBtn').onclick = () => {
    $('editorView').classList.add('hidden');
    $('mainPanels').classList.remove('hidden');
    state.currentDir = '';
  };

  $('addExtraBtn').onclick = () => addExtraRow();
  $('saveMetaBtn').onclick = saveCurrentMeta;
  $('copyExtraBtn').onclick = async () => {
    const text = collectExtraRows().map((it) => `${it.key}：${it.value}`).join('\n');
    await copyText(text);
    setStatus('已复制自定义信息');
  };
  $('copyAllBtn').onclick = async () => {
    await copyText(buildCopyAllText());
    setStatus('已复制当前简历内容');
  };

  ['basicName', 'basicEmail', 'basicPhone'].forEach((id) => {
    $(id).addEventListener('input', () => { if (state.currentDir) saveCurrentMeta(); });
  });
});
