window.ResumeFill = window.ResumeFill || {};

ResumeFill.GenericAdapter = class GenericAdapter extends ResumeFill.BaseAdapter {
  get platform() { return 'generic'; }

  isApplicationPage() {
    // Heuristic: page has a form with multiple input fields
    const forms = document.querySelectorAll('form');
    for (const form of forms) {
      const inputs = form.querySelectorAll('input:not([type="hidden"]), select, textarea');
      if (inputs.length >= 3) return true;
    }
    return false;
  }

  isJobDetailPage() {
    // Heuristic: URL contains job-related keywords
    const url = location.href.toLowerCase();
    const path = location.pathname.toLowerCase();
    return /job|career|position|vacancy|opening|posting/.test(url) ||
           /job|career|position|vacancy|opening|posting/.test(path);
  }

  findFields() {
    return this._findFieldsByInputs('form');
  }
};
