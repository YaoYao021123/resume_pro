window.ResumeFill = window.ResumeFill || {};

ResumeFill.Filler = {
  _fillData: null,
  _filledFields: [], // Track what we filled for correction tracking

  /** Set value on an input element, triggering React/Angular/Vue updates */
  fillInput(el, value) {
    if (!el || value === undefined || value === null) return false;
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value'
    )?.set;
    const nativeTextareaValueSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value'
    )?.set;

    const setter = el.tagName === 'TEXTAREA' ? nativeTextareaValueSetter : nativeInputValueSetter;
    if (setter) {
      setter.call(el, value);
    } else {
      el.value = value;
    }

    el.dispatchEvent(new Event('focus', { bubbles: true }));
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return true;
  },

  /** Fill a select/dropdown element */
  fillSelect(el, value) {
    if (!el || !value) return false;
    const valueLower = value.toLowerCase();

    // Try matching option text or value
    for (const opt of el.options) {
      if (opt.text.toLowerCase().includes(valueLower) ||
          opt.value.toLowerCase().includes(valueLower)) {
        el.value = opt.value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }
    }
    return false;
  },

  /** Get fill data from server (with caching) */
  async loadFillData() {
    // Try cache first
    const cached = await ResumeFill.Storage.getCachedFillData();
    if (cached) {
      this._fillData = cached;
      return cached;
    }

    try {
      const data = await ResumeFill.ApiClient.getFillData();
      this._fillData = data;
      await ResumeFill.Storage.setCachedFillData(data);
      return data;
    } catch (e) {
      console.error('[ResumeFill] Failed to load fill data:', e);
      return null;
    }
  },

  /** Map field type to the corresponding value from fill data */
  getValueForField(fieldType) {
    if (!this._fillData || !this._fillData.profile) return null;
    const p = this._fillData.profile;
    const basic = p.basic || {};
    const edu = (p.education || [])[0] || {};

    const mapping = {
      first_name: basic.name_en ? basic.name_en.split(' ')[0] : '',
      last_name: basic.name_en ? basic.name_en.split(' ').slice(1).join(' ') : '',
      full_name: basic.name_zh || basic.name_en || '',
      email: basic.email || '',
      phone: basic.phone || '',
      school: edu.school || '',
      degree: edu.degree || '',
      major: edu.major || '',
      gpa: edu.gpa || '',
      linkedin_url: basic.linkedin || '',
      github_url: basic.github || '',
      website: basic.website || '',
    };

    return mapping[fieldType] || null;
  },

  /** Fill all discovered fields */
  async fillAllFields(fields) {
    if (!this._fillData) {
      await this.loadFillData();
    }
    if (!this._fillData) return { filled: 0, skipped: 0, total: fields.length };

    const adapter = ResumeFill.Detector.getAdapter();
    this._filledFields = [];
    let filled = 0;
    let skipped = 0;

    for (const field of fields) {
      const { element, fieldType } = field;

      // Skip unknown fields, file uploads, and already-filled fields
      if (fieldType === 'unknown' || fieldType === 'resume_upload' || fieldType === 'cover_letter') {
        skipped++;
        continue;
      }
      if (element.value && element.value.trim()) {
        skipped++;
        continue;
      }

      const value = this.getValueForField(fieldType);
      if (!value) {
        skipped++;
        continue;
      }

      let success = false;
      if (element.tagName === 'SELECT') {
        success = adapter.fillDropdown
          ? (adapter.fillDropdown(element, value), true)
          : this.fillSelect(element, value);
      } else if (field.fieldType.includes('date')) {
        adapter.fillDate(element, value);
        success = true;
      } else {
        success = this.fillInput(element, value);
      }

      if (success) {
        filled++;
        this._filledFields.push({
          element,
          fieldType,
          filledValue: value,
          label: field.label,
          selector: field.selector,
        });
      } else {
        skipped++;
      }
    }

    // Log the fill operation
    try {
      const platform = ResumeFill.Detector.getPlatformName();
      const result = await ResumeFill.ApiClient.logFill(location.href, platform, filled);
      this._currentFillId = result.fill_id;

      // Auto-create application record
      try {
        const company = ResumeFill.Detector.getCompanyName?.() || '';
        const role = ResumeFill.Detector.getRoleName?.() || '';
        // Get selected resume version from storage
        const stored = await new Promise(r => chrome.storage.local.get(['last_version'], r));
        await ResumeFill.ApiClient.logApplication({
          company,
          role,
          url: location.href,
          platform,
          fill_id: result.fill_id,
          resume_dir: stored.last_version || '',
          status: '投递',
        });
      } catch (appErr) {
        console.log('[ResumeFill] Failed to log application:', appErr.message);
      }
    } catch (e) {
      console.log('[ResumeFill] Failed to log fill:', e.message);
    }

    return { filled, skipped, total: fields.length };
  },

  /** Get the list of fields that were filled (for tracking) */
  getFilledFields() {
    return this._filledFields;
  },

  /** Get current fill ID */
  getCurrentFillId() {
    return this._currentFillId || null;
  }
};
