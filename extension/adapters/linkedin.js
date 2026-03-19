window.ResumeFill = window.ResumeFill || {};

ResumeFill.LinkedInAdapter = class LinkedInAdapter extends ResumeFill.BaseAdapter {
  get platform() { return 'linkedin'; }

  isApplicationPage() {
    return !!document.querySelector('.jobs-easy-apply-modal, .jobs-apply-form') ||
           !!document.querySelector('[class*="easy-apply"]');
  }

  isJobDetailPage() {
    return !!document.querySelector('.job-view-layout, .jobs-details, .jobs-description');
  }

  findFields() {
    const container = document.querySelector('.jobs-easy-apply-modal, .jobs-apply-form, form');
    if (!container) return [];

    const fields = [];
    const inputs = container.querySelectorAll('input, select, textarea');
    inputs.forEach(el => {
      if (el.type === 'hidden' || el.type === 'submit') return;
      const label = ResumeFill.DomUtils.getFieldLabel(el);
      const fieldType = ResumeFill.FieldTypes.classify(el);
      const section = ResumeFill.DomUtils.getFieldSection(el);
      const selector = this._buildSelector(el);
      fields.push({ element: el, label, fieldType, section, selector });
    });
    return fields;
  }

  fillDropdown(el, value) {
    // LinkedIn uses typeahead dropdowns
    ResumeFill.Filler.fillInput(el, value);
    // Trigger typeahead
    el.dispatchEvent(new Event('focus', { bubbles: true }));
    setTimeout(() => {
      el.dispatchEvent(new Event('input', { bubbles: true }));
      setTimeout(() => {
        const options = document.querySelectorAll(
          '[role="option"], .basic-typeahead__selectable, [class*="typeahead"] li'
        );
        for (const opt of options) {
          if (opt.textContent.trim().toLowerCase().includes(value.toLowerCase())) {
            opt.click();
            return;
          }
        }
      }, 500);
    }, 200);
  }

  extractJDText() {
    const desc = document.querySelector('.jobs-description__content, .jobs-description, [class*="description__text"]');
    if (desc) return desc.textContent.trim();
    return super.extractJDText();
  }
};
