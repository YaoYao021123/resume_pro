window.ResumeFill = window.ResumeFill || {};

ResumeFill.Detector = {
  _adapter: null,

  /** Detect the current ATS platform and return the appropriate adapter */
  detectPlatform() {
    const host = location.hostname.toLowerCase();

    if (host.includes('workday.com') || host.includes('myworkdayjobs.com')) {
      this._adapter = new ResumeFill.WorkdayAdapter();
    } else if (host.includes('greenhouse.io') || host.includes('boards.greenhouse.io')) {
      this._adapter = new ResumeFill.GreenhouseAdapter();
    } else if (host.includes('lever.co') || host.includes('jobs.lever.co')) {
      this._adapter = new ResumeFill.LeverAdapter();
    } else if (host.includes('smartrecruiters.com')) {
      this._adapter = new ResumeFill.SmartRecruitersAdapter();
    } else if (host.includes('linkedin.com')) {
      this._adapter = new ResumeFill.LinkedInAdapter();
    } else {
      this._adapter = new ResumeFill.GenericAdapter();
    }

    return this._adapter;
  },

  /** Get the current adapter (detect if not already done) */
  getAdapter() {
    if (!this._adapter) this.detectPlatform();
    return this._adapter;
  },

  /** Discover all fillable fields on the current page */
  async discoverFields() {
    const adapter = this.getAdapter();
    const fields = adapter.findFields();

    // Enhance with stored field mappings
    try {
      const { mappings } = await ResumeFill.ApiClient.getFieldMappings(adapter.platform);
      if (mappings && mappings.length > 0) {
        const mappingMap = {};
        mappings.forEach(m => { mappingMap[m.field_selector] = m; });

        fields.forEach(f => {
          if (f.fieldType === 'unknown' && mappingMap[f.selector]) {
            f.fieldType = mappingMap[f.selector].mapped_to;
            f.mappingSource = 'learned';
          }
        });
      }
    } catch (e) {
      // Server not available, proceed with local detection only
      console.log('[ResumeFill] Field mapping fetch failed:', e.message);
    }

    return fields;
  },

  /** Check if current page is a job application form */
  isApplicationPage() {
    return this.getAdapter().isApplicationPage();
  },

  /** Check if current page is a job detail / JD page */
  isJobDetailPage() {
    return this.getAdapter().isJobDetailPage();
  },

  /** Get detected platform name */
  getPlatformName() {
    return this.getAdapter().platform;
  },

  /** Extract company name from the current page */
  getCompanyName() {
    // Platform-specific selectors
    const selectors = [
      '[data-automation-id="jobPostingHeader"] [data-automation-id="company"]',
      '.company-name', '.employer-name',
      '[itemprop="hiringOrganization"] [itemprop="name"]',
      '.job-company', '.posting-categories .company',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el?.textContent?.trim()) return el.textContent.trim();
    }
    // Fallback: parse page title ("Role at Company" / "Company - Role")
    const title = document.title || '';
    const atMatch = title.match(/(?:at|@)\s+(.+?)(?:\s*[-|]|$)/i);
    if (atMatch) return atMatch[1].trim();
    const dashMatch = title.match(/^(.+?)\s*[-|]\s*.+/);
    if (dashMatch && dashMatch[1].length < 40) return dashMatch[1].trim();
    // Try hostname
    const host = location.hostname.replace(/^(?:www|jobs|careers)\./, '').split('.')[0];
    return host.charAt(0).toUpperCase() + host.slice(1);
  },

  /** Extract role/job title from the current page */
  getRoleName() {
    const selectors = [
      '[data-automation-id="jobPostingHeader"] h2',
      '.job-title', '.posting-headline h2', '.app-title',
      '[itemprop="title"]', 'h1.t-24', '.jobs-details h1',
      'h1', '.position-title',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      const text = el?.textContent?.trim();
      if (text && text.length < 120) return text;
    }
    // Fallback: parse page title
    const title = document.title || '';
    const atMatch = title.match(/^(.+?)\s+(?:at|@)\s+/i);
    if (atMatch) return atMatch[1].trim();
    return '';
  }
};
