// Namespace
window.ResumeFill = window.ResumeFill || {};

ResumeFill.DomUtils = {
  /**
   * Get the visible label text for an input element.
   * Tries: aria-label, aria-labelledby, associated <label>, preceding text, placeholder
   */
  getFieldLabel(el) {
    // aria-label
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    // aria-labelledby
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const labelEl = document.getElementById(labelledBy);
      if (labelEl) return labelEl.textContent.trim();
    }
    // associated label via for= or wrapping
    if (el.id) {
      const label = document.querySelector(`label[for="${el.id}"]`);
      if (label) return label.textContent.trim();
    }
    const parent = el.closest('label');
    if (parent) return parent.textContent.replace(el.value || '', '').trim();
    // placeholder
    if (el.placeholder) return el.placeholder.trim();
    // name attribute as last resort
    return el.name || el.id || '';
  },

  /**
   * Get all visible form inputs on the page
   */
  getVisibleInputs() {
    const inputs = document.querySelectorAll('input, select, textarea');
    return Array.from(inputs).filter(el => {
      if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button') return false;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return false;
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      return true;
    });
  },

  /**
   * Create a Shadow DOM container for sidebar injection
   */
  createShadowContainer(id) {
    let host = document.getElementById(id);
    if (host) return host.shadowRoot;
    host = document.createElement('div');
    host.id = id;
    document.body.appendChild(host);
    return host.attachShadow({ mode: 'open' });
  },

  /**
   * Wait for an element matching selector to appear
   */
  waitForElement(selector, timeout = 5000) {
    return new Promise((resolve, reject) => {
      const el = document.querySelector(selector);
      if (el) { resolve(el); return; }
      const observer = new MutationObserver(() => {
        const el = document.querySelector(selector);
        if (el) { observer.disconnect(); resolve(el); }
      });
      observer.observe(document.body, { childList: true, subtree: true });
      setTimeout(() => { observer.disconnect(); reject(new Error('Timeout')); }, timeout);
    });
  },

  /**
   * Get the closest section/fieldset context for a field
   */
  getFieldSection(el) {
    const fieldset = el.closest('fieldset');
    if (fieldset) {
      const legend = fieldset.querySelector('legend');
      if (legend) return legend.textContent.trim();
    }
    const section = el.closest('section, [role="group"], .section, .form-section');
    if (section) {
      const heading = section.querySelector('h1, h2, h3, h4, h5, h6, .section-title');
      if (heading) return heading.textContent.trim();
    }
    return '';
  }
};
