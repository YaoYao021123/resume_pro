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
  }
};
