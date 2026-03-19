window.ResumeFill = window.ResumeFill || {};

ResumeFill.SmartRecruitersAdapter = class SmartRecruitersAdapter extends ResumeFill.BaseAdapter {
  get platform() { return 'smartrecruiters'; }

  isApplicationPage() {
    return !!document.querySelector('[class*="application"], .js-application-form') ||
           location.pathname.includes('/apply');
  }

  isJobDetailPage() {
    return !!document.querySelector('[class*="jobDescription"], .job-description');
  }

  findFields() {
    const fields = this._findFieldsByInputs('[class*="application"], .js-application-form, form');
    // SmartRecruiters uses React — ensure we find React-controlled inputs
    if (fields.length === 0) {
      return this._findFieldsByInputs('body');
    }
    return fields;
  }

  extractJDText() {
    const desc = document.querySelector('[class*="jobDescription"], .job-description, .job-ad-description');
    if (desc) return desc.textContent.trim();
    return super.extractJDText();
  }
};
