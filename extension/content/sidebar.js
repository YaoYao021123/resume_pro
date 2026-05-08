window.ResumeFill = window.ResumeFill || {};

ResumeFill.Sidebar = {
  _shadow: null,
  _container: null,
  _visible: false,

  /** Initialize the sidebar (inject into page) */
  init() {
    if (this._shadow) return;

    const host = document.createElement('div');
    host.id = 'resume-fill-sidebar-host';
    host.style.cssText = 'all: initial; position: fixed; top: 0; right: 0; z-index: 2147483647; height: 100vh; pointer-events: none;';
    document.body.appendChild(host);

    this._shadow = host.attachShadow({ mode: 'closed' });
    this._container = document.createElement('div');
    this._container.className = 'rf-sidebar rf-hidden';
    this._container.innerHTML = this._getTemplate();

    // Inject styles
    const style = document.createElement('style');
    style.textContent = this._getStyles();
    this._shadow.appendChild(style);
    this._shadow.appendChild(this._container);

    this._bindEvents();
  },

  /** Show the sidebar */
  show() {
    if (!this._container) this.init();
    this._container.classList.remove('rf-hidden');
    this._visible = true;
  },

  /** Hide the sidebar */
  hide() {
    if (this._container) {
      this._container.classList.add('rf-hidden');
      this._visible = false;
    }
  },

  /** Toggle visibility */
  toggle() {
    if (this._visible) this.hide();
    else this.show();
  },

  /** Update the platform info display */
  updatePlatform(platform, fieldCount) {
    const platformEl = this._shadow.querySelector('.rf-platform');
    const fieldCountEl = this._shadow.querySelector('.rf-field-count');
    if (platformEl) platformEl.textContent = platform;
    if (fieldCountEl) fieldCountEl.textContent = fieldCount;
  },

  /** Update JD analysis results */
  updateJDAnalysis(analysis) {
    const container = this._shadow.querySelector('.rf-jd-keywords');
    if (!container) return;

    if (analysis.error) {
      container.innerHTML = `<span class="rf-error">${analysis.error}</span>`;
      return;
    }

    const tags = [
      ...(analysis.tech_stack || []).map(t => `<span class="rf-tag rf-tag-tech">${t}</span>`),
      ...(analysis.roles || []).map(r => `<span class="rf-tag rf-tag-role">${r}</span>`),
      ...(analysis.soft_skills || []).map(s => `<span class="rf-tag rf-tag-soft">${s}</span>`),
    ];

    let html = tags.join(' ');
    if (analysis.education_req) html += `<div class="rf-req">学历: ${analysis.education_req}</div>`;
    if (analysis.experience_req) html += `<div class="rf-req">经验: ${analysis.experience_req}</div>`;

    container.innerHTML = html || '<span class="rf-muted">未检测到关键词</span>';
  },

  /** Update fill status */
  updateFillStatus(result, fields) {
    const container = this._shadow.querySelector('.rf-fill-status');
    if (!container) return;

    let html = `<div class="rf-summary">已填充 ${result.filled} / ${result.total} 个字段</div>`;
    fields.forEach(f => {
      const icon = f.fieldType === 'unknown' ? '⚪' : (result.filled > 0 ? '✅' : '⚠️');
      html += `<div class="rf-field-item">${icon} ${f.label || f.fieldType}</div>`;
    });

    container.innerHTML = html;
  },

  /** Update corrections display */
  updateCorrections(corrections) {
    const container = this._shadow.querySelector('.rf-corrections');
    if (!container) return;

    if (corrections.length === 0) {
      container.innerHTML = '<span class="rf-muted">暂无修正</span>';
      return;
    }

    container.innerHTML = corrections.map(c =>
      `<div class="rf-correction-item">
        <span class="rf-correction-label">${c.field_label || c.field_name}</span>
        <span class="rf-correction-change">"${c.original_value}" → "${c.corrected_value}"</span>
      </div>`
    ).join('');
  },

  /** Show a status message */
  setStatus(message, type = 'info') {
    const statusEl = this._shadow.querySelector('.rf-status');
    if (statusEl) {
      statusEl.textContent = message;
      statusEl.className = `rf-status rf-status-${type}`;
    }
  },

  _getTemplate() {
    return `
      <div class="rf-header">
        <span class="rf-title">Resume Fill 智能网申助手</span>
        <button class="rf-close-btn">✕</button>
      </div>
      <div class="rf-body">
        <div class="rf-status rf-status-info">就绪</div>

        <div class="rf-section">
          <div class="rf-section-title">平台检测</div>
          <div>检测到: <strong class="rf-platform">—</strong></div>
          <div>可填充字段: <strong class="rf-field-count">0</strong> 个</div>
        </div>

        <div class="rf-section rf-actions">
          <button class="rf-btn rf-btn-primary rf-fill-btn">一键填充</button>
          <button class="rf-btn rf-analyze-btn">分析 JD</button>
        </div>

        <div class="rf-section">
          <div class="rf-section-title">JD 关键词</div>
          <div class="rf-jd-keywords"><span class="rf-muted">点击"分析 JD"开始</span></div>
        </div>

        <div class="rf-section">
          <div class="rf-section-title">填充状态</div>
          <div class="rf-fill-status"><span class="rf-muted">尚未填充</span></div>
        </div>

        <div class="rf-section">
          <div class="rf-section-title">修正记录</div>
          <div class="rf-corrections"><span class="rf-muted">暂无修正</span></div>
        </div>
      </div>
    `;
  },

  _getStyles() {
    return `
      :host { all: initial; }
      .rf-sidebar {
        pointer-events: auto;
        position: fixed;
        top: 0;
        right: 0;
        width: 320px;
        height: 100vh;
        background: #fff;
        box-shadow: -2px 0 12px rgba(0,0,0,0.15);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        font-size: 13px;
        color: #333;
        display: flex;
        flex-direction: column;
        transition: transform 0.3s ease;
        overflow: hidden;
      }
      .rf-hidden {
        transform: translateX(100%);
        pointer-events: none;
      }
      .rf-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 12px 16px;
        background: #4A90D9;
        color: #fff;
        flex-shrink: 0;
      }
      .rf-title { font-weight: 600; font-size: 14px; }
      .rf-close-btn {
        background: none; border: none; color: #fff;
        font-size: 16px; cursor: pointer; padding: 4px 8px;
      }
      .rf-close-btn:hover { opacity: 0.8; }
      .rf-body {
        flex: 1;
        overflow-y: auto;
        padding: 12px 16px;
      }
      .rf-section {
        margin-bottom: 16px;
        padding-bottom: 12px;
        border-bottom: 1px solid #eee;
      }
      .rf-section:last-child { border-bottom: none; }
      .rf-section-title {
        font-weight: 600;
        font-size: 12px;
        color: #666;
        text-transform: uppercase;
        margin-bottom: 8px;
        letter-spacing: 0.5px;
      }
      .rf-status {
        padding: 8px 12px;
        border-radius: 6px;
        margin-bottom: 12px;
        font-size: 12px;
      }
      .rf-status-info { background: #E3F2FD; color: #1565C0; }
      .rf-status-success { background: #E8F5E9; color: #2E7D32; }
      .rf-status-error { background: #FFEBEE; color: #C62828; }
      .rf-status-warning { background: #FFF3E0; color: #EF6C00; }
      .rf-actions { display: flex; gap: 8px; }
      .rf-btn {
        padding: 8px 16px;
        border: 1px solid #ddd;
        border-radius: 6px;
        cursor: pointer;
        font-size: 13px;
        background: #fff;
        flex: 1;
        transition: all 0.2s;
      }
      .rf-btn:hover { background: #f5f5f5; }
      .rf-btn-primary {
        background: #4A90D9;
        color: #fff;
        border-color: #4A90D9;
      }
      .rf-btn-primary:hover { background: #3A7BC8; }
      .rf-btn:disabled { opacity: 0.5; cursor: not-allowed; }
      .rf-tag {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 11px;
        margin: 2px;
      }
      .rf-tag-tech { background: #E3F2FD; color: #1565C0; }
      .rf-tag-role { background: #F3E5F5; color: #7B1FA2; }
      .rf-tag-soft { background: #E8F5E9; color: #2E7D32; }
      .rf-req { margin-top: 6px; font-size: 12px; color: #666; }
      .rf-muted { color: #999; font-size: 12px; }
      .rf-error { color: #C62828; font-size: 12px; }
      .rf-field-item { padding: 2px 0; font-size: 12px; }
      .rf-summary { font-weight: 600; margin-bottom: 6px; }
      .rf-correction-item {
        padding: 4px 0;
        font-size: 12px;
        border-bottom: 1px solid #f5f5f5;
      }
      .rf-correction-label { font-weight: 500; }
      .rf-correction-change { color: #666; display: block; font-size: 11px; }
    `;
  },

  _bindEvents() {
    // Close button
    const closeBtn = this._shadow.querySelector('.rf-close-btn');
    if (closeBtn) {
      closeBtn.addEventListener('click', () => this.hide());
    }

    // Fill button
    const fillBtn = this._shadow.querySelector('.rf-fill-btn');
    if (fillBtn) {
      fillBtn.addEventListener('click', async () => {
        fillBtn.disabled = true;
        this.setStatus('正在填充...', 'info');

        try {
          await ResumeFill.Filler.loadFillData();
          const fields = await ResumeFill.Detector.discoverFields();
          const result = await ResumeFill.Filler.fillAllFields(fields);

          this.updateFillStatus(result, fields);
          this.setStatus(`填充完成: ${result.filled} 个字段`, 'success');

          // Start tracking corrections
          ResumeFill.Tracker.startTracking(ResumeFill.Filler.getFilledFields());
        } catch (e) {
          this.setStatus(`填充失败: ${e.message}`, 'error');
        } finally {
          fillBtn.disabled = false;
        }
      });
    }

    // Analyze button
    const analyzeBtn = this._shadow.querySelector('.rf-analyze-btn');
    if (analyzeBtn) {
      analyzeBtn.addEventListener('click', async () => {
        analyzeBtn.disabled = true;
        this.setStatus('正在分析 JD...', 'info');

        try {
          const result = await ResumeFill.JDAnalyzer.analyze();
          this.updateJDAnalysis(result);
          this.setStatus('JD 分析完成', 'success');
        } catch (e) {
          this.setStatus(`分析失败: ${e.message}`, 'error');
        } finally {
          analyzeBtn.disabled = false;
        }
      });
    }
  }
};

// ─── Content Script Initialization ─────────────────────────────
// This runs when all content scripts are loaded (document_idle)

(function initResumeFill() {
  // Track last focused input for "fill into" feature
  ResumeFill._lastFocusedInput = null;
  document.addEventListener('focusin', (e) => {
    const el = e.target;
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) {
      ResumeFill._lastFocusedInput = el;
    }
  }, true);

  // Detect platform on load
  const adapter = ResumeFill.Detector.detectPlatform();
  const platform = adapter.platform;
  const isApp = adapter.isApplicationPage();
  const isJD = adapter.isJobDetailPage();

  // If on a relevant page, initialize sidebar and show badge
  if (isApp || isJD) {
    ResumeFill.Sidebar.init();

    // Notify the background script to show badge
    if (typeof chrome !== 'undefined' && chrome.runtime) {
      chrome.runtime.sendMessage({
        action: 'showBadge',
        text: isApp ? 'F' : 'JD',
        color: isApp ? '#4CAF50' : '#FF9800',
      }).catch(() => {});
    }

    // Auto-detect fields if it's an application page
    if (isApp) {
      ResumeFill.Detector.discoverFields().then(fields => {
        ResumeFill.Sidebar.updatePlatform(platform, fields.length);
      }).catch(() => {});
    }

    // Auto-analyze JD if it's a job detail page
    if (isJD) {
      ResumeFill.Sidebar.updatePlatform(platform, 0);
      ResumeFill.JDAnalyzer.analyze().then(result => {
        ResumeFill.Sidebar.updateJDAnalysis(result);
      }).catch(() => {});
    }
  }

  // Listen for messages from popup / service worker
  if (typeof chrome !== 'undefined' && chrome.runtime) {
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (message.action === 'getPageInfo') {
        sendResponse({
          platform,
          isApplication: isApp,
          isJobDetail: isJD,
        });
        return false;
      }

      if (message.action === 'toggleSidebar') {
        ResumeFill.Sidebar.init();
        ResumeFill.Sidebar.toggle();
        sendResponse({ success: true });
        return false;
      }

      if (message.action === 'quickFill') {
        (async () => {
          try {
            ResumeFill.Sidebar.init();
            ResumeFill.Sidebar.show();
            ResumeFill.Sidebar.setStatus('正在填充...', 'info');

            await ResumeFill.Filler.loadFillData();
            const fields = await ResumeFill.Detector.discoverFields();
            const result = await ResumeFill.Filler.fillAllFields(fields);

            ResumeFill.Sidebar.updateFillStatus(result, fields);
            ResumeFill.Sidebar.setStatus(`填充完成: ${result.filled} 个字段`, 'success');
            ResumeFill.Tracker.startTracking(ResumeFill.Filler.getFilledFields());

            sendResponse({ success: true, result });
          } catch (e) {
            ResumeFill.Sidebar.setStatus(`填充失败: ${e.message}`, 'error');
            sendResponse({ success: false, error: e.message });
          }
        })();
        return true; // async response
      }

      if (message.action === 'analyzeJD') {
        (async () => {
          try {
            ResumeFill.Sidebar.init();
            ResumeFill.Sidebar.show();
            const result = await ResumeFill.JDAnalyzer.analyze();
            ResumeFill.Sidebar.updateJDAnalysis(result);
            sendResponse({ success: true, result });
          } catch (e) {
            sendResponse({ success: false, error: e.message });
          }
        })();
        return true;
      }

      if (message.action === 'insertText') {
        // Insert text into the last focused input/textarea on the page
        const el = ResumeFill._lastFocusedInput || document.activeElement;
        if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) {
          if (el.isContentEditable) {
            el.focus();
            document.execCommand('insertText', false, message.text);
          } else {
            el.focus();
            const start = el.selectionStart || 0;
            el.value = el.value.substring(0, start) + message.text + el.value.substring(el.selectionEnd || start);
            el.selectionStart = el.selectionEnd = start + message.text.length;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
          }
          sendResponse({ success: true });
        } else {
          sendResponse({ success: false, error: 'no input focused' });
        }
        return false;
      }

      if (message.action === 'pageLoaded') {
        // Re-detect on navigation (SPA)
        sendResponse({ success: true });
        return false;
      }
    });
  }
})();
