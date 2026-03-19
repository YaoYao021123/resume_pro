window.ResumeFill = window.ResumeFill || {};

ResumeFill.WorkdayAdapter = class WorkdayAdapter extends ResumeFill.BaseAdapter {
  get platform() { return 'workday'; }

  isApplicationPage() {
    return !!document.querySelector('[data-automation-id="jobApplicationPage"]') ||
           !!document.querySelector('[data-automation-id="applyButton"]') ||
           location.pathname.includes('/apply');
  }

  isJobDetailPage() {
    return !!document.querySelector('[data-automation-id="jobPostingHeader"]') ||
           location.pathname.includes('/job/');
  }

  findFields() {
    const fields = [];
    // Workday uses data-automation-id attributes
    const inputs = document.querySelectorAll(
      '[data-automation-id] input, [data-automation-id] select, [data-automation-id] textarea'
    );
    inputs.forEach(el => {
      if (el.type === 'hidden' || el.type === 'submit') return;
      const automationId = el.closest('[data-automation-id]')?.getAttribute('data-automation-id') || '';
      const label = ResumeFill.DomUtils.getFieldLabel(el) || automationId;
      const fieldType = this._mapWorkdayField(automationId) || ResumeFill.FieldTypes.classify(el);
      const section = ResumeFill.DomUtils.getFieldSection(el);
      fields.push({ element: el, label, fieldType, section, selector: `[data-automation-id="${automationId}"] input` });
    });

    // Also check for standard form inputs not caught above
    const standardFields = this._findFieldsByInputs('form');
    const existingEls = new Set(fields.map(f => f.element));
    standardFields.forEach(f => {
      if (!existingEls.has(f.element)) fields.push(f);
    });

    return fields;
  }

  fillDate(el, dateStr) {
    // Workday often uses custom date pickers, try direct input first
    const container = el.closest('[data-automation-id]');
    if (container) {
      const dateInput = container.querySelector('input[type="text"], input[type="date"]');
      if (dateInput) {
        ResumeFill.Filler.fillInput(dateInput, dateStr);
        return;
      }
    }
    ResumeFill.Filler.fillInput(el, dateStr);
  }

  fillDropdown(el, value) {
    // Workday dropdowns may be custom components
    const container = el.closest('[data-automation-id]');
    if (container) {
      const btn = container.querySelector('button, [role="button"]');
      if (btn) {
        btn.click();
        setTimeout(() => {
          const options = document.querySelectorAll('[role="option"], [role="listbox"] li');
          for (const opt of options) {
            if (opt.textContent.trim().toLowerCase().includes(value.toLowerCase())) {
              opt.click();
              return;
            }
          }
        }, 300);
        return;
      }
    }
    ResumeFill.Filler.fillSelect(el, value);
  }

  extractJDText() {
    const jobDesc = document.querySelector('[data-automation-id="jobPostingDescription"]');
    if (jobDesc) return jobDesc.textContent.trim();
    return super.extractJDText();
  }

  _mapWorkdayField(automationId) {
    const map = {
      'legalNameSection_firstName': 'first_name',
      'legalNameSection_lastName': 'last_name',
      'addressSection_addressLine1': 'address',
      'addressSection_city': 'city',
      'addressSection_countryRegion': 'country',
      'addressSection_postalCode': 'zip_code',
      'phone-number': 'phone',
      'email': 'email',
      'previousWorkerSource': 'referral',
    };
    return map[automationId] || null;
  }
};
