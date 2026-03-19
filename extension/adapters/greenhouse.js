window.ResumeFill = window.ResumeFill || {};

ResumeFill.GreenhouseAdapter = class GreenhouseAdapter extends ResumeFill.BaseAdapter {
  get platform() { return 'greenhouse'; }

  isApplicationPage() {
    return !!document.querySelector('#application_form, .application-form, #grnhse_app');
  }

  isJobDetailPage() {
    return !!document.querySelector('.job-post, #content .body');
  }

  findFields() {
    return this._findFieldsByInputs('#application_form, .application-form, #grnhse_app, form');
  }

  extractJDText() {
    const content = document.querySelector('#content .body, .job-post .body');
    if (content) return content.textContent.trim();
    return super.extractJDText();
  }
};
