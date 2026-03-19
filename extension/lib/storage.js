window.ResumeFill = window.ResumeFill || {};

ResumeFill.Storage = {
  /** Get value from chrome.storage.local */
  async get(key, defaultValue = null) {
    return new Promise((resolve) => {
      if (typeof chrome !== 'undefined' && chrome.storage) {
        chrome.storage.local.get([key], (result) => {
          resolve(result[key] !== undefined ? result[key] : defaultValue);
        });
      } else {
        // Fallback to localStorage for development
        const val = localStorage.getItem(`resumefill_${key}`);
        resolve(val ? JSON.parse(val) : defaultValue);
      }
    });
  },

  /** Set value in chrome.storage.local */
  async set(key, value) {
    return new Promise((resolve) => {
      if (typeof chrome !== 'undefined' && chrome.storage) {
        chrome.storage.local.set({ [key]: value }, resolve);
      } else {
        localStorage.setItem(`resumefill_${key}`, JSON.stringify(value));
        resolve();
      }
    });
  },

  /** Remove key from storage */
  async remove(key) {
    return new Promise((resolve) => {
      if (typeof chrome !== 'undefined' && chrome.storage) {
        chrome.storage.local.remove([key], resolve);
      } else {
        localStorage.removeItem(`resumefill_${key}`);
        resolve();
      }
    });
  },

  /** Cache fill data with TTL (default 5 minutes) */
  async getCachedFillData(ttlMs = 300000) {
    const cached = await this.get('fill_data_cache');
    if (cached && (Date.now() - cached.timestamp) < ttlMs) {
      return cached.data;
    }
    return null;
  },

  async setCachedFillData(data) {
    await this.set('fill_data_cache', { data, timestamp: Date.now() });
  },

  /** Store server URL preference */
  async getServerUrl() {
    return this.get('server_url', 'http://localhost:8765');
  },

  async setServerUrl(url) {
    await this.set('server_url', url);
  }
};
