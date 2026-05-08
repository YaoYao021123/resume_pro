window.ResumeFill = window.ResumeFill || {};

ResumeFill.ApiClient = {
  BASE_URL: 'http://localhost:8765',

  async _fetch(path, options = {}) {
    const url = this.BASE_URL + path;
    try {
      const resp = await fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: resp.statusText }));
        throw new Error(err.error || `HTTP ${resp.status}`);
      }
      return resp.json();
    } catch (e) {
      if (e.message.includes('Failed to fetch') || e.message.includes('NetworkError')) {
        throw new Error('无法连接到本地服务器。请确认 server.py 正在运行 (python3 web/server.py)');
      }
      throw e;
    }
  },

  /** 获取用户 profile */
  getProfile() {
    return this._fetch('/api/ext/profile');
  },

  /** 获取完整填充数据包 */
  getFillData() {
    return this._fetch('/api/ext/fill-data');
  },

  /** JD 关键词分析 */
  analyzeJD(text) {
    return this._fetch('/api/ext/jd-analyze', {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
  },

  /** 记录填充操作 */
  logFill(url, platform, fieldsFilled) {
    return this._fetch('/api/ext/fill-log', {
      method: 'POST',
      body: JSON.stringify({ url, platform, fields_filled: fieldsFilled }),
    });
  },

  /** 记录用户修正 */
  logCorrections(fillId, corrections) {
    return this._fetch('/api/ext/correction', {
      method: 'POST',
      body: JSON.stringify({ fill_id: fillId, corrections }),
    });
  },

  /** 获取字段映射 */
  getFieldMappings(platform) {
    const query = platform ? `?platform=${encodeURIComponent(platform)}` : '';
    return this._fetch(`/api/ext/field-map${query}`);
  },

  /** 更新字段映射 */
  updateFieldMappings(mappings) {
    return this._fetch('/api/ext/field-map', {
      method: 'POST',
      body: JSON.stringify({ mappings }),
    });
  },

  /** 获取历史记录 */
  getHistory() {
    return this._fetch('/api/ext/history');
  },

  /** 创建投递记录 */
  logApplication(data) {
    return this._fetch('/api/applications', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },
};
