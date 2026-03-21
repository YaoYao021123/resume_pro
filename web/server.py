#!/usr/bin/env python3
"""Resume Generator Pro — 本地 Web 数据管理服务器

启动方式：python3 web/server.py [--port 8765]
自动打开浏览器 → http://localhost:8765
"""

import argparse
import cgi
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import socket
import subprocess as _sp
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
import zipfile
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / 'data'
WEB_DIR = PROJECT_ROOT / 'web'

# 多人档案管理支持
import sys
_tools_parent = str(PROJECT_ROOT)
if _tools_parent not in sys.path:
    sys.path.insert(0, _tools_parent)

from tools.person_manager import (
    get_active_person_id,
    get_person_profile_path,
    get_person_experiences_dir,
    get_person_work_materials_dir,
    get_person_output_dir,
    list_persons,
    create_person,
    set_active_person,
    delete_person,
    is_multi_person_mode,
)
from tools.migrate_to_multi_person import maybe_migrate as _maybe_migrate
from tools import gen_log
from tools.ext_db import (
    log_fill as ext_log_fill,
    log_correction as ext_log_correction,
    get_field_mappings as ext_get_field_mappings,
    update_field_mapping as ext_update_field_mapping,
    get_corrections_summary as ext_get_corrections_summary,
    get_fill_history as ext_get_fill_history,
)
from tools.model_config import (
    get_model_config,
    save_model_config,
    load_local_env,
)

load_local_env()


def _profile_path():
    return get_person_profile_path(get_active_person_id())

def _experiences_dir():
    return get_person_experiences_dir(get_active_person_id())

def _work_materials_dir():
    return get_person_work_materials_dir(get_active_person_id())

def _output_dir():
    return get_person_output_dir(get_active_person_id())

def _extra_info_path():
    return _profile_path().parent / 'extra_info.json'

def _ext_draft_path():
    return _profile_path().parent / 'ext_draft.json'

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB
ENTITLEMENT_TIMEOUT_SEC = float(os.getenv('AUTH_BILLING_TIMEOUT_SEC', '2.0'))


def _auth_billing_enabled() -> bool:
    return os.getenv('AUTH_BILLING_ENFORCE', '0').strip().lower() in {'1', 'true', 'yes', 'on'}


def _auth_billing_base_url() -> str:
    return os.getenv('AUTH_BILLING_BASE_URL', 'http://127.0.0.1:8080').rstrip('/')


def _auth_billing_shared_secret() -> str:
    return os.getenv('AUTH_BILLING_SERVICE_SECRET', '')


def _header_get(headers, key: str, default=''):
    if headers is None:
        return default
    if hasattr(headers, 'get'):
        return headers.get(key, default)
    return default


def _extract_auth_context(headers) -> tuple[str | None, str | None]:
    validated = str(_header_get(headers, 'X-Auth-Validated', '')).strip().lower()
    user_id = str(_header_get(headers, 'X-Auth-User-Id', '')).strip()
    if validated not in {'1', 'true', 'yes'} or not user_id:
        return None, 'AUTH_REQUIRED'
    return user_id, None


def _sign_service_request(
    *,
    action: str,
    user_id: str,
    request_id: str,
    reservation_id: str = '',
    idempotency_key: str = '',
    result: str = '',
    timestamp: str | None = None,
) -> dict[str, str]:
    ts = timestamp or str(int(time.time()))
    secret = _auth_billing_shared_secret()
    if not secret:
        raise RuntimeError('AUTH_BILLING_SERVICE_SECRET is required when auth billing is enabled')
    message = f'{action}|{user_id}|{request_id}|{reservation_id}|{idempotency_key}|{result}|{ts}'
    signature = hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
    return {
        'X-Auth-User-Id': user_id,
        'X-Service-Request-Id': request_id,
        'X-Service-Reservation-Id': reservation_id,
        'X-Service-Idempotency-Key': idempotency_key,
        'X-Service-Result': result,
        'X-Service-Timestamp': ts,
        'X-Service-Signature': signature,
    }


def _call_auth_billing(path: str, payload: dict, headers: dict[str, str], timeout: float = ENTITLEMENT_TIMEOUT_SEC) -> dict:
    url = f'{_auth_billing_base_url()}{path}'
    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        method='POST',
        headers={
            'Content-Type': 'application/json',
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body.decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8') if exc.fp else ''
        detail = ''
        if body:
            try:
                detail = json.loads(body).get('detail', '')
            except Exception:
                detail = body
        raise RuntimeError(f'HTTP_{exc.code}:{detail}')
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise TimeoutError(str(exc)) from exc


def _create_pending_finalize_job(job: dict):
    jobs_path = DATA_DIR / 'pending_finalize_jobs.json'
    payload = []
    if jobs_path.exists():
        try:
            payload = json.loads(jobs_path.read_text(encoding='utf-8'))
            if not isinstance(payload, list):
                payload = []
        except (OSError, json.JSONDecodeError):
            payload = []
    payload.append(job)
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _default_reserve(*, user_id: str, person_id: str, request_id: str, mode: str) -> dict:
    headers = _sign_service_request(action='reserve', user_id=user_id, request_id=request_id)
    return _call_auth_billing(
        '/entitlements/reserve',
        {
            'mode': mode,
            'request_id': request_id,
            'person_id': person_id,
            'user_id': user_id,
        },
        headers=headers,
    )


def _default_finalize(*, user_id: str, request_id: str, reservation_id: str, result: str, idempotency_key: str) -> dict:
    headers = _sign_service_request(
        action='finalize',
        user_id=user_id,
        request_id=request_id,
        reservation_id=reservation_id,
        idempotency_key=idempotency_key,
        result=result,
    )
    return _call_auth_billing(
        '/entitlements/finalize',
        {
            'reservation_id': reservation_id,
            'result': result,
            'idempotency_key': idempotency_key,
            'request_id': request_id,
            'user_id': user_id,
        },
        headers=headers,
    )


def _run_generate_with_entitlement(
    *,
    data: dict,
    headers,
    active_person_id: str,
    generate_func,
    reserve_func=_default_reserve,
    finalize_func=_default_finalize,
    enqueue_func=_create_pending_finalize_job,
    enforce_auth_billing: bool | None = None,
) -> tuple[int, dict]:
    jd_text = data.get('jd', '').strip()
    interview_text = data.get('interview', '').strip()
    company_override = data.get('company', '').strip()
    role_override = data.get('role', '').strip()
    prefer_ai = bool(data.get('prefer_ai'))
    mode = str(data.get('mode', 'platform_key')).strip() or 'platform_key'

    if not jd_text:
        return 400, {'error': '请输入 JD 内容'}

    if enforce_auth_billing is None:
        enforce_auth_billing = _auth_billing_enabled()

    requested_person_id = str(data.get('person_id', '')).strip()
    person_id = (requested_person_id if enforce_auth_billing else '') or active_person_id
    user_id = None
    request_id = f'gen_{uuid4().hex}'
    reservation_id = None

    if enforce_auth_billing:
        user_id, auth_error = _extract_auth_context(headers)
        if auth_error:
            return 401, {'error': auth_error, 'error_code': auth_error}

        try:
            reserve_decision = reserve_func(
                user_id=user_id,
                person_id=person_id,
                request_id=request_id,
                mode=mode,
            )
        except TimeoutError:
            return 503, {'error': 'entitlement reserve timeout', 'error_code': 'ENTITLEMENT_RESERVE_TIMEOUT'}
        except Exception as exc:
            err = str(exc)
            if 'PERSON_NOT_AUTHORIZED' in err:
                return 403, {'error': 'PERSON_NOT_AUTHORIZED', 'error_code': 'PERSON_NOT_AUTHORIZED'}
            if 'QUOTA_EXCEEDED' in err:
                code = 'QUOTA_EXCEEDED'
                if 'QUOTA_EXCEEDED_MONTHLY_FREE' in err:
                    code = 'QUOTA_EXCEEDED_MONTHLY_FREE'
                elif 'QUOTA_EXCEEDED_WEEKLY_MEMBER' in err:
                    code = 'QUOTA_EXCEEDED_WEEKLY_MEMBER'
                return 403, {'error': code, 'error_code': code}
            return 503, {'error': err, 'error_code': 'ENTITLEMENT_RESERVE_FAILED'}

        if not reserve_decision.get('allow', False):
            code = reserve_decision.get('error_code') or 'ENTITLEMENT_DENIED'
            status = 403 if code in {'PERSON_NOT_AUTHORIZED', 'QUOTA_EXCEEDED_MONTHLY_FREE', 'QUOTA_EXCEEDED_WEEKLY_MEMBER'} else 403
            return status, {'error': code, 'error_code': code}
        reservation_id = reserve_decision.get('reservation_id')

    finalize_result = 'success'
    generation_exception = False
    try:
        result = generate_func(
            jd_text,
            interview_text,
            company=company_override,
            role=role_override,
            person_id=person_id,
            prefer_ai=prefer_ai,
        )
        if isinstance(result, dict) and not result.get('success', True):
            finalize_result = 'fail'
    except Exception:
        result = {'success': False, 'error': '生成失败'}
        finalize_result = 'fail'
        generation_exception = True

    if enforce_auth_billing and mode == 'platform_key' and reservation_id and user_id:
        idempotency_key = f'finalize_{finalize_result}_{uuid4().hex}'
        try:
            finalize_func(
                user_id=user_id,
                request_id=request_id,
                reservation_id=reservation_id,
                result=finalize_result,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            enqueue_func(
                {
                    'request_id': request_id,
                    'user_id': user_id,
                    'person_id': person_id,
                    'mode': mode,
                    'reservation_id': reservation_id,
                    'idempotency_key': idempotency_key,
                    'result': finalize_result,
                    'status': 'pending',
                    'retry_count': 0,
                    'last_error': str(exc),
                    'created_at': datetime.utcnow().isoformat() + 'Z',
                }
            )

    if generation_exception:
        return 500, result
    return 200, result


# ─── Profile.md 解析 ───────────────────────────────────────────

def parse_profile() -> dict:
    """将 profile.md 解析为 JSON 结构"""
    PROFILE_PATH = _profile_path()
    if not PROFILE_PATH.exists():
        return {}

    text = _profile_path().read_text(encoding='utf-8')
    lines = text.split('\n')

    data = {
        'basic': {
            'name_zh': '', 'name_en': '', 'email': '', 'phone': '',
            'linkedin': '', 'github': '', 'website': ''
        },
        'education': [],
        'awards': [],
        'skills': {'tech': '', 'software': '', 'languages': ''},
        'projects': [],
        'publications': [],
        'directions': {'primary': '', 'secondary': ''}
    }

    section = ''
    subsection = ''
    in_code_block = False
    current_edu = None
    code_lines = []

    for line in lines:
        stripped = line.strip()

        # 检测 section
        if stripped.startswith('## ') and not stripped.startswith('### '):
            section = stripped[3:].strip()
            subsection = ''
            in_code_block = False
            continue

        # 检测 subsection
        if stripped.startswith('### '):
            subsection = stripped[4:].strip()
            in_code_block = False

            # 新的教育条目
            if section == '教育背景' and '学历' in subsection:
                if current_edu:
                    data['education'].append(current_edu)
                current_edu = {
                    'school': '', 'degree': '', 'major': '', 'department': '',
                    'time_start': '', 'time_end': '', 'gpa': '', 'rank': '', 'courses': ''
                }

            if section == '项目经历' and '项目' in subsection:
                # Each project is parsed from its code block
                pass
            continue

        # 代码块边界
        if stripped.startswith('```'):
            if in_code_block:
                # 代码块结束，处理收集的行
                _process_code_block(data, section, subsection, current_edu, code_lines)
                code_lines = []
            in_code_block = not in_code_block
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        # 课程行（在教育背景中，不在代码块中的特殊处理）
        # 课程通常在代码块中，已在 _process_code_block 处理

    # 收尾最后一个教育条目
    if current_edu:
        data['education'].append(current_edu)

    # 确保至少有一个教育条目
    if not data['education']:
        data['education'].append({
            'school': '', 'degree': '', 'major': '', 'department': '',
            'time_start': '', 'time_end': '', 'gpa': '', 'rank': '', 'courses': ''
        })

    return data


def _process_code_block(data, section, subsection, current_edu, code_lines):
    """处理一个代码块中的内容"""
    block_text = '\n'.join(code_lines)

    if section == '基本信息':
        kv = _parse_kv_block(code_lines)
        # 第一个代码块：必填信息
        if '姓名（中文）' in kv or '姓名(中文)' in kv:
            data['basic']['name_zh'] = kv.get('姓名（中文）', kv.get('姓名(中文)', ''))
            data['basic']['name_en'] = kv.get('姓名（英文）', kv.get('姓名(英文)', ''))
            data['basic']['email'] = kv.get('邮箱', '')
            data['basic']['phone'] = kv.get('电话', '')
        # 第二个代码块：可选信息
        if 'LinkedIn（可选）' in kv or 'LinkedIn(可选)' in kv or 'LinkedIn' in kv:
            data['basic']['linkedin'] = kv.get('LinkedIn（可选）', kv.get('LinkedIn(可选)', kv.get('LinkedIn', '')))
            data['basic']['github'] = kv.get('GitHub（可选）', kv.get('GitHub(可选)', kv.get('GitHub', '')))
            data['basic']['website'] = kv.get('个人网站（可选）', kv.get('个人网站(可选)', kv.get('个人网站', '')))

    elif section == '教育背景' and current_edu is not None:
        kv = _parse_kv_block(code_lines)
        if '学校' in kv:
            current_edu['school'] = kv.get('学校', '')
            current_edu['degree'] = kv.get('学历', '')
            current_edu['major'] = kv.get('专业', '')
            current_edu['department'] = kv.get('学院', '')
            time_str = kv.get('时间', '')
            if '--' in time_str:
                parts = time_str.split('--')
                current_edu['time_start'] = parts[0].strip()
                current_edu['time_end'] = parts[1].strip()
            current_edu['gpa'] = kv.get('GPA', '')
            current_edu['rank'] = kv.get('排名', '')
        else:
            # 课程代码块
            courses = block_text.strip()
            if courses and not courses.startswith('['):
                current_edu['courses'] = courses

    elif section == '获奖情况':
        for line in code_lines:
            line = line.strip()
            if not line or line.startswith('['):
                continue
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 3:
                data['awards'].append({
                    'name': parts[0], 'issuer': parts[1], 'date': parts[2]
                })
            elif len(parts) == 2:
                data['awards'].append({
                    'name': parts[0], 'issuer': parts[1], 'date': ''
                })

    elif section == '技能':
        content = block_text.strip()
        if content.startswith('['):
            content = ''
        if '编程' in subsection or '技术' in subsection:
            data['skills']['tech'] = content
        elif '软件' in subsection:
            data['skills']['software'] = content
        elif '外语' in subsection or '语言' in subsection:
            data['skills']['languages'] = content

    elif section == '求职方向标签':
        kv = _parse_kv_block(code_lines)
        data['directions']['primary'] = kv.get('主要方向', '')
        data['directions']['secondary'] = kv.get('兴趣方向（可选）', kv.get('兴趣方向', ''))

    elif section == '项目经历':
        kv = _parse_kv_block(code_lines)
        if '项目名称' in kv:
            data['projects'].append({
                'name': kv.get('项目名称', ''),
                'role': kv.get('角色', ''),
                'time_start': '',
                'time_end': '',
                'desc': kv.get('描述', ''),
                'tags': kv.get('标签', ''),
            })
            time_str = kv.get('时间', '')
            if '--' in time_str:
                parts = time_str.split('--')
                data['projects'][-1]['time_start'] = parts[0].strip()
                data['projects'][-1]['time_end'] = parts[1].strip()

    elif section == '论文发表':
        for line in code_lines:
            line = line.strip()
            if not line or line.startswith('['):
                continue
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 5:
                data['publications'].append({
                    'title': parts[0], 'authors': parts[1], 'venue': parts[2], 'year': parts[3], 'description': parts[4]
                })
            elif len(parts) >= 4:
                data['publications'].append({
                    'title': parts[0], 'authors': parts[1], 'venue': parts[2], 'year': parts[3], 'description': ''
                })
            elif len(parts) == 3:
                data['publications'].append({
                    'title': parts[0], 'authors': parts[1], 'venue': parts[2], 'year': '', 'description': ''
                })


def _parse_kv_block(lines):
    """解析 key：value 格式的代码块"""
    result = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 支持中文冒号和英文冒号
        for sep in ['：', ':']:
            if sep in line:
                key, value = line.split(sep, 1)
                key = key.strip()
                value = value.strip()
                # 去除占位符：[YOUR_XXX]、[X.X/4.0]（可选）、前 X% 等
                if re.match(r'^\[YOUR_.*\]', value):
                    value = ''
                elif re.match(r'^\[.*\]', value):
                    value = ''
                elif re.match(r'^前 X%', value):
                    value = ''
                elif re.match(r'^\[硕士', value):
                    value = ''
                result[key] = value
                break
    return result


# ─── Profile.md 写入 ───────────────────────────────────────────

def render_profile(data: dict) -> str:
    """将 JSON 数据渲染为 profile.md 格式"""
    b = data.get('basic', {})
    edu_list = data.get('education', [])
    awards = data.get('awards', [])
    skills = data.get('skills', {})
    dirs = data.get('directions', {})

    sections = []

    # 头部
    sections.append("""# 个人信息档案

> **填写说明**
> - 用 `[YOUR_XXX]` 标记的字段必须替换为你的真实信息
> - 用 `<!-- 注释 -->` 包裹的是说明文字，填完后可删除
> - 课程、技能等列表：尽量多填，生成简历时会按岗位自动筛选

---""")

    # 基本信息
    sections.append(f"""## 基本信息

```
姓名（中文）：{b.get('name_zh', '')}
姓名（英文）：{b.get('name_en', '')}
邮箱：{b.get('email', '')}
电话：{b.get('phone', '')}
```

<!-- 可选：LinkedIn、GitHub、个人网站 -->
```
LinkedIn（可选）：{b.get('linkedin', '')}
GitHub（可选）：{b.get('github', '')}
个人网站（可选）：{b.get('website', '')}
```

---""")

    # 教育背景
    edu_sections = ['## 教育背景\n\n<!-- 按时间倒序填写，最新在前。可添加多段。 -->']

    for i, edu in enumerate(edu_list):
        label = '最新' if i == 0 else ''
        label_suffix = f'（{label}）' if label else ''
        time_str = f"{edu.get('time_start', '')} -- {edu.get('time_end', '')}"

        edu_sections.append(f"""### 学历 {i + 1}{label_suffix}

```
学校：{edu.get('school', '')}
学历：{edu.get('degree', '')}
专业：{edu.get('major', '')}
学院：{edu.get('department', '')}
时间：{time_str}
GPA：{edu.get('gpa', '')}
排名：{edu.get('rank', '')}
```

**主修课程{('（尽量多填，生成时按岗位筛选 4-5 门）' if i == 0 else '')}：**

```
{edu.get('courses', '')}
```""")

    sections.append('\n\n'.join(edu_sections) + '\n\n---')

    # 获奖情况
    award_lines = []
    for a in awards:
        if a.get('name'):
            parts = [a.get('name', ''), a.get('issuer', ''), a.get('date', '')]
            award_lines.append(' | '.join(parts))

    award_text = '\n'.join(award_lines) if award_lines else ''
    sections.append(f"""## 获奖情况

<!-- 每行一条，格式：奖项名称 | 颁发机构 | 时间 -->
<!-- 示例：全国大学生数学建模竞赛一等奖 | 教育部 | 2023/11 -->

```
{award_text}
```

---""")

    # 技能
    sections.append(f"""## 技能

### 编程与技术

<!-- 列举你掌握的编程语言、框架、工具 -->
<!-- 示例：Python (Pandas, NumPy, Sklearn), SQL, R, Stata, Excel VBA -->

```
{skills.get('tech', '')}
```

### 软件工具

<!-- 列举常用软件 -->
<!-- 示例：Tableau, Power BI, Figma, Notion, Salesforce -->

```
{skills.get('software', '')}
```

### 外语水平

<!-- 格式：语言 - 证书/考试成绩（水平描述） -->
<!-- 示例：英语 - IELTS 7.5（流利）；日语 - JLPT N2 -->

```
{skills.get('languages', '')}
```

---""")

    # 项目经历
    projects = data.get('projects', [])
    if projects:
        proj_sections = ['## 项目经历\n\n<!-- 按时间倒序填写 -->']
        for i, p in enumerate(projects):
            time_str = f"{p.get('time_start', '')} -- {p.get('time_end', '')}"
            proj_sections.append(f"""### 项目 {i + 1}

```
项目名称：{p.get('name', '')}
角色：{p.get('role', '')}
时间：{time_str}
描述：{p.get('desc', '')}
标签：{p.get('tags', '')}
```""")
        sections.append('\n\n'.join(proj_sections) + '\n\n---')
    else:
        sections.append("""## 项目经历

<!-- 按时间倒序填写 -->
<!-- 格式：项目名称 | 角色 | 时间 | 描述 | 标签 -->

---""")

    # 论文发表
    pubs = data.get('publications', [])
    pub_lines = []
    for pub in pubs:
        if pub.get('title'):
            parts = [pub.get('title',''), pub.get('authors',''), pub.get('venue',''), pub.get('year',''), pub.get('description','')]
            pub_lines.append(' | '.join(parts))
    pub_text = '\n'.join(pub_lines) if pub_lines else ''
    sections.append(f"""## 论文发表

<!-- 每行一条，格式：论文标题 | 作者 | 期刊/会议 | 年份 | 内容描述 -->

```
{pub_text}
```

---""")

    # 求职方向
    sections.append(f"""## 求职方向标签

<!-- 填写你的目标岗位类型，用于辅助匹配判断 -->
<!-- 可多选，以逗号分隔 -->
<!-- 常见方向：产品经理、运营、数据分析、技术开发、金融分析、咨询、投资、市场营销、人力资源 -->

```
主要方向：{dirs.get('primary', '')}
兴趣方向（可选）：{dirs.get('secondary', '')}
```
""")

    return '\n\n'.join(sections)


def parse_experience_file(filepath: Path) -> dict:
    """解析单个经历 .md 文件为 JSON 结构"""
    if not filepath.exists():
        return {}

    text = filepath.read_text(encoding='utf-8')
    lines = text.split('\n')

    result = {
        'company': '', 'city': '', 'department': '', 'role': '',
        'time_start': '', 'time_end': '', 'tags': '', 'notes': '',
        'work_items': []
    }

    section = ''
    in_code_block = False
    code_lines = []
    work_item_title = ''

    for line in lines:
        stripped = line.strip()

        if stripped.startswith('# ') and not stripped.startswith('## '):
            # Title line like "# 公司名 - 角色"
            continue

        if stripped.startswith('## '):
            section = stripped[3:].strip()
            continue

        if stripped.startswith('### '):
            sub = stripped[4:].strip()
            if '工作内容' in sub:
                # Extract title after colon if present
                if '：' in sub:
                    work_item_title = sub.split('：', 1)[1].strip()
                elif ':' in sub:
                    work_item_title = sub.split(':', 1)[1].strip()
                else:
                    work_item_title = sub
            continue

        if stripped.startswith('```'):
            if in_code_block:
                block_text = '\n'.join(code_lines).strip()
                if section == '基本信息':
                    kv = _parse_kv_block(code_lines)
                    result['company'] = kv.get('公司/机构', kv.get('公司', ''))
                    result['city'] = kv.get('所在城市', '')
                    result['department'] = kv.get('部门', '')
                    result['role'] = kv.get('职位', '')
                    time_str = kv.get('时间', '')
                    if '--' in time_str:
                        parts = time_str.split('--')
                        result['time_start'] = parts[0].strip()
                        result['time_end'] = parts[1].strip()
                elif section == '标签':
                    result['tags'] = block_text
                elif '工作内容' in section or work_item_title:
                    if block_text:
                        result['work_items'].append({
                            'title': work_item_title or block_text[:50],
                            'desc': block_text
                        })
                    work_item_title = ''
                elif '补充说明' in section:
                    result['notes'] = block_text
                code_lines = []
            in_code_block = not in_code_block
            continue

        if in_code_block:
            code_lines.append(line)

    return result


# ─── 文件名安全处理 ─────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """清理文件名，去除危险字符"""
    name = name.replace('\x00', '').replace('..', '').replace('/', '').replace('\\', '')
    name = name.strip('. ')
    if not name:
        name = 'unnamed'
    return name


# ─── 文件上传处理 ───────────────────────────────────────────────

def get_next_experience_number() -> int:
    """获取下一个经历文件编号"""
    existing = sorted(_experiences_dir().glob('[0-9][0-9]_*.md'))
    if existing:
        try:
            return int(existing[-1].name[:2]) + 1
        except ValueError:
            pass
    return 1


def handle_md_upload(content: bytes, filename: str, company_name: str) -> str:
    """处理 .md 文件上传 → data/experiences/"""
    _experiences_dir().mkdir(parents=True, exist_ok=True)

    if re.match(r'^\d{2}_', filename):
        target = _experiences_dir() / sanitize_filename(filename)
    else:
        num = get_next_experience_number()
        safe_name = sanitize_filename(company_name or filename.replace('.md', ''))
        target = _experiences_dir() / f'{num:02d}_{safe_name}.md'

    target.write_bytes(content)
    return str(target.relative_to(PROJECT_ROOT))


def handle_pdf_upload(content: bytes, filename: str, company_name: str) -> str:
    """处理 .pdf 文件上传 → data/work_materials/{company}/"""
    if not company_name:
        raise ValueError("PDF 文件上传需要填写关联公司名称")

    materials_dir = _work_materials_dir() / sanitize_filename(company_name)
    materials_dir.mkdir(parents=True, exist_ok=True)

    target = materials_dir / sanitize_filename(filename)
    target.write_bytes(content)
    return str(target.relative_to(PROJECT_ROOT))


def handle_zip_upload(content: bytes, filename: str, company_name: str) -> list:
    """处理 .zip 文件上传，按类型分流"""
    if not company_name:
        raise ValueError("ZIP 文件上传需要填写关联公司名称")

    results = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if '..' in info.filename or info.filename.startswith('/'):
                raise ValueError(f"ZIP 中包含不安全路径: {info.filename}")
            if info.file_size > MAX_UPLOAD_SIZE:
                raise ValueError(f"ZIP 中文件过大: {info.filename}")

        for info in zf.infolist():
            if info.is_dir():
                continue

            file_content = zf.read(info.filename)
            basename = Path(info.filename).name

            if not basename or basename.startswith('.'):
                continue

            if basename.lower().endswith('.md'):
                result = handle_md_upload(file_content, basename, company_name)
            else:
                result = handle_pdf_upload(file_content, basename, company_name)

            results.append(result)

    return results


# ─── 经历表单录入 ───────────────────────────────────────────────

def render_experience_md(data: dict) -> str:
    """将经历表单 JSON 渲染为 _template.md 格式的 Markdown"""
    company = data.get('company', '')
    city = data.get('city', '')
    dept = data.get('department', '')
    role = data.get('role', '')
    time_start = data.get('time_start', '')
    time_end = data.get('time_end', '')
    tags = data.get('tags', '')
    work_items = data.get('work_items', [])
    notes = data.get('notes', '')

    lines = []
    lines.append(f'# {company} - {role}')
    lines.append('')
    lines.append('---')
    lines.append('')
    lines.append('## 基本信息')
    lines.append('')
    lines.append('```')
    lines.append(f'公司/机构：{company}')
    lines.append(f'所在城市：{city}')
    lines.append(f'部门：{dept}')
    lines.append(f'职位：{role}')
    lines.append(f'时间：{time_start} -- {time_end}')
    lines.append('```')
    lines.append('')
    lines.append('---')
    lines.append('')
    lines.append('## 标签')
    lines.append('')
    lines.append('```')
    lines.append(tags)
    lines.append('```')
    lines.append('')
    lines.append('---')
    lines.append('')
    lines.append('## 工作内容')
    lines.append('')

    for i, item in enumerate(work_items, 1):
        title = item.get('title', f'工作内容 {i}')
        desc = item.get('desc', '')
        lines.append(f'### 工作内容 {i}：{title}')
        lines.append('')
        lines.append('```')
        lines.append(desc)
        lines.append('```')
        lines.append('')

    if notes:
        lines.append('---')
        lines.append('')
        lines.append('## 补充说明（可选）')
        lines.append('')
        lines.append('```')
        lines.append(notes)
        lines.append('```')
        lines.append('')

    return '\n'.join(lines)


def save_experience_form(data: dict) -> str:
    """保存表单录入的经历为 .md 文件（支持新建和更新）"""
    _experiences_dir().mkdir(parents=True, exist_ok=True)

    company = data.get('company', '').strip()
    if not company:
        raise ValueError('请填写公司/机构名')

    update_filename = data.get('update_filename', '').strip()
    if update_filename:
        # Update existing file
        safe_name = sanitize_filename(update_filename)
        target = _experiences_dir() / safe_name
        if not target.exists():
            raise ValueError(f'文件不存在: {safe_name}')
        filename = safe_name
    else:
        # Create new file
        num = get_next_experience_number()
        safe_name = sanitize_filename(company)
        filename = f'{num:02d}_{safe_name}.md'
        target = _experiences_dir() / filename

    md_content = render_experience_md(data)
    target.write_text(md_content, encoding='utf-8')

    return filename


# ─── 经历文件列表 ───────────────────────────────────────────────

def list_experiences() -> dict:
    """列出所有经历文件和工作材料"""
    experiences = []
    work_materials = []

    # 扫描 experiences/
    if _experiences_dir().exists():
        for f in sorted(_experiences_dir().iterdir()):
            if f.name in ('_template.md', 'README.md') or f.name.startswith('.'):
                continue
            if f.is_file():
                parsed = parse_experience_file(f)
                experiences.append({
                    'filename': f.name,
                    'size': f.stat().st_size,
                    'type': 'experience',
                    'company': parsed.get('company', ''),
                    'city': parsed.get('city', ''),
                    'department': parsed.get('department', ''),
                    'role': parsed.get('role', ''),
                    'time_start': parsed.get('time_start', ''),
                    'time_end': parsed.get('time_end', ''),
                    'tags': parsed.get('tags', ''),
                    'notes': parsed.get('notes', ''),
                    'work_items': parsed.get('work_items', []),
                })

    # 扫描 work_materials/
    if _work_materials_dir().exists():
        for d in sorted(_work_materials_dir().iterdir()):
            if d.name == 'README.md' or d.name.startswith('.'):
                continue
            if d.is_dir():
                files = [f.name for f in d.iterdir() if f.is_file() and not f.name.startswith('.')]
                if files:
                    work_materials.append({
                        'company': d.name,
                        'files': sorted(files),
                        'type': 'work_material'
                    })

    return {'experiences': experiences, 'work_materials': work_materials}


def _extract_pdf_metadata(content: bytes, filename: str) -> dict:
    """从 PDF 文件中提取论文元信息（标题、作者等）

    尝试使用 PyPDF2/pypdf 提取 PDF 元数据和首页文本。
    如果没有安装相关库，则回退为仅从文件名推断标题。
    """
    result = {
        'title': '',
        'authors': '',
        'venue': '',
        'year': '',
        'description': ''
    }

    # 尝试从文件名推断标题
    base_name = Path(filename).stem
    # 清理常见的文件名格式
    base_name = re.sub(r'[-_]', ' ', base_name)
    result['title'] = base_name

    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(content))

        # 尝试从 PDF 元数据获取信息
        meta = reader.metadata
        if meta:
            if meta.title:
                result['title'] = meta.title
            if meta.author:
                result['authors'] = meta.author

        # 提取首页文本用于描述
        if len(reader.pages) > 0:
            first_page_text = reader.pages[0].extract_text() or ''
            # 取前 500 字作为参考描述
            if first_page_text.strip():
                # 尝试提取 abstract
                abstract_match = re.search(
                    r'(?:Abstract|摘要)[:\s—\-]*(.*?)(?:\n\n|\n(?:Keywords|关键词|1[\.\s]|Introduction|引言))',
                    first_page_text,
                    re.IGNORECASE | re.DOTALL
                )
                if abstract_match:
                    result['description'] = abstract_match.group(1).strip()[:500]
                else:
                    result['description'] = first_page_text[:300].strip()

    except ImportError:
        # pypdf not installed, try PyPDF2
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(content))
            meta = reader.metadata
            if meta:
                if meta.get('/Title'):
                    result['title'] = meta['/Title']
                if meta.get('/Author'):
                    result['authors'] = meta['/Author']

            if len(reader.pages) > 0:
                first_page_text = reader.pages[0].extract_text() or ''
                if first_page_text.strip():
                    abstract_match = re.search(
                        r'(?:Abstract|摘要)[:\s—\-]*(.*?)(?:\n\n|\n(?:Keywords|关键词|1[\.\s]|Introduction|引言))',
                        first_page_text,
                        re.IGNORECASE | re.DOTALL
                    )
                    if abstract_match:
                        result['description'] = abstract_match.group(1).strip()[:500]
                    else:
                        result['description'] = first_page_text[:300].strip()

        except ImportError:
            # No PDF library available, return basic info from filename
            pass
        except Exception:
            pass
    except Exception:
        pass

    return result


def _sanitize_ext_draft_value(value: str, kind: str) -> str:
    text = str(value or '').strip()
    placeholders = {
        'jd': {'QA JD'},
        'interview': {'QA Interview'},
    }
    if text in placeholders.get(kind, set()):
        return ''
    return text


def _summarize_generation_text(text: str, limit: int = 72) -> str:
    compact = re.sub(r'\s+', ' ', str(text or '')).strip()
    if not compact:
        return ''
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + '…'


# ─── 版本快照 ─────────────────────────────────────────────────

def _create_version_snapshot(out_dir: Path, fill_rate: float = 0, pages: int = 1) -> int:
    """编译成功后创建 tex 快照，返回新版本号"""
    versions_dir = out_dir / 'versions'
    versions_dir.mkdir(parents=True, exist_ok=True)
    versions_json = versions_dir / 'versions.json'

    versions = []
    if versions_json.exists():
        try:
            versions = json.loads(versions_json.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            versions = []

    new_version = (versions[-1]['version'] + 1) if versions else 1
    ts = datetime.now().strftime('%Y%m%dT%H%M%S')
    snapshot_name = f'v{new_version}_{ts}.tex'

    tex_src = out_dir / 'resume-zh_CN.tex'
    if tex_src.exists():
        shutil.copy2(str(tex_src), str(versions_dir / snapshot_name))

    versions.append({
        'version': new_version,
        'timestamp': datetime.now().isoformat(),
        'filename': snapshot_name,
        'note': '',
        'fill_rate': fill_rate,
        'pages': pages,
    })
    versions_json.write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding='utf-8')
    return new_version


def _get_version_count(out_dir: Path) -> int:
    """读取某个输出目录的版本数"""
    versions_json = out_dir / 'versions' / 'versions.json'
    if not versions_json.exists():
        return 0
    try:
        versions = json.loads(versions_json.read_text(encoding='utf-8'))
        return len(versions)
    except (json.JSONDecodeError, OSError):
        return 0


def _find_xelatex() -> str:
    """查找 xelatex 二进制路径"""
    candidates = [
        Path.home() / 'Library' / 'TinyTeX' / 'bin' / 'universal-darwin' / 'xelatex',
        Path.home() / '.TinyTeX' / 'bin' / 'x86_64-linux' / 'xelatex',
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return 'xelatex'


# ─── 简历画廊 ─────────────────────────────────────────────────

def list_gallery_resumes() -> list:
    """扫描 output/ 目录，列出已生成的简历"""
    resumes = []
    if not _output_dir().exists():
        return resumes

    for d in sorted(_output_dir().iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith('.'):
            continue
        # 目录名格式: {公司名}_{岗位名}_{YYYYMMDD}
        pdf_files = list(d.glob('*.pdf'))
        if not pdf_files:
            continue

        parts = d.name.split('_')
        company = parts[0] if len(parts) >= 1 else d.name
        role = parts[1] if len(parts) >= 2 else ''
        date = parts[2] if len(parts) >= 3 else ''
        if date and len(date) == 8:
            date = f'{date[:4]}/{date[4:6]}/{date[6:]}'

        for pdf in pdf_files:
            rel_path = str(pdf.relative_to(_output_dir()))
            context_path = d / 'generation_context.json'
            context = {}
            if context_path.exists():
                try:
                    context = json.loads(context_path.read_text(encoding='utf-8'))
                except (json.JSONDecodeError, OSError):
                    context = {}
            resumes.append({
                'company': company,
                'role': role,
                'date': date,
                'dir_name': d.name,
                'pdf_name': pdf.name,
                'pdf_path': rel_path,
                'size': pdf.stat().st_size,
                'version_count': _get_version_count(d),
                'jd_text': context.get('jd_text', '') or '',
                'interview_text': context.get('interview_text', '') or '',
                'interview_notes': context.get('interview_notes', '') or '',
                'jd_excerpt': _summarize_generation_text(context.get('jd_text', ''), 88),
                'interview_excerpt': _summarize_generation_text(context.get('interview_text', ''), 66),
                'engine': context.get('engine', ''),
                'ai_provider': context.get('ai_provider', ''),
                'ai_model': context.get('ai_model', ''),
            })

    return resumes


# ─── HTTP 请求处理器 ───────────────────────────────────────────

# ─── JD 关键词提取 ────────────────────────────────────────────

def _extract_jd_keywords(text: str) -> dict:
    """从 JD 文本中提取关键词（基于规则的简单实现）"""
    text_lower = text.lower()

    # 技术栈关键词
    tech_patterns = [
        'python', 'java', 'javascript', 'typescript', 'c\\+\\+', 'go', 'rust',
        'sql', 'r', 'stata', 'matlab', 'scala', 'ruby', 'swift', 'kotlin',
        'react', 'vue', 'angular', 'node\\.js', 'django', 'flask', 'spring',
        'tensorflow', 'pytorch', 'pandas', 'numpy', 'sklearn', 'spark',
        'docker', 'kubernetes', 'aws', 'gcp', 'azure',
        'mysql', 'postgresql', 'mongodb', 'redis', 'elasticsearch',
        'tableau', 'power bi', 'excel', 'spss', 'sas',
        'git', 'linux', 'ci/cd', 'agile', 'scrum',
    ]

    # 职能关键词
    role_patterns = {
        '数据分析': ['数据分析', '数据挖掘', 'data analysis', 'analytics', 'bi'],
        '产品经理': ['产品经理', '产品设计', 'product manager', 'pm', '需求分析'],
        '运营': ['运营', '增长', 'growth', 'operation', '用户运营', '内容运营'],
        '开发': ['开发', '研发', 'engineer', 'developer', '后端', '前端', '全栈'],
        '算法': ['算法', 'algorithm', 'machine learning', '机器学习', '深度学习', 'nlp', 'cv'],
        '金融': ['金融', '投资', '风控', '量化', 'finance', 'investment', '券商', '基金'],
        '咨询': ['咨询', 'consulting', '战略', 'strategy', '行业研究'],
        '设计': ['设计', 'design', 'ui', 'ux', '交互', '视觉'],
        '市场': ['市场', 'marketing', '品牌', 'brand', '推广'],
        '人力资源': ['人力', 'hr', '招聘', '薪酬', 'human resource'],
    }

    # 软技能
    soft_skills_patterns = {
        '沟通能力': ['沟通', 'communication', '表达'],
        '团队协作': ['团队', 'teamwork', '协作', 'collaboration'],
        '领导力': ['领导', 'leadership', '带团队'],
        '抗压能力': ['抗压', '压力', 'pressure'],
        '学习能力': ['学习能力', '快速学习', 'fast learner'],
        '分析能力': ['分析能力', 'analytical', '逻辑'],
    }

    found_tech = []
    for pattern in tech_patterns:
        if re.search(r'\b' + pattern + r'\b', text_lower) or pattern in text_lower:
            found_tech.append(pattern.replace('\\', ''))

    found_roles = []
    for role, keywords in role_patterns.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                found_roles.append(role)
                break

    found_soft = []
    for skill, keywords in soft_skills_patterns.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                found_soft.append(skill)
                break

    # 学历要求
    edu_req = ''
    if '硕士' in text or 'master' in text_lower:
        edu_req = '硕士'
    elif '博士' in text or 'phd' in text_lower or 'doctor' in text_lower:
        edu_req = '博士'
    elif '本科' in text or 'bachelor' in text_lower:
        edu_req = '本科'

    # 经验要求
    exp_req = ''
    exp_match = re.search(r'(\d+)[年\-\+]*[\s]*(?:年|years?)', text)
    if exp_match:
        exp_req = f'{exp_match.group(1)}年'

    return {
        'tech_stack': found_tech,
        'roles': found_roles,
        'soft_skills': found_soft,
        'education_req': edu_req,
        'experience_req': exp_req,
        'raw_length': len(text),
    }


class ResumeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        """简化日志输出"""
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, status=400):
        self._send_json({'error': message}, status)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == '/' or path == '/index.html':
            self._serve_html()
        elif path == '/api/persons':
            self._get_persons()
        elif path == '/api/profile':
            self._get_profile()
        elif path == '/api/model-config':
            self._get_model_config()
        elif path == '/api/extra-info':
            self._get_extra_info()
        elif path == '/api/experiences':
            self._get_experiences()
        elif path.startswith('/api/experiences/') and path.endswith('/content'):
            filename = urllib.parse.unquote(path[len('/api/experiences/'):-len('/content')])
            self._get_experience_content(filename)
        elif path == '/api/gallery':
            self._get_gallery()
        elif path.startswith('/api/gallery/pdf/'):
            rel_path = urllib.parse.unquote(path[len('/api/gallery/pdf/'):])
            self._serve_gallery_pdf(rel_path)
        elif path.startswith('/api/editor/tex'):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            dir_name = params.get('dir', [''])[0]
            self._get_editor_tex(dir_name)
        elif path.startswith('/api/editor/versions'):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            dir_name = params.get('dir', [''])[0]
            self._get_editor_versions(dir_name)
        # ─── Chrome Extension API ───
        elif path == '/api/ext/profile':
            self._ext_get_profile()
        elif path == '/api/ext/fill-data':
            self._ext_get_fill_data()
        elif path == '/api/ext/field-map':
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            platform = params.get('platform', [''])[0]
            self._ext_get_field_map(platform)
        elif path == '/api/ext/history':
            self._ext_get_history()
        elif path == '/api/ext/draft':
            self._ext_get_draft()
        elif path == '/monitor' or path == '/monitor.html':
            self._serve_monitor_html()
        elif path == '/api/monitor/logs':
            self._get_monitor_logs()
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        # 检查请求大小
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > MAX_UPLOAD_SIZE:
            self._send_error_json('文件大小超过限制（50MB）', 413)
            return

        if path == '/api/persons':
            self._create_person()
        elif path == '/api/persons/active':
            self._set_active_person()
        elif path == '/api/profile':
            self._save_profile()
        elif path == '/api/model-config':
            self._save_model_config()
        elif path == '/api/extra-info':
            self._save_extra_info()
        elif path == '/api/experiences/form':
            self._save_experience_form()
        elif path == '/api/experiences':
            self._upload_experience()
        elif path == '/api/publications/upload':
            self._upload_publication_pdf()
        elif path == '/api/generate':
            self._generate_resume()
        elif path == '/api/editor/regenerate':
            self._regenerate_resume()
        elif path == '/api/editor/save':
            self._save_editor_tex()
        elif path == '/api/editor/saveas':
            self._saveas_editor_tex()
        elif path == '/api/editor/compile':
            self._compile_editor_tex()
        elif path == '/api/editor/versions/note':
            self._update_version_note()
        elif path == '/api/editor/versions/restore':
            self._restore_version()
        elif path == '/api/editor/synctex':
            self._synctex_query()
        # ─── Chrome Extension API ───
        elif path == '/api/ext/jd-analyze':
            self._ext_jd_analyze()
        elif path == '/api/ext/fill-log':
            self._ext_fill_log()
        elif path == '/api/ext/correction':
            self._ext_correction()
        elif path == '/api/ext/field-map':
            self._ext_update_field_map()
        elif path == '/api/ext/draft':
            self._ext_save_draft()
        elif path == '/api/monitor/clear':
            self._clear_monitor_logs()
        else:
            self.send_error(404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path

        if path.startswith('/api/persons/'):
            person_id = urllib.parse.unquote(path[len('/api/persons/'):])
            self._delete_person(person_id)
        elif path.startswith('/api/experiences/'):
            filename = urllib.parse.unquote(path[len('/api/experiences/'):])
            self._delete_experience(filename)
        elif path.startswith('/api/gallery/'):
            dir_name = urllib.parse.unquote(path[len('/api/gallery/'):])
            self._delete_gallery_item(dir_name)
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_PATCH(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith('/api/gallery/') and path.endswith('/notes'):
            dir_name = urllib.parse.unquote(path[len('/api/gallery/'):-len('/notes')])
            self._save_gallery_notes(dir_name)
        elif path.startswith('/api/gallery/') and path.endswith('/meta'):
            dir_name = urllib.parse.unquote(path[len('/api/gallery/'):-len('/meta')])
            self._save_gallery_meta(dir_name)
        else:
            self.send_error(404)

    # ─── 路由实现 ──────────────────────────────────────────

    def _serve_html(self):
        html_path = WEB_DIR / 'index.html'
        if not html_path.exists():
            self.send_error(404, 'index.html not found')
            return

        content = html_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def _serve_monitor_html(self):
        html_path = WEB_DIR / 'monitor.html'
        if not html_path.exists():
            self.send_error(404, 'monitor.html not found')
            return
        content = html_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def _get_monitor_logs(self):
        """Return gen_log entries since the given seq number."""
        try:
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            since = int(params.get('since', ['0'])[0])
            entries = gen_log.get_entries_since(since)
            self._send_json({'entries': entries})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _clear_monitor_logs(self):
        try:
            gen_log.clear()
            self._send_json({'ok': True})
        except Exception as e:
            self._send_error_json(str(e), 500)

    # ─── 人员管理 ──────────────────────────────────────────

    def _get_persons(self):
        try:
            persons = list_persons()
            active = get_active_person_id()
            self._send_json({'persons': persons, 'active': active})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _create_person(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            display_name = data.get('display_name', '').strip()
            if not display_name:
                self._send_error_json('请填写显示名', 400)
                return
            person = create_person(display_name)
            self._send_json({'success': True, 'person': person})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _set_active_person(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            person_id = data.get('person_id', '').strip()
            if not person_id:
                self._send_error_json('请提供 person_id', 400)
                return
            set_active_person(person_id)
            self._send_json({'success': True, 'active': person_id})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _delete_person(self, person_id):
        try:
            if not person_id:
                self._send_error_json('请提供 person_id', 400)
                return
            persons = list_persons()
            if len(persons) <= 1:
                self._send_error_json('至少保留一个人员', 400)
                return
            delete_person(person_id, delete_data=False)
            self._send_json({'success': True, 'message': f'已删除人员: {person_id}'})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_profile(self):
        try:
            data = parse_profile()
            self._send_json(data)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_model_config(self):
        try:
            self._send_json(get_model_config())
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _save_profile(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            md_content = render_profile(data)
            _profile_path().parent.mkdir(parents=True, exist_ok=True)
            _profile_path().write_text(md_content, encoding='utf-8')

            self._send_json({'success': True, 'message': '个人信息已保存'})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _save_model_config(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            config = save_model_config({
                'enabled': bool(data.get('enabled')),
                'provider': data.get('provider', ''),
                'model': data.get('model', ''),
                'base_url': data.get('base_url', ''),
                'api_key': data.get('api_key', ''),
                'platform_url': data.get('platform_url', ''),
            })
            self._send_json({'success': True, 'config': config})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_extra_info(self):
        try:
            path = _extra_info_path()
            if not path.exists():
                self._send_json({'items': []})
                return
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except json.JSONDecodeError:
                data = []
            items = data if isinstance(data, list) else data.get('items', [])
            if not isinstance(items, list):
                items = []
            cleaned = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                cleaned.append({
                    'key': str(item.get('key', '')).strip(),
                    'value': str(item.get('value', '')).strip(),
                })
            self._send_json({'items': cleaned})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _save_extra_info(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            items = data.get('items', [])
            if not isinstance(items, list):
                self._send_error_json('items 格式错误', 400)
                return
            cleaned = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = str(item.get('key', '')).strip()
                value = str(item.get('value', '')).strip()
                if key or value:
                    cleaned.append({'key': key, 'value': value})
            path = _extra_info_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding='utf-8')
            self._send_json({'success': True, 'items': cleaned})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_experiences(self):
        try:
            data = list_experiences()
            self._send_json(data)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_experience_content(self, filename):
        try:
            safe_name = sanitize_filename(filename)
            target = _experiences_dir() / safe_name
            if not target.exists():
                self._send_error_json(f'文件不存在: {safe_name}', 404)
                return
            data = parse_experience_file(target)
            self._send_json(data)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_gallery(self):
        try:
            resumes = list_gallery_resumes()
            self._send_json({'resumes': resumes})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _serve_gallery_pdf(self, rel_path):
        try:
            # Security: prevent path traversal
            if '..' in rel_path or rel_path.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            pdf_path = _output_dir() / rel_path
            if not pdf_path.exists() or not pdf_path.is_file():
                self._send_error_json('文件不存在', 404)
                return
            # Ensure path is within _output_dir()
            try:
                pdf_path.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            content = pdf_path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Length', len(content))
            self.send_header('Content-Disposition', f'inline; filename="{pdf_path.name}"')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _delete_gallery_item(self, dir_name):
        """删除画廊中的简历目录"""
        import shutil
        try:
            # Security: prevent path traversal
            if '..' in dir_name or '/' in dir_name or dir_name.startswith('.'):
                self._send_error_json('非法路径', 403)
                return
            target = _output_dir() / dir_name
            if not target.exists() or not target.is_dir():
                self._send_error_json('目录不存在', 404)
                return
            # Ensure path is within _output_dir()
            try:
                target.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            shutil.rmtree(target)
            self._send_json({'success': True, 'message': f'已删除: {dir_name}'})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _save_gallery_meta(self, dir_name):
        """更新公司名/岗位名到 generation_context.json"""
        try:
            if '..' in dir_name or '/' in dir_name or dir_name.startswith('.'):
                self._send_error_json('非法路径', 403)
                return
            target = _output_dir() / dir_name
            if not target.exists() or not target.is_dir():
                self._send_error_json('目录不存在', 404)
                return
            try:
                target.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
            context_path = target / 'generation_context.json'
            context = {}
            if context_path.exists():
                try:
                    context = json.loads(context_path.read_text(encoding='utf-8'))
                except (json.JSONDecodeError, OSError):
                    context = {}
            if 'company' in data:
                context['company'] = str(data['company']).strip()
            if 'role' in data:
                context['role'] = str(data['role']).strip()
            context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')
            self._send_json({'success': True, 'company': context.get('company', ''), 'role': context.get('role', '')})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _save_gallery_notes(self, dir_name):
        """保存面经笔记到 generation_context.json"""
        try:
            if '..' in dir_name or '/' in dir_name or dir_name.startswith('.'):
                self._send_error_json('非法路径', 403)
                return
            target = _output_dir() / dir_name
            if not target.exists() or not target.is_dir():
                self._send_error_json('目录不存在', 404)
                return
            try:
                target.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
            notes = str(data.get('interview_notes', ''))
            context_path = target / 'generation_context.json'
            context = {}
            if context_path.exists():
                try:
                    context = json.loads(context_path.read_text(encoding='utf-8'))
                except (json.JSONDecodeError, OSError):
                    context = {}
            context['interview_notes'] = notes
            context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')
            self._send_json({'success': True})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _regenerate_resume(self):
        """在编辑器中重新生成简历（带用户反馈），结果覆盖当前目录并记录为新版本"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            dir_name = data.get('dir', '').strip()
            feedback = data.get('feedback', '').strip()

            if not dir_name or '..' in dir_name or '/' in dir_name:
                self._send_error_json('非法路径', 403)
                return

            target_dir = _output_dir() / dir_name
            try:
                target_dir.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            if not target_dir.exists():
                self._send_error_json('目录不存在', 404)
                return

            # Read original generation context
            context_path = target_dir / 'generation_context.json'
            if not context_path.exists():
                self._send_error_json('找不到生成上下文，无法重新生成', 400)
                return
            context = json.loads(context_path.read_text(encoding='utf-8'))

            jd_text = context.get('jd_text', '').strip()
            interview_text = context.get('interview_text', '').strip()
            company = context.get('company', '').strip()
            role = context.get('role', '').strip()
            prefer_ai = context.get('engine', '') == 'ai'

            if not jd_text:
                self._send_error_json('原始 JD 内容为空，无法重新生成', 400)
                return

            # Use original interview_text; feedback is passed as dedicated param
            from tools.generate_resume import generate_resume

            # Create a snapshot of current state BEFORE regenerating
            _create_version_snapshot(target_dir, fill_rate=context.get('fill_ratio', 0) * 100)

            # Generate into a temp dir (generate_resume always creates new dir)
            result = generate_resume(
                jd_text,
                interview_text,
                company=company,
                role=role,
                person_id=get_active_person_id(),
                prefer_ai=prefer_ai,
                feedback=feedback,
            )

            if not result.get('success'):
                self._send_error_json(result.get('error', '重新生成失败'), 500)
                return

            # Move generated files from new temp dir into the current dir
            new_dir_name = result.get('output_dir', '')
            if new_dir_name:
                new_dir = _output_dir() / new_dir_name
                if new_dir.exists() and new_dir != target_dir:
                    for item in new_dir.iterdir():
                        if item.name == 'versions':
                            continue  # preserve existing versions
                        dest = target_dir / item.name
                        if item.is_dir():
                            if dest.exists():
                                shutil.rmtree(dest)
                            shutil.copytree(str(item), str(dest))
                        else:
                            shutil.copy2(str(item), str(dest))
                    shutil.rmtree(str(new_dir))

            # Update generation context with new metadata
            new_context_path = target_dir / 'generation_context.json'
            new_context = json.loads(new_context_path.read_text(encoding='utf-8')) if new_context_path.exists() else {}
            if feedback:
                new_context['last_feedback'] = feedback
            new_context_path.write_text(json.dumps(new_context, ensure_ascii=False, indent=2), encoding='utf-8')

            # Create version snapshot for the newly regenerated result
            fill_ratio = result.get('fill_ratio', 0)
            new_ver = _create_version_snapshot(target_dir, fill_rate=fill_ratio * 100)
            # Annotate with feedback
            if feedback:
                versions_json_path = target_dir / 'versions' / 'versions.json'
                if versions_json_path.exists():
                    versions = json.loads(versions_json_path.read_text(encoding='utf-8'))
                    for v in versions:
                        if v['version'] == new_ver:
                            v['note'] = f'重新生成: {feedback[:60]}'
                            break
                    versions_json_path.write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding='utf-8')

            # Read new tex content to return to editor
            tex_path = target_dir / 'resume-zh_CN.tex'
            tex_content = tex_path.read_text(encoding='utf-8') if tex_path.exists() else ''

            self._send_json({
                'success': True,
                'content': tex_content,
                'version': new_ver,
                'fill_ratio': fill_ratio,
                'version_count': _get_version_count(target_dir),
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_error_json(str(e), 500)

    def _generate_resume(self):
        """调用生成引擎，编译 PDF 并返回结果"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            # 导入生成引擎
            import sys
            tools_dir = str(PROJECT_ROOT / 'tools')
            if tools_dir not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))

            from tools.generate_resume import generate_resume

            status, result = _run_generate_with_entitlement(
                data=data,
                headers=self.headers,
                active_person_id=get_active_person_id(),
                generate_func=generate_resume,
            )
            self._send_json(result, status=status)

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_error_json(str(e), 500)

    # ─── LaTeX Editor API ──────────────────────────────────────

    def _get_editor_tex(self, dir_name):
        """读取 output 目录中的 .tex 文件内容"""
        try:
            if not dir_name or '..' in dir_name or dir_name.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            tex_path = _output_dir() / dir_name / 'resume-zh_CN.tex'
            try:
                tex_path.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            if not tex_path.exists():
                self._send_error_json('文件不存在', 404)
                return
            content = tex_path.read_text(encoding='utf-8')
            self._send_json({
                'content': content,
                'filename': 'resume-zh_CN.tex',
                'dir_name': dir_name,
            })
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _save_editor_tex(self):
        """保存编辑后的 .tex 内容"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            dir_name = data.get('dir', '')
            tex_content = data.get('content', '')
            if not dir_name or '..' in dir_name or dir_name.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            tex_path = _output_dir() / dir_name / 'resume-zh_CN.tex'
            try:
                tex_path.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            if not tex_path.parent.exists():
                self._send_error_json('目录不存在', 404)
                return
            tex_path.write_text(tex_content, encoding='utf-8')
            self._send_json({'success': True})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _saveas_editor_tex(self):
        """另存为新目录（新公司/岗位）"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            source_dir = data.get('dir', '')
            tex_content = data.get('content', '')
            new_dir = data.get('new_dir', '')
            if not new_dir or '..' in new_dir or new_dir.startswith('/'):
                self._send_error_json('非法目录名', 403)
                return
            new_path = _output_dir() / new_dir
            try:
                new_path.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            # Create new directory
            new_path.mkdir(parents=True, exist_ok=True)
            # Copy latex support files from source (fallback to template)
            src_dir = _output_dir() / source_dir if source_dir else None
            template_dir = PROJECT_ROOT / 'latex_src' / 'resume'
            for f in ['resume.cls', 'zh_CN-Adobefonts_external.sty',
                      'linespacing_fix.sty']:
                src_file = (src_dir / f) if (src_dir and (src_dir / f).exists()) else (template_dir / f)
                if src_file.exists():
                    shutil.copy2(str(src_file), str(new_path / f))

            # Reuse shared fonts via symlink (avoid per-resume duplication)
            dst_fonts = new_path / 'fonts'
            if not dst_fonts.exists():
                src_fonts = template_dir / 'fonts'
                if src_fonts.exists():
                    try:
                        os.symlink(src_fonts.resolve(), dst_fonts)
                    except OSError:
                        if src_fonts.is_dir():
                            shutil.copytree(str(src_fonts), str(dst_fonts))
            # Write tex
            tex_path = new_path / 'resume-zh_CN.tex'
            tex_path.write_text(tex_content, encoding='utf-8')
            self._send_json({'success': True, 'new_dir': new_dir})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _compile_editor_tex(self):
        """保存 + 编译 + 版本快照 + 返回状态"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            dir_name = data.get('dir', '')
            tex_content = data.get('content', '')
            if not dir_name or '..' in dir_name or dir_name.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            out_dir = _output_dir() / dir_name
            tex_path = out_dir / 'resume-zh_CN.tex'
            try:
                tex_path.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            if not out_dir.exists():
                self._send_error_json('目录不存在', 404)
                return

            # 1. Save
            tex_path.write_text(tex_content, encoding='utf-8')

            # 2. Find xelatex
            xelatex_bin = _find_xelatex()

            # 3. Compile with synctex
            env = os.environ.copy()
            xelatex_dir = str(Path(xelatex_bin).parent) if xelatex_bin != 'xelatex' else ''
            if xelatex_dir:
                env['PATH'] = xelatex_dir + ':' + env.get('PATH', '')

            result = _sp.run(
                [xelatex_bin, '-interaction=nonstopmode', '--synctex=1', 'resume-zh_CN.tex'],
                cwd=str(out_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )

            pdf_path = out_dir / 'resume-zh_CN.pdf'
            log_path = out_dir / 'resume-zh_CN.log'

            # 4. Read log tail on failure
            log_tail = ''
            if log_path.exists():
                lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
                log_tail = '\n'.join(lines[-30:])

            if result.returncode != 0 or not pdf_path.exists():
                self._send_json({
                    'success': False,
                    'pages': 0,
                    'fill_rate': 0,
                    'errors': f'编译失败 (exit code {result.returncode})',
                    'log_tail': log_tail,
                    'version_count': _get_version_count(out_dir),
                })
                return

            # 5. Get page count (macOS mdls or fallback)
            pages = 1
            try:
                mdls = _sp.run(
                    ['mdls', '-name', 'kMDItemNumberOfPages', str(pdf_path)],
                    capture_output=True, text=True, timeout=10,
                )
                for line in mdls.stdout.splitlines():
                    if 'kMDItemNumberOfPages' in line and '=' in line:
                        val = line.split('=')[1].strip()
                        if val != '(null)':
                            pages = int(val)
            except Exception:
                pass

            # 6. Get fill rate
            fill_rate = 0.0
            try:
                fill_check = str(PROJECT_ROOT / 'tools' / 'page_fill_check.py')
                fr_result = _sp.run(
                    ['python3', fill_check, str(out_dir)],
                    capture_output=True, text=True, timeout=60, env=env,
                )
                m = re.search(r'填充率[：:]\s*([\d.]+)%', fr_result.stdout)
                if m:
                    fill_rate = float(m.group(1))
            except Exception:
                pass

            # 7. Create version snapshot
            version_num = _create_version_snapshot(out_dir, fill_rate, pages)

            self._send_json({
                'success': True,
                'pages': pages,
                'fill_rate': fill_rate,
                'errors': '',
                'log_tail': log_tail if pages > 1 else '',
                'version_count': _get_version_count(out_dir),
            })

        except _sp.TimeoutExpired:
            self._send_json({
                'success': False, 'pages': 0, 'fill_rate': 0,
                'errors': '编译超时（60秒）', 'log_tail': '',
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_error_json(str(e), 500)

    # ─── Version Management API ───────────────────────────────

    def _get_editor_versions(self, dir_name):
        """返回版本列表"""
        try:
            if not dir_name or '..' in dir_name or dir_name.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            out_dir = _output_dir() / dir_name
            try:
                out_dir.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            versions_json = out_dir / 'versions' / 'versions.json'
            if not versions_json.exists():
                self._send_json({'versions': []})
                return
            versions = json.loads(versions_json.read_text(encoding='utf-8'))
            self._send_json({'versions': versions})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _update_version_note(self):
        """更新版本备注"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            dir_name = data.get('dir', '')
            version = data.get('version', 0)
            note = data.get('note', '')
            if not dir_name or '..' in dir_name or dir_name.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            out_dir = _output_dir() / dir_name
            try:
                out_dir.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            versions_json = out_dir / 'versions' / 'versions.json'
            if not versions_json.exists():
                self._send_error_json('版本记录不存在', 404)
                return
            versions = json.loads(versions_json.read_text(encoding='utf-8'))
            found = False
            for v in versions:
                if v['version'] == version:
                    v['note'] = note
                    found = True
                    break
            if not found:
                self._send_error_json(f'版本 {version} 不存在', 404)
                return
            versions_json.write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding='utf-8')
            self._send_json({'success': True})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _restore_version(self):
        """恢复某个版本的 tex 内容（只返回文本，不覆盖文件）"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            dir_name = data.get('dir', '')
            version = data.get('version', 0)
            if not dir_name or '..' in dir_name or dir_name.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            out_dir = _output_dir() / dir_name
            try:
                out_dir.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            versions_json = out_dir / 'versions' / 'versions.json'
            if not versions_json.exists():
                self._send_error_json('版本记录不存在', 404)
                return
            versions = json.loads(versions_json.read_text(encoding='utf-8'))
            target = None
            for v in versions:
                if v['version'] == version:
                    target = v
                    break
            if not target:
                self._send_error_json(f'版本 {version} 不存在', 404)
                return
            tex_path = out_dir / 'versions' / target['filename']
            if not tex_path.exists():
                self._send_error_json('版本文件不存在', 404)
                return
            content = tex_path.read_text(encoding='utf-8')
            self._send_json({'success': True, 'content': content})
        except Exception as e:
            self._send_error_json(str(e), 500)

    # ─── SyncTeX API ──────────────────────────────────────────

    def _synctex_query(self):
        """SyncTeX 正向/反向查询"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            dir_name = data.get('dir', '')
            action = data.get('action', '')  # "forward" or "inverse"
            if not dir_name or '..' in dir_name or dir_name.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            out_dir = _output_dir() / dir_name
            try:
                out_dir.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return

            pdf_file = out_dir / 'resume-zh_CN.pdf'
            tex_file = 'resume-zh_CN.tex'

            # Find synctex binary (same dir as xelatex)
            xelatex_bin = _find_xelatex()
            synctex_bin = str(Path(xelatex_bin).parent / 'synctex') if xelatex_bin != 'xelatex' else 'synctex'

            env = os.environ.copy()
            synctex_dir = str(Path(synctex_bin).parent) if synctex_bin != 'synctex' else ''
            if synctex_dir:
                env['PATH'] = synctex_dir + ':' + env.get('PATH', '')

            if action == 'forward':
                line = data.get('line', 1)
                col = data.get('col', 0)
                cmd = [synctex_bin, 'view', '-i', f'{line}:{col}:{tex_file}', '-o', str(pdf_file)]
                result = _sp.run(cmd, capture_output=True, text=True, timeout=10, cwd=str(out_dir), env=env)
                parsed = self._parse_synctex_output(result.stdout, 'forward')
                self._send_json({'success': True, **parsed})

            elif action == 'inverse':
                page = data.get('page', 1)
                x = data.get('x', 0)
                y = data.get('y', 0)
                cmd = [synctex_bin, 'edit', '-o', f'{page}:{x}:{y}:{str(pdf_file)}']
                result = _sp.run(cmd, capture_output=True, text=True, timeout=10, cwd=str(out_dir), env=env)
                parsed = self._parse_synctex_output(result.stdout, 'inverse')
                self._send_json({'success': True, **parsed})
            else:
                self._send_error_json('action 必须为 forward 或 inverse', 400)

        except _sp.TimeoutExpired:
            self._send_error_json('SyncTeX 查询超时', 500)
        except Exception as e:
            self._send_error_json(str(e), 500)

    @staticmethod
    def _parse_synctex_output(output: str, mode: str) -> dict:
        """解析 synctex 命令行输出"""
        result = {}
        for line in output.splitlines():
            line = line.strip()
            if ':' not in line:
                continue
            key, _, val = line.partition(':')
            key = key.strip()
            val = val.strip()
            try:
                if key == 'Page':
                    result['page'] = int(val)
                elif key == 'x':
                    result['x'] = float(val)
                elif key == 'y':
                    result['y'] = float(val)
                elif key == 'h':
                    result['h'] = float(val)
                elif key == 'v':
                    result['v'] = float(val)
                elif key == 'W':
                    result['W'] = float(val)
                elif key == 'H':
                    result['H'] = float(val)
                elif key == 'Line':
                    result['line'] = int(val)
                elif key == 'Column':
                    result['col'] = int(val)
            except (ValueError, TypeError):
                pass
        return result

    def _save_experience_form(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            filename = save_experience_form(data)
            self._send_json({'success': True, 'filename': filename})
        except ValueError as e:
            self._send_error_json(str(e))
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _upload_experience(self):
        try:
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self._send_error_json('请使用 multipart/form-data 格式上传')
                return

            # 解析 multipart
            ctype, pdict = cgi.parse_header(content_type)
            if 'boundary' in pdict:
                if isinstance(pdict['boundary'], str):
                    pdict['boundary'] = pdict['boundary'].encode()

            content_length = int(self.headers.get('Content-Length', 0))
            environ = {
                'REQUEST_METHOD': 'POST',
                'CONTENT_TYPE': content_type,
                'CONTENT_LENGTH': str(content_length),
            }

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ=environ
            )

            company_name = form.getfirst('company_name', '').strip()

            results = []
            file_items = form['file'] if 'file' in form else []
            if not isinstance(file_items, list):
                file_items = [file_items]

            for item in file_items:
                if not hasattr(item, 'filename') or not item.filename:
                    continue

                filename = sanitize_filename(item.filename)
                file_content = item.file.read()
                ext = Path(filename).suffix.lower()

                if ext == '.md':
                    result = handle_md_upload(file_content, filename, company_name)
                    results.append({'file': filename, 'saved_to': result, 'status': 'success'})
                elif ext == '.pdf':
                    result = handle_pdf_upload(file_content, filename, company_name)
                    results.append({'file': filename, 'saved_to': result, 'status': 'success'})
                elif ext == '.zip':
                    zip_results = handle_zip_upload(file_content, filename, company_name)
                    for r in zip_results:
                        results.append({'file': f'{filename} -> {Path(r).name}', 'saved_to': r, 'status': 'success'})
                else:
                    results.append({'file': filename, 'status': 'skipped', 'reason': f'不支持的格式: {ext}'})

            self._send_json({'success': True, 'results': results})

        except ValueError as e:
            self._send_error_json(str(e))
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _upload_publication_pdf(self):
        """接收论文 PDF 上传，提取元信息"""
        try:
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self._send_error_json('请使用 multipart/form-data 格式上传')
                return

            ctype, pdict = cgi.parse_header(content_type)
            if 'boundary' in pdict:
                if isinstance(pdict['boundary'], str):
                    pdict['boundary'] = pdict['boundary'].encode()

            content_length = int(self.headers.get('Content-Length', 0))
            environ = {
                'REQUEST_METHOD': 'POST',
                'CONTENT_TYPE': content_type,
                'CONTENT_LENGTH': str(content_length),
            }

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ=environ
            )

            file_item = form['file'] if 'file' in form else None
            if not file_item or not hasattr(file_item, 'filename') or not file_item.filename:
                self._send_error_json('未找到上传的文件')
                return

            filename = file_item.filename
            file_content = file_item.file.read()

            # Try to extract metadata from PDF
            pub_data = _extract_pdf_metadata(file_content, filename)
            self._send_json(pub_data)

        except Exception as e:
            self._send_error_json(str(e), 500)

    # ─── Chrome Extension API 实现 ──────────────────────────────

    def _ext_read_body(self) -> dict:
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        return json.loads(body.decode('utf-8'))

    def _ext_get_profile(self):
        """GET /api/ext/profile — 返回活跃人员的结构化 profile"""
        try:
            data = parse_profile()
            data['person_id'] = get_active_person_id()
            self._send_json(data)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _ext_get_draft(self):
        """GET /api/ext/draft — 读取插件缓存的 JD/面经"""
        try:
            path = _ext_draft_path()
            if not path.exists():
                self._send_json({'jd': '', 'interview': ''})
                return
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except json.JSONDecodeError:
                data = {}
            self._send_json({
                'jd': _sanitize_ext_draft_value(data.get('jd', ''), 'jd'),
                'interview': _sanitize_ext_draft_value(data.get('interview', ''), 'interview'),
            })
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _ext_save_draft(self):
        """POST /api/ext/draft — 保存插件缓存的 JD/面经"""
        try:
            data = self._ext_read_body()
            jd = str(data.get('jd', '')).strip()
            interview = str(data.get('interview', '')).strip()
            path = _ext_draft_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({'jd': jd, 'interview': interview}, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
            self._send_json({'success': True})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _ext_get_fill_data(self):
        """GET /api/ext/fill-data — 返回完整填充数据包"""
        try:
            profile = parse_profile()
            person_id = get_active_person_id()

            # 读取所有经历
            experiences = []
            exp_dir = _experiences_dir()
            if exp_dir.exists():
                for f in sorted(exp_dir.iterdir()):
                    if f.name in ('_template.md', 'README.md') or f.name.startswith('.'):
                        continue
                    if f.is_file() and f.suffix == '.md':
                        exp = parse_experience_file(f)
                        exp['filename'] = f.name
                        experiences.append(exp)

            self._send_json({
                'person_id': person_id,
                'profile': profile,
                'experiences': experiences,
            })
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _ext_jd_analyze(self):
        """POST /api/ext/jd-analyze — 提取 JD 关键词"""
        try:
            data = self._ext_read_body()
            jd_text = data.get('text', '')
            if not jd_text:
                self._send_error_json('缺少 JD 文本', 400)
                return

            keywords = _extract_jd_keywords(jd_text)
            self._send_json(keywords)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _ext_fill_log(self):
        """POST /api/ext/fill-log — 记录一次填充"""
        try:
            data = self._ext_read_body()
            url = data.get('url', '')
            platform = data.get('platform', 'generic')
            fields_filled = data.get('fields_filled', 0)

            fill_id = ext_log_fill(url, platform, fields_filled)
            self._send_json({'fill_id': fill_id})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _ext_correction(self):
        """POST /api/ext/correction — 记录用户修正"""
        try:
            data = self._ext_read_body()
            fill_id = data.get('fill_id')
            corrections = data.get('corrections', [])

            if not fill_id:
                self._send_error_json('缺少 fill_id', 400)
                return

            for c in corrections:
                ext_log_correction(
                    fill_id=fill_id,
                    field_name=c.get('field_name', ''),
                    field_label=c.get('field_label', ''),
                    original_value=c.get('original_value', ''),
                    corrected_value=c.get('corrected_value', ''),
                    platform=c.get('platform', 'generic'),
                )

            self._send_json({'success': True, 'count': len(corrections)})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _ext_get_field_map(self, platform: str):
        """GET /api/ext/field-map?platform=xxx — 获取字段映射"""
        try:
            mappings = ext_get_field_mappings(platform or None)
            self._send_json({'mappings': mappings})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _ext_update_field_map(self):
        """POST /api/ext/field-map — 更新字段映射"""
        try:
            data = self._ext_read_body()
            mappings = data.get('mappings', [])

            for m in mappings:
                ext_update_field_mapping(
                    platform=m.get('platform', 'generic'),
                    field_selector=m.get('field_selector', ''),
                    field_label=m.get('field_label', ''),
                    mapped_to=m.get('mapped_to', ''),
                    confidence=m.get('confidence', 0.5),
                )

            self._send_json({'success': True, 'count': len(mappings)})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _ext_get_history(self):
        """GET /api/ext/history — 获取填充历史和修正汇总"""
        try:
            history = ext_get_fill_history()
            summary = ext_get_corrections_summary()
            self._send_json({'history': history, 'summary': summary})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _delete_experience(self, filename):
        try:
            safe_name = sanitize_filename(filename)
            target = _experiences_dir() / safe_name

            if not target.exists():
                self._send_error_json(f'文件不存在: {safe_name}', 404)
                return

            if safe_name in ('_template.md', 'README.md'):
                self._send_error_json('不能删除模板文件', 403)
                return

            target.unlink()
            self._send_json({'success': True, 'message': f'已删除 {safe_name}'})
        except Exception as e:
            self._send_error_json(str(e), 500)


# ─── 启动 ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Resume Generator Pro Web UI')
    parser.add_argument('--port', type=int, default=8765, help='服务端口 (默认 8765)')
    parser.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    args = parser.parse_args()

    # 自动迁移到多人模式
    _maybe_migrate()

    # 确保目录存在
    _experiences_dir().mkdir(parents=True, exist_ok=True)
    _work_materials_dir().mkdir(parents=True, exist_ok=True)

    server = HTTPServer(('127.0.0.1', args.port), ResumeHandler)
    url = f'http://localhost:{args.port}'
    print(f'Resume Generator Pro Web UI')
    print(f'服务地址: {url}')
    print(f'数据目录: {DATA_DIR}')
    print(f'按 Ctrl+C 停止服务\n')

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n服务已停止')
        server.server_close()


if __name__ == '__main__':
    main()
