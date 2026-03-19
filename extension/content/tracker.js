window.ResumeFill = window.ResumeFill || {};

ResumeFill.Tracker = {
  _corrections: [],
  _tracking: false,

  /** Start tracking corrections on filled fields */
  startTracking(filledFields) {
    if (this._tracking) return;
    this._tracking = true;
    this._corrections = [];

    filledFields.forEach(({ element, fieldType, filledValue, label, selector }) => {
      const handler = () => {
        const newVal = element.value;
        if (newVal !== filledValue) {
          // Check if we already recorded a correction for this field
          const existing = this._corrections.find(c => c.field_name === fieldType && c.selector === selector);
          if (existing) {
            existing.corrected_value = newVal;
          } else {
            this._corrections.push({
              field_name: fieldType,
              field_label: label,
              original_value: filledValue,
              corrected_value: newVal,
              platform: ResumeFill.Detector.getPlatformName(),
              selector,
            });
          }
          // Update sidebar if available
          if (ResumeFill.Sidebar && ResumeFill.Sidebar.updateCorrections) {
            ResumeFill.Sidebar.updateCorrections(this._corrections);
          }
        }
      };

      element.addEventListener('input', handler);
      element.addEventListener('change', handler);
    });

    // Flush corrections before page unload
    window.addEventListener('beforeunload', () => this.flushCorrections());
  },

  /** Get current corrections */
  getCorrections() {
    return this._corrections;
  },

  /** Send corrections to server */
  async flushCorrections() {
    if (this._corrections.length === 0) return;

    const fillId = ResumeFill.Filler.getCurrentFillId();
    if (!fillId) return;

    try {
      await ResumeFill.ApiClient.logCorrections(fillId, this._corrections);

      // Also update field mappings based on corrections
      const mappings = this._corrections.map(c => ({
        platform: c.platform,
        field_selector: c.selector,
        field_label: c.field_label,
        mapped_to: c.field_name,
        confidence: 0.8,
      }));
      await ResumeFill.ApiClient.updateFieldMappings(mappings);

      this._corrections = [];
    } catch (e) {
      console.log('[ResumeFill] Failed to flush corrections:', e.message);
    }
  },

  /** Stop tracking */
  stopTracking() {
    this._tracking = false;
  }
};
