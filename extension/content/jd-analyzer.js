window.ResumeFill = window.ResumeFill || {};

ResumeFill.JDAnalyzer = {
  _lastAnalysis: null,

  /** Check if this page looks like a job description */
  isJDPage() {
    return ResumeFill.Detector.isJobDetailPage();
  },

  /** Extract JD text from the current page */
  extractText() {
    const adapter = ResumeFill.Detector.getAdapter();
    return adapter.extractJDText();
  },

  /** Analyze the JD on the current page */
  async analyze() {
    const text = this.extractText();
    if (!text || text.length < 50) {
      return { error: '未检测到有效的 JD 内容' };
    }

    try {
      const result = await ResumeFill.ApiClient.analyzeJD(text);
      this._lastAnalysis = result;
      return result;
    } catch (e) {
      return { error: e.message };
    }
  },

  /** Get the last analysis result */
  getLastAnalysis() {
    return this._lastAnalysis;
  },

  /** Extract job title from the page */
  extractJobTitle() {
    // Try common selectors
    const titleSelectors = [
      'h1.job-title', '.posting-headline h2', '.job-title',
      '[data-automation-id="jobPostingHeader"]',
      '.jobs-details h1', 'h1',
    ];
    for (const sel of titleSelectors) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim()) {
        return el.textContent.trim();
      }
    }
    return document.title || '';
  },

  /** Extract company name from the page */
  extractCompanyName() {
    const selectors = [
      '.company-name', '.posting-headline .company',
      '[data-automation-id="company"]', '.jobs-details .company',
      '.employer-name',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim()) {
        return el.textContent.trim();
      }
    }
    return '';
  }
};
