window.ResumeFill = window.ResumeFill || {};

ResumeFill.LeverAdapter = class LeverAdapter extends ResumeFill.BaseAdapter {
  get platform() { return 'lever'; }

  isApplicationPage() {
    return !!document.querySelector('.application-form, .lever-application-form');
  }

  isJobDetailPage() {
    return !!document.querySelector('.posting-headline, .section-wrapper.page-full-width');
  }

  findFields() {
    return this._findFieldsByInputs('.application-form, .lever-application-form, form');
  }

  extractJDText() {
    const sections = document.querySelectorAll('.section-wrapper .section');
    if (sections.length > 0) {
      return Array.from(sections).map(s => s.textContent.trim()).join('\n\n');
    }
    return super.extractJDText();
  }
};
