window.ResumeFill = window.ResumeFill || {};

ResumeFill.FieldTypes = {
  // Pattern map: fieldType -> regex that matches label/name/placeholder
  PATTERNS: {
    first_name: /first.?name|名(?!称)|given.?name|prénom/i,
    last_name: /last.?name|姓(?!名)|family.?name|surname/i,
    full_name: /full.?name|姓名|your.?name|^name$/i,
    email: /e-?mail|邮箱|电子邮件/i,
    phone: /phone|tel(?:ephone)?|手机|电话|mobile/i,
    school: /school|university|college|院校|学校|institution/i,
    degree: /degree|学位|学历|education.?level/i,
    major: /major|专业|field.?of.?study|concentration/i,
    gpa: /\bgpa\b|绩点|成绩|grade.?point/i,
    company: /company|employer|公司|organization|雇主/i,
    job_title: /job.?title|role|职位|岗位|position/i,
    start_date: /start.?date|开始日期|from.?date|begin/i,
    end_date: /end.?date|结束日期|to.?date/i,
    linkedin_url: /linkedin/i,
    github_url: /github/i,
    website: /website|portfolio|个人网站|personal.?site/i,
    resume_upload: /resume|cv|简历|attach/i,
    cover_letter: /cover.?letter|求职信/i,
    address: /address|地址|street/i,
    city: /\bcity\b|城市/i,
    state: /\bstate\b|province|省/i,
    zip_code: /zip|postal|邮编/i,
    country: /country|国家/i,
    gender: /gender|性别/i,
    ethnicity: /ethnic|race|民族/i,
    veteran: /veteran|退伍/i,
    disability: /disability|残疾/i,
    salary: /salary|compensation|薪资|期望薪资/i,
    availability: /avail|start|入职|到岗/i,
    referral: /referral|refer|推荐人|内推/i,
    summary: /summary|自我介绍|about|简介/i,
  },

  /**
   * Classify a field based on its label, name, id, placeholder
   * Returns the fieldType string or 'unknown'
   */
  classify(el) {
    const texts = [
      ResumeFill.DomUtils.getFieldLabel(el),
      el.name || '',
      el.id || '',
      el.placeholder || '',
      el.getAttribute('data-automation-id') || '',
      el.getAttribute('autocomplete') || '',
    ].join(' ');

    for (const [fieldType, pattern] of Object.entries(this.PATTERNS)) {
      if (pattern.test(texts)) return fieldType;
    }

    // Autocomplete hint fallback
    const ac = el.getAttribute('autocomplete') || '';
    const acMap = {
      'given-name': 'first_name', 'family-name': 'last_name',
      'name': 'full_name', 'email': 'email', 'tel': 'phone',
      'organization': 'company', 'street-address': 'address',
      'postal-code': 'zip_code', 'country': 'country',
    };
    if (acMap[ac]) return acMap[ac];

    return 'unknown';
  },

  /**
   * Check if a field type is an education-related field
   */
  isEducationField(fieldType) {
    return ['school', 'degree', 'major', 'gpa'].includes(fieldType);
  },

  /**
   * Check if a field type is a work-experience field
   */
  isWorkField(fieldType) {
    return ['company', 'job_title', 'start_date', 'end_date'].includes(fieldType);
  },

  /**
   * Check if a field type is personal info
   */
  isPersonalField(fieldType) {
    return ['first_name', 'last_name', 'full_name', 'email', 'phone',
            'linkedin_url', 'github_url', 'website', 'address', 'city',
            'state', 'zip_code', 'country'].includes(fieldType);
  }
};
