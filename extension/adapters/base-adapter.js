window.ResumeFill = window.ResumeFill || {};

ResumeFill.BaseAdapter = class BaseAdapter {
  get platform() { return 'generic'; }

  /** Is the current page an application form? */
  isApplicationPage() { return false; }

  /** Is the current page a JD / job details page? */
  isJobDetailPage() { return false; }

  /**
   * Find all fillable fields on the page.
   * Returns: [{ element, label, fieldType, section, selector }]
   */
  findFields() { return []; }

  /** Platform-specific date filling */
  fillDate(el, dateStr) {
    // Default: set value directly
    ResumeFill.Filler.fillInput(el, dateStr);
  }

  /** Platform-specific dropdown filling */
  fillDropdown(el, value) {
    ResumeFill.Filler.fillSelect(el, value);
  }

  /** Extract JD text from the page */
  extractJDText() {
    // Default: get main content area text
    const selectors = [
      '[class*="job-description"]', '[class*="jobDescription"]',
      '[class*="job-details"]', '[class*="jd-"]',
      '[data-testid*="job"]', 'article',
      '.description', '#job-description', '.job-desc',
      'main', '[role="main"]'
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim().length > 100) {
        return el.textContent.trim();
      }
    }
    return document.body.innerText.substring(0, 5000);
  }

  /** Helper: find fields by querying inputs and classifying them */
  _findFieldsByInputs(containerSelector = 'form') {
    const containers = document.querySelectorAll(containerSelector);
    const fields = [];
    const processContainer = (container) => {
      const inputs = container.querySelectorAll('input, select, textarea');
      inputs.forEach(el => {
        if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button') return;
        const label = ResumeFill.DomUtils.getFieldLabel(el);
        const fieldType = ResumeFill.FieldTypes.classify(el);
        const section = ResumeFill.DomUtils.getFieldSection(el);
        const selector = this._buildSelector(el);
        fields.push({ element: el, label, fieldType, section, selector });
      });
    };
    if (containers.length === 0) {
      processContainer(document.body);
    } else {
      containers.forEach(processContainer);
    }
    return fields;
  }

  /** Build a CSS selector for an element (for storage) */
  _buildSelector(el) {
    if (el.id) return `#${el.id}`;
    if (el.name) return `[name="${el.name}"]`;
    const dataId = el.getAttribute('data-automation-id');
    if (dataId) return `[data-automation-id="${dataId}"]`;
    // Fallback: tag + nth-of-type
    const parent = el.parentElement;
    if (!parent) return el.tagName.toLowerCase();
    const siblings = Array.from(parent.children).filter(s => s.tagName === el.tagName);
    const idx = siblings.indexOf(el) + 1;
    return `${el.tagName.toLowerCase()}:nth-of-type(${idx})`;
  }
};
