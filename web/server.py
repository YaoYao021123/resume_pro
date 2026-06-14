#!/usr/bin/env python3
from __future__ import annotations
"""Resume Generator Pro — 本地 Web 数据管理服务器

启动方式：python3 web/server.py [--port 8765]
自动打开浏览器 → http://localhost:8765
"""

import argparse
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess as _sp
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

try:
    import cgi
except ModuleNotFoundError:
    class _CgiCompat:
        @staticmethod
        def parse_header(value):
            msg = Message()
            msg['content-type'] = value or ''
            params = msg.get_params(header='content-type') or []
            main = params[0][0] if params else ''
            return main, {k: v for k, v in params[1:]}

        class FieldStorage:
            def __init__(self, *args, **kwargs):
                raise RuntimeError('当前 Python 版本缺少 cgi.FieldStorage；请使用 Python 3.11/3.12 运行文件上传功能')

    cgi = _CgiCompat()

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
    create_application as ext_create_application,
    get_applications as ext_get_applications,
    update_application as ext_update_application,
    delete_application as ext_delete_application,
)
from tools.model_config import (
    get_model_config,
    save_model_config,
    load_local_env,
)
try:
    from backend.auth_billing_service.services.byok_service import ByokService, ByokValidationError
except ImportError:
    ByokService = None
    ByokValidationError = Exception
from tools.language_utils import (
    normalize_language,
    resolve_resume_filenames,
    infer_language_from_output_dir,
)
from tools.skill_derivation import (
    count_skill_values,
    derive_skills_from_text,
    existing_skill_values_from_profile,
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
    timestamp = str(_header_get(headers, 'X-Auth-Timestamp', '')).strip()
    signature = str(_header_get(headers, 'X-Auth-Signature', '')).strip()
    secret = _auth_billing_shared_secret()
    if not secret:
        return None, 'AUTH_BILLING_MISCONFIGURED'
    if not timestamp or not signature:
        return None, 'AUTH_INVALID_SIGNATURE'
    try:
        ts = int(timestamp)
    except ValueError:
        return None, 'AUTH_INVALID_SIGNATURE'
    if abs(int(time.time()) - ts) > 300:
        return None, 'AUTH_INVALID_SIGNATURE'
    message = f'auth|{user_id}|{timestamp}'
    expected = hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None, 'AUTH_INVALID_SIGNATURE'
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


def _mask_secret(value: str) -> str:
    if len(value) <= 8:
        return '*' * len(value)
    return f'{value[:4]}{"*" * (len(value) - 8)}{value[-4:]}'


def _build_byok_ai_config_override(data: dict) -> dict | None:
    byok = data.get('byok')
    if not isinstance(byok, dict):
        return None

    provider = str(byok.get('provider', '')).strip()
    model = str(byok.get('model', '')).strip()
    api_key = str(byok.get('api_key', '')).strip()
    if not provider or not model or not api_key:
        return None

    try:
        if ByokService is not None:
            provider = ByokService._validate_provider(provider)
            api_key = ByokService._validate_api_key(api_key)
    except ByokValidationError:
        return None

    global_config = get_model_config()
    providers = {
        str(item.get('id', '')).strip().lower(): item
        for item in global_config.get('providers', [])
        if isinstance(item, dict)
    }
    provider_meta = providers.get(provider)
    if not isinstance(provider_meta, dict):
        return None
    base_url = str(provider_meta.get('default_base_url', '')).strip()

    return {
        'enabled': True,
        'provider': provider,
        'model': model,
        'base_url': base_url,
        'api_key': api_key,
        'platform_url': str(provider_meta.get('platform_url', '')).strip(),
        'api_style': str(provider_meta.get('api_style', 'openai')).strip() or 'openai',
        'supports_json_object': bool(provider_meta.get('supports_json_object', True)),
        'supports_thinking_off': bool(provider_meta.get('supports_thinking_off', False)),
        'byok_masked_key': _mask_secret(api_key),
        'byok_fingerprint': hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:12],
    }


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
    feedback = data.get('feedback', '').strip()
    selection_plan = data.get('selection_plan') if isinstance(data.get('selection_plan'), dict) else None
    try:
        language = normalize_language(data.get('language'))
    except ValueError as exc:
        return 400, {'error': str(exc), 'error_code': 'INVALID_LANGUAGE'}
    mode = str(data.get('mode', 'platform_key')).strip() or 'platform_key'
    ai_config_override = None

    if not jd_text:
        return 400, {'error': '请输入 JD 内容'}

    if mode == 'byok':
        ai_config_override = _build_byok_ai_config_override(data)
        if ai_config_override is None:
            return 400, {'error': 'BYOK_INVALID', 'error_code': 'BYOK_INVALID'}

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

        if mode != 'byok':
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
                return 503, {'error': 'entitlement reserve failed', 'error_code': 'ENTITLEMENT_RESERVE_FAILED'}

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
            feedback=feedback,
            language=language,
            ai_config_override=ai_config_override,
            selection_plan=selection_plan,
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

_DATE_RANGE_RE = re.compile(
    r'(?P<start>\d{4}[\/\-.年]\d{1,2}(?:[\/\-.月]\d{1,2})?)\s*(?:--|—|–|-)\s*'
    r'(?P<end>至今|present|Present|\d{4}[\/\-.年]\d{1,2}(?:[\/\-.月]\d{1,2})?)'
)
_EMAIL_RE = re.compile(r'[\w.\-+]+@[\w.\-]+\.\w+')
_PHONE_RE = re.compile(r'(\+?\d[\d\s\-\(\)]{7,}\d)')

_TEX_ESCAPE_MAP = {
    '&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#',
    '_': r'\_', '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}',
    '^': r'\textasciicircum{}',
}
_TEX_ESCAPE_RE = re.compile('|'.join(re.escape(k) for k in _TEX_ESCAPE_MAP))


def _tex_escape(text: str) -> str:
    return _TEX_ESCAPE_RE.sub(lambda m: _TEX_ESCAPE_MAP[m.group()], str(text or ''))


def _decode_text_bytes(content: bytes) -> str:
    if not content:
        return ''

    bom_candidates: list[str] = []
    if content.startswith(b'\xef\xbb\xbf'):
        bom_candidates.append('utf-8-sig')
    elif content.startswith(b'\xff\xfe\x00\x00') or content.startswith(b'\x00\x00\xfe\xff'):
        bom_candidates.append('utf-32')
    elif content.startswith(b'\xff\xfe') or content.startswith(b'\xfe\xff'):
        bom_candidates.append('utf-16')

    candidates = bom_candidates + ['utf-8', 'utf-16', 'utf-32', 'gb18030', 'gbk']
    scored: list[tuple[float, int, str]] = []

    for idx, enc in enumerate(candidates):
        try:
            text = content.decode(enc)
        except UnicodeDecodeError:
            continue

        total = max(len(text), 1)
        replacement_ratio = text.count('\ufffd') / total
        printable = sum(1 for ch in text if ch.isprintable() or ch in '\n\r\t')
        printable_ratio = printable / total
        if printable_ratio < 0.85:
            continue
        score = printable_ratio - 2.0 * replacement_ratio
        scored.append((score, -idx, text))

    if scored:
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return scored[0][2]

    return content.decode('latin-1', errors='replace')


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF or
        0x3400 <= code <= 0x4DBF or
        0x20000 <= code <= 0x2A6DF
    )


def _text_quality_score(text: str) -> float:
    if not text:
        return float('-inf')

    total = max(len(text), 1)
    replacement_ratio = text.count('\ufffd') / total
    printable = sum(1 for ch in text if ch.isprintable() or ch in '\n\r\t')
    printable_ratio = printable / total

    exotic_letters = 0
    for ch in text:
        if not ch.isalpha():
            continue
        if _is_cjk_char(ch):
            continue
        if ch.isascii():
            continue
        exotic_letters += 1
    exotic_ratio = exotic_letters / total

    marker_pool = (
        '教育', '实习', '工作经历', '获奖', '技能', '项目',
        'education', 'experience', 'skills', 'gpa', '@',
    )
    lower = text.lower()
    marker_hits = sum(1 for marker in marker_pool if marker in lower)

    # 长文本通常更完整，给轻微加分避免截断提取被选中
    length_boost = min(total / 3000.0, 1.0)

    # CJK 密度奖励：含正确中文字符的文本应比乱码得分更高
    cjk_count = sum(1 for ch in text if _is_cjk_char(ch))
    cjk_ratio = cjk_count / total if total else 0
    cjk_bonus = min(cjk_ratio * 1.5, 1.5)

    return (
        printable_ratio * 2.5 +
        marker_hits * 0.35 +
        length_boost * 0.4 -
        replacement_ratio * 4.0 -
        exotic_ratio * 3.0 +
        cjk_bonus
    )


def _choose_best_text_candidate(candidates: list[tuple[str, str]]) -> tuple[str, str]:
    if not candidates:
        raise ValueError('no text candidates')
    scored = [(_text_quality_score(text), engine, text) for engine, text in candidates if text and text.strip()]
    if not scored:
        raise ValueError('all text candidates are empty')
    scored.sort(key=lambda item: item[0], reverse=True)
    _, engine, text = scored[0]
    return engine, text


def _extract_pdf_text(content: bytes) -> str:
    candidates: list[tuple[str, str]] = []
    errors: list[str] = []

    # 1. pymupdf — 首选，CJK 提取最可靠
    try:
        try:
            import pymupdf as fitz   # >= 1.24.0
        except ImportError:
            import fitz              # < 1.24.0
        doc = fitz.open(stream=content, filetype='pdf')
        text = '\n'.join(page.get_text() or '' for page in doc).strip()
        doc.close()
        if text:
            candidates.append(('pymupdf', text))
    except Exception as e:
        errors.append(f'pymupdf: {e}')

    # 2. pdftotext CLI — 黄金标准，但可能未安装
    pdftotext_bin = shutil.which('pdftotext')
    if pdftotext_bin:
        try:
            with tempfile.NamedTemporaryFile(suffix='.pdf') as tmp:
                tmp.write(content)
                tmp.flush()
                result = _sp.run(
                    [pdftotext_bin, '-layout', tmp.name, '-'],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if result.returncode == 0 and result.stdout.strip():
                    candidates.append(('pdftotext', result.stdout.strip()))
        except Exception as e:
            errors.append(f'pdftotext: {e}')

    # 3. pdfminer
    try:
        from pdfminer.high_level import extract_text as _pdfminer_extract_text
        with tempfile.NamedTemporaryFile(suffix='.pdf') as tmp:
            tmp.write(content)
            tmp.flush()
            text = (_pdfminer_extract_text(tmp.name) or '').strip()
            if text:
                candidates.append(('pdfminer', text))
    except Exception as e:
        errors.append(f'pdfminer: {e}')

    # 4. pdfplumber
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as doc:
            text = '\n'.join((page.extract_text() or '') for page in doc.pages).strip()
            if text:
                candidates.append(('pdfplumber', text))
    except Exception as e:
        errors.append(f'pdfplumber: {e}')

    # 5. pypdf — 最后手段
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(content))
        text = '\n'.join((page.extract_text() or '') for page in reader.pages).strip()
        if text:
            candidates.append(('pypdf', text))
    except Exception as e:
        errors.append(f'pypdf: {e}')

    # 6. PyPDF2 — 与 pypdf 等价，兜底
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(content))
        text = '\n'.join((page.extract_text() or '') for page in reader.pages).strip()
        if text:
            candidates.append(('PyPDF2', text))
    except Exception as e:
        errors.append(f'PyPDF2: {e}')

    if not candidates:
        # 文本层全部失败，尝试 OCR（扫描件 PDF）
        ocr_text = _ocr_pdf_text(content)
        if ocr_text.strip():
            return ocr_text
        detail = '; '.join(errors) if errors else 'no engine available'
        raise ValueError(f'无法解析 PDF 文本: {detail}')

    best_engine, best_text = _choose_best_text_candidate(candidates)

    # 低质量 CJK 输出检测：exotic_ratio 过高说明文本仍然乱码
    total = max(len(best_text), 1)
    exotic_letters = 0
    for ch in best_text:
        if not ch.isalpha():
            continue
        if _is_cjk_char(ch):
            continue
        if ch.isascii():
            continue
        exotic_letters += 1
    exotic_ratio = exotic_letters / total
    if exotic_ratio > 0.1:
        raise ValueError(
            'PDF 中文文本解析质量不佳（可能是 XeLaTeX 子集字体）。'
            '建议上传 .docx 或 .txt 格式。'
        )

    return best_text


_OCR_INSTANCE = None


def _get_paddle_ocr():
    """懒加载 PaddleOCR 单例实例，未安装时抛 ValueError"""
    global _OCR_INSTANCE
    if _OCR_INSTANCE is None:
        try:
            from paddleocr import PaddleOCR
        except ImportError:
            raise ValueError('PaddleOCR 未安装，无法识别图片/扫描件。请运行 pip3 install paddleocr paddlepaddle Pillow')
        _OCR_INSTANCE = PaddleOCR(use_angle_cls=True, lang='ch', show_log=False)
    return _OCR_INSTANCE


def _ocr_pdf_text(content: bytes) -> str:
    """用 PaddleOCR 从 PDF/图片中提取文本（扫描件降级方案）"""
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        ocr = _get_paddle_ocr()
        doc = fitz.open(stream=content, filetype='pdf')
        all_text = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes('png')
            result = ocr.ocr(img_bytes, cls=True)
            page_text = '\n'.join(line[1][0] for line in result[0] if line[1][0]) if result and result[0] else ''
            all_text.append(page_text)
        doc.close()
        return '\n'.join(all_text).strip()
    except ImportError:
        return ''
    except Exception:
        return ''


def _ocr_image_text(content: bytes, ext: str) -> str:
    """用 PaddleOCR 从图片中提取文本"""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        raise ValueError('Pillow/numpy 未安装，无法识别图片文件。请上传 .pdf .docx .md .txt 格式')
    ocr = _get_paddle_ocr()
    img = Image.open(io.BytesIO(content))
    img_array = np.array(img)
    result = ocr.ocr(img_array, cls=True)
    return '\n'.join(line[1][0] for line in result[0] if line[1][0]) if result and result[0] else ''


def _call_ai_simple_chat(prompt: str, max_tokens: int = 3000) -> str:
    """用当前配置的 AI 模型执行简单 chat，返回 assistant content"""
    cfg = get_model_config()
    if not cfg.get('enabled') or not cfg.get('api_key'):
        raise RuntimeError('AI 未启用')
    api_url = cfg['base_url'].rstrip('/') + '/chat/completions'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {cfg['api_key']}",
    }
    payload = {
        'model': cfg['model'],
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.1,
        'max_tokens': max_tokens,
    }
    if cfg.get('supports_thinking_off'):
        payload['thinking'] = {'type': 'disabled'}
    req = urllib.request.Request(api_url, data=json.dumps(payload).encode(), headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read())
    return body['choices'][0]['message'].get('content', '')


_AI_PARSE_PROMPT = """你是一个简历解析专家。请将以下简历原文解析为结构化 JSON，格式如下：
{
  "basic": {"name_zh": "", "name_en": "", "email": "", "phone": ""},
  "education": [{"school": "", "degree": "", "major": "", "time_start": "", "time_end": "", "gpa": "", "rank": "", "courses": ""}],
  "experiences": [{"company": "", "city": "", "department": "", "role": "", "time_start": "", "time_end": "", "tags": "", "bullets": []}],
  "awards": [{"name": "", "issuer": "", "date": ""}],
  "skills": {"tech": "", "software": "", "languages": ""}
}
规则：
- bullets 是字符串数组，每条无句号结尾
- tags 用逗号分隔
- 时间格式 YYYY/MM 或 YYYY
- 只输出 JSON，不要额外解释

简历原文：
"""


def _ai_parse_resume_text(text: str) -> dict:
    """用 AI 模型解析简历文本为结构化数据，失败返回空 dict"""
    prompt = _AI_PARSE_PROMPT + text
    raw = _call_ai_simple_chat(prompt)
    # 提取 JSON（AI 可能在前后加 markdown 标记）
    json_match = re.search(r'\{[\s\S]*\}', raw)
    if not json_match:
        return {}
    try:
        parsed = json.loads(json_match.group())
        if not isinstance(parsed, dict):
            return {}
        return parsed
    except json.JSONDecodeError:
        return {}


_AI_PROFILE_SUGGEST_PROMPT = """你是一个简历资料补全助手。请基于用户现有资料、补充材料和目标岗位信息，生成可写入资料表单的 JSON 补充建议。

输出格式：
{
  "summary": ["简短说明补充了什么"],
  "warnings": ["无法从材料确认、需要用户核实的点"],
  "profile_patch": {
    "basic": {"name_zh": "", "name_en": "", "email": "", "phone": "", "linkedin": "", "github": "", "website": ""},
    "directions": {"primary": "", "secondary": ""},
    "education": [{"school": "", "degree": "", "major": "", "department": "", "time_start": "", "time_end": "", "gpa": "", "rank": "", "courses": ""}],
    "skills": {"tech": "", "software": "", "languages": ""},
    "projects": [{"name": "", "role": "", "time_start": "", "time_end": "", "desc": "", "tags": ""}],
    "campus_experiences": [{"name": "", "role": "", "time_start": "", "time_end": "", "highlights": "", "tags": ""}],
    "awards": [{"name": "", "issuer": "", "date": ""}],
    "publications": [{"title": "", "authors": "", "venue": "", "year": "", "description": ""}]
  }
}

规则：
- 只输出 JSON，不要 markdown，不要额外解释
- 优先补齐空字段，不要覆盖用户已有明确内容
- 技能可以根据补充材料/JD/求职方向提出建议，但必须放入 warnings 提醒用户确认
- 不要编造学校、公司、奖项、论文、联系方式、日期、量化成果
- 项目/校园经历只在材料中有明确事实时生成；bullet/成果要点结尾不加句号
- tags 使用逗号分隔，时间格式使用 YYYY/MM 或 YYYY

现有资料：
{profile_json}

目标岗位/JD：
{jd_text}

补充材料：
{source_text}
"""


def _extract_json_object(text: str) -> dict:
    json_match = re.search(r'\{[\s\S]*\}', text or '')
    if not json_match:
        return {}
    try:
        parsed = json.loads(json_match.group())
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _clean_str(value, max_len: int = 1000) -> str:
    if value is None:
        return ''
    text = str(value).replace('\x00', '').strip()
    return text[:max_len]


def _clean_mapping(raw: dict, allowed: list[str], max_len: int = 1000) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {
        key: _clean_str(raw.get(key, ''), max_len=max_len)
        for key in allowed
        if _clean_str(raw.get(key, ''), max_len=max_len)
    }


def _clean_items(raw_items, allowed: list[str], identity_keys: tuple[str, ...], limit: int = 5) -> list[dict]:
    if not isinstance(raw_items, list):
        return []
    cleaned = []
    for item in raw_items:
        obj = _clean_mapping(item, allowed)
        if any(obj.get(key) for key in identity_keys):
            cleaned.append(obj)
        if len(cleaned) >= limit:
            break
    return cleaned


def sanitize_profile_suggestion(raw: dict) -> dict:
    """Normalize AI profile suggestions to the web profile payload shape."""
    if not isinstance(raw, dict):
        raw = {}
    patch = raw.get('profile_patch') if isinstance(raw.get('profile_patch'), dict) else raw
    if not isinstance(patch, dict):
        patch = {}
    result = {
        'summary': [],
        'warnings': [],
        'profile_patch': {
            'basic': _clean_mapping(
                patch.get('basic', {}),
                ['name_zh', 'name_en', 'email', 'phone', 'linkedin', 'github', 'website'],
                max_len=200,
            ),
            'directions': _clean_mapping(
                patch.get('directions', {}),
                ['primary', 'secondary'],
                max_len=300,
            ),
            'education': _clean_items(
                patch.get('education', []),
                ['school', 'degree', 'major', 'department', 'time_start', 'time_end', 'gpa', 'rank', 'courses'],
                ('school',),
                limit=3,
            ),
            'skills': _clean_mapping(
                patch.get('skills', {}),
                ['tech', 'software', 'languages'],
                max_len=500,
            ),
            'projects': _clean_items(
                patch.get('projects', []),
                ['name', 'role', 'time_start', 'time_end', 'desc', 'tags'],
                ('name',),
                limit=5,
            ),
            'campus_experiences': _clean_items(
                patch.get('campus_experiences', []),
                ['name', 'role', 'time_start', 'time_end', 'highlights', 'tags'],
                ('name',),
                limit=5,
            ),
            'awards': _clean_items(
                patch.get('awards', []),
                ['name', 'issuer', 'date'],
                ('name',),
                limit=5,
            ),
            'publications': _clean_items(
                patch.get('publications', []),
                ['title', 'authors', 'venue', 'year', 'description'],
                ('title',),
                limit=5,
            ),
        },
    }
    for key in ('summary', 'warnings'):
        values = raw.get(key, [])
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            result[key] = [_clean_str(item, max_len=300) for item in values if _clean_str(item, max_len=300)][:6]
    return result


def ai_suggest_profile_completion(profile: dict, source_text: str = '', jd_text: str = '') -> dict:
    prompt = (
        _AI_PROFILE_SUGGEST_PROMPT
        .replace('{profile_json}', json.dumps(profile or {}, ensure_ascii=False, indent=2))
        .replace('{jd_text}', _clean_str(jd_text, max_len=5000))
        .replace('{source_text}', _clean_str(source_text, max_len=12000))
    )
    raw = _call_ai_simple_chat(prompt, max_tokens=4000)
    parsed = _extract_json_object(raw)
    suggestion = sanitize_profile_suggestion(parsed)
    suggestion['raw_model_output'] = raw[:2000]
    return suggestion


def _extract_docx_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        try:
            xml_bytes = zf.read('word/document.xml')
        except KeyError as e:
            raise ValueError('DOCX 缺少 word/document.xml') from e
    root = ET.fromstring(xml_bytes)
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    lines = []
    for para in root.findall('.//w:p', ns):
        segs = []
        for tnode in para.findall('.//w:t', ns):
            if tnode.text:
                segs.append(tnode.text)
        line = ''.join(segs).strip()
        if line:
            lines.append(line)
    return '\n'.join(lines)


def extract_text_from_upload(filename: str, content: bytes) -> str:
    ext = Path(filename or '').suffix.lower()
    if ext in {'.txt', '.md'}:
        return _decode_text_bytes(content).strip()
    if ext == '.docx':
        return _extract_docx_text(content).strip()
    if ext in {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}:
        text = _ocr_image_text(content, ext)
        if not text.strip():
            raise ValueError('图片内容为空或无法识别')
        return text
    if ext == '.pdf':
        try:
            return _extract_pdf_text(content).strip()
        except ValueError:
            # 文本引擎全部失败（扫描件 PDF），尝试 OCR
            ocr_text = _ocr_pdf_text(content)
            if ocr_text.strip():
                return ocr_text
            raise
    raise ValueError(f'不支持的文件格式: {ext or "unknown"}')


def _normalize_ym(date_str: str) -> str:
    s = str(date_str or '').strip()
    if not s:
        return ''
    if s in ('至今', 'present', 'Present'):
        return '至今'
    m = re.search(r'(\d{4})[\/\-.年](\d{1,2})', s)
    if not m:
        return s
    return f'{m.group(1)}/{int(m.group(2)):02d}'


def _pick_name(lines: list[str], used: set[int]) -> tuple[str, str]:
    for idx, line in enumerate(lines):
        if idx in used:
            continue
        if _EMAIL_RE.search(line) or _PHONE_RE.search(line):
            continue
        if len(line) > 40:
            continue
        if any(key in line for key in ('教育', '经历', '技能', '奖项', '项目', '工作')):
            continue
        used.add(idx)
        if re.search(r'[\u4e00-\u9fff]', line):
            return line.strip(), ''
        return '', line.strip()
    return '', ''


def parse_resume_text_to_structured(text: str) -> dict:
    raw_lines = [ln.strip() for ln in str(text or '').splitlines()]
    lines = [ln for ln in raw_lines if ln]
    used: set[int] = set()
    data = {
        'basic': {'name_zh': '', 'name_en': '', 'email': '', 'phone': ''},
        'education': [],
        'experiences': [],
        'awards': [],
        'skills': {'tech': '', 'software': '', 'languages': ''},
        'pending_text': '',
    }

    current_section = ''
    current_exp = None

    for idx, line in enumerate(lines):
        lower = line.lower()
        if '教育' in line:
            current_section = 'education'
            used.add(idx)
            continue
        if any(k in line for k in ('实习经历', '工作经历', '职业经历')):
            current_section = 'experience'
            current_exp = None
            used.add(idx)
            continue
        if any(k in line for k in ('技能', 'skills')):
            current_section = 'skills'
            used.add(idx)
            continue
        if '获奖' in line:
            current_section = 'awards'
            used.add(idx)
            continue

        email_match = _EMAIL_RE.search(line)
        if email_match and not data['basic']['email']:
            data['basic']['email'] = email_match.group().strip()
            used.add(idx)
            continue

        phone_match = _PHONE_RE.search(line)
        if phone_match and not data['basic']['phone']:
            data['basic']['phone'] = phone_match.group(1).strip()
            used.add(idx)
            continue

        if current_section == 'skills':
            values = [seg.strip() for seg in re.split(r'[，,;；]', line) if seg.strip()]
            if values:
                if not data['skills']['tech']:
                    data['skills']['tech'] = ', '.join(values)
                else:
                    data['skills']['software'] = ', '.join(values)
                used.add(idx)
                continue

        dr = _DATE_RANGE_RE.search(line)
        if dr:
            start = _normalize_ym(dr.group('start'))
            end = _normalize_ym(dr.group('end'))
            prefix = line[:dr.start()].strip(' -—|｜')
            if current_section == 'education' or any(s in line for s in ('大学', '学院', '学校')):
                degree = ''
                for dg in ('博士', '硕士', '本科', '大专'):
                    if dg in line:
                        degree = dg
                        break
                school = ''
                for token in prefix.split():
                    if any(k in token for k in ('大学', '学院', '学校')):
                        school = token
                        break
                major = prefix.replace(school, '').replace(degree, '').strip()
                data['education'].append({
                    'school': school or prefix,
                    'degree': degree,
                    'major': major,
                    'department': '',
                    'time_start': start,
                    'time_end': end,
                    'gpa': '',
                    'rank': '',
                    'courses': '',
                })
                used.add(idx)
                continue

            parts = [p.strip() for p in re.split(r'[|｜]', prefix) if p.strip()]
            if len(parts) >= 2:
                company, role = parts[0], parts[1]
            else:
                tokens = [t for t in prefix.split() if t]
                company = tokens[0] if tokens else '导入经历'
                role = ' '.join(tokens[1:]) if len(tokens) > 1 else '岗位'
            current_exp = {
                'company': company,
                'city': '',
                'department': '',
                'role': role,
                'time_start': start,
                'time_end': end,
                'tags': '',
                'notes': '',
                'bullets': [],
            }
            data['experiences'].append(current_exp)
            used.add(idx)
            continue

        if re.match(r'^[\-\*\u2022]\s*', line) and current_exp is not None:
            bullet = re.sub(r'^[\-\*\u2022]\s*', '', line).strip()
            if bullet:
                current_exp['bullets'].append(bullet)
                used.add(idx)
            continue

        if current_section == 'awards' and ('|' in line or '奖' in line):
            parts = [p.strip() for p in line.split('|')]
            if parts:
                data['awards'].append({
                    'name': parts[0],
                    'issuer': parts[1] if len(parts) > 1 else '',
                    'date': parts[2] if len(parts) > 2 else '',
                })
                used.add(idx)
                continue

    name_zh, name_en = _pick_name(lines, used)
    data['basic']['name_zh'] = name_zh
    data['basic']['name_en'] = name_en

    pending_lines = [line for idx, line in enumerate(lines) if idx not in used]
    data['pending_text'] = '\n'.join(pending_lines).strip()
    return data


def _sanitize_dir_part(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[\/\\:*?"<>|]+', '', str(value or '').strip())
    cleaned = cleaned.replace(' ', '')
    return cleaned or fallback


def create_import_draft_dir(company: str = '', role: str = '', language: str = 'zh') -> str:
    language = normalize_language(language)
    tex_filename, _ = resolve_resume_filenames(language)
    company_part = _sanitize_dir_part(company, '导入简历')
    role_part = _sanitize_dir_part(role, '草稿')
    base = f'{company_part}_{role_part}_{datetime.now().strftime("%Y%m%d")}'
    out_dir = _output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    dir_name = base
    seq = 1
    while (out_dir / dir_name).exists():
        seq += 1
        dir_name = f'{base}_{seq}'
    target = out_dir / dir_name
    target.mkdir(parents=True, exist_ok=True)

    template_dir = PROJECT_ROOT / 'latex_src' / 'resume'
    for f in ('resume.cls', 'zh_CN-Adobefonts_external.sty', 'linespacing_fix.sty'):
        src = template_dir / f
        if src.exists():
            shutil.copy2(str(src), str(target / f))
    src_fonts = template_dir / 'fonts'
    dst_fonts = target / 'fonts'
    if src_fonts.exists() and not dst_fonts.exists():
        try:
            os.symlink(src_fonts.resolve(), dst_fonts)
        except OSError:
            shutil.copytree(str(src_fonts), str(dst_fonts))
    template_tex = template_dir / tex_filename
    if template_tex.exists():
        shutil.copy2(str(template_tex), str(target / tex_filename))
    else:
        raise FileNotFoundError(f'模板文件不存在: {template_tex.name}')

    context_path = target / 'generation_context.json'
    context_payload = {
        'company': company,
        'role': role,
        'engine': 'import',
        'jd_text': '',
        'interview_text': '',
        'language': language,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
    }
    context_path.write_text(json.dumps(context_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return dir_name


def _format_resume_range(start: str, end: str) -> str:
    s = _normalize_ym(start)
    e = _normalize_ym(end)
    if s and e:
        return f'{s} -- {e}'
    return s or e or ''


def _load_generation_context(out_dir: Path) -> dict:
    context_path = out_dir / 'generation_context.json'
    if not context_path.exists():
        return {}
    try:
        return json.loads(context_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_generation_context(out_dir: Path, context: dict) -> None:
    context_path = out_dir / 'generation_context.json'
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')


def _normalize_fill_ratio_value(value) -> float:
    try:
        ratio = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return ratio / 100 if ratio > 1.0 else ratio


def _measure_resume_metrics(
    out_dir: Path,
    language: str,
    *,
    context: dict | None = None,
    existing_review: dict | None = None,
    compile_result: dict | None = None,
) -> dict:
    """Return page/fill metrics, measuring the TeX file when cached data is missing."""
    context = context if isinstance(context, dict) else {}
    existing_review = existing_review if isinstance(existing_review, dict) else {}
    compile_result = compile_result if isinstance(compile_result, dict) else {}

    fill_ratio = _normalize_fill_ratio_value(
        compile_result.get('fill_rate')
        or context.get('fill_ratio')
        or existing_review.get('fill_ratio')
        or 0.0
    )
    try:
        page_count = int(
            compile_result.get('pages')
            or existing_review.get('page_count')
            or context.get('page_count')
            or context.get('pages')
            or 0
        )
    except (TypeError, ValueError):
        page_count = 0

    metrics = {
        'fill_ratio': fill_ratio,
        'page_count': page_count,
        'status': '',
        'message': '',
    }
    if fill_ratio and page_count:
        return metrics

    try:
        _, tex_path, _ = _resolve_resume_paths(out_dir, language)
        if not tex_path.exists():
            metrics['status'] = 'missing_tex'
            metrics['message'] = f'未找到 {tex_path.name}，无法检测页数和填充率'
            return metrics

        from tools.page_fill_check import check_page_fill
        measured = check_page_fill(str(out_dir), _find_xelatex(), tex_path.name)
        measured_fill = _normalize_fill_ratio_value(measured.get('ratio') or 0.0)
        try:
            measured_pages = int(measured.get('page_count') or 0)
        except (TypeError, ValueError):
            measured_pages = 0
        if measured_fill:
            metrics['fill_ratio'] = measured_fill
        if measured_pages:
            metrics['page_count'] = measured_pages
        metrics['status'] = str(measured.get('status') or '')
        metrics['message'] = str(measured.get('message') or '')
    except Exception as exc:
        metrics['status'] = 'measure_error'
        metrics['message'] = str(exc)
    return metrics


def _with_visual_review_screenshot_path(out_dir: Path, visual_review: dict) -> dict:
    if not isinstance(visual_review, dict) or not visual_review:
        return {}
    enriched = dict(visual_review)
    shots = enriched.get('screenshots') if isinstance(enriched.get('screenshots'), list) else []
    if shots:
        try:
            enriched['screenshot_path'] = str((out_dir / shots[0]).relative_to(_output_dir()))
        except ValueError:
            enriched['screenshot_path'] = ''
    enriched.setdefault('agent_feedback', _build_visual_review_agent_feedback(enriched))
    return enriched


def _load_visual_review_for_output(out_dir: Path, context: dict | None = None) -> dict:
    context = context if isinstance(context, dict) else {}
    visual_review = context.get('visual_review') if isinstance(context.get('visual_review'), dict) else {}
    if not visual_review:
        review_json = out_dir / 'visual_review' / 'visual_review.json'
        if review_json.exists():
            try:
                loaded_review = json.loads(review_json.read_text(encoding='utf-8'))
                if isinstance(loaded_review, dict):
                    visual_review = loaded_review
            except Exception:
                visual_review = {}
    return _with_visual_review_screenshot_path(out_dir, visual_review)


def _resolve_gallery_output_dir(dir_name: str) -> Path:
    safe_name = str(dir_name or '').strip()
    if not safe_name or '..' in safe_name or '/' in safe_name or safe_name.startswith('.'):
        raise ValueError('非法路径')
    out_dir = _output_dir() / safe_name
    try:
        out_dir.resolve().relative_to(_output_dir().resolve())
    except ValueError as exc:
        raise ValueError('非法路径') from exc
    if not out_dir.exists() or not out_dir.is_dir():
        raise FileNotFoundError('目录不存在')
    return out_dir


def _build_visual_review_agent_feedback(visual_review: dict) -> dict:
    review = visual_review if isinstance(visual_review, dict) else {}
    status = str(review.get('status') or '').strip()
    issues = [str(i) for i in (review.get('issues') or []) if str(i).strip()]
    issue_set = set(issues)
    fill_ratio = _normalize_fill_ratio_value(review.get('fill_ratio', 0.0))
    try:
        page_count = int(review.get('page_count') or 0)
    except (TypeError, ValueError):
        page_count = 0

    actions = []
    prompt_lines = ['根据视觉 QA 结果进行下一轮简历优化：']

    if page_count and page_count != 1 or 'page_count_not_one' in issue_set:
        actions.append('控制到单页')
        prompt_lines.append('- 当前 PDF 不是稳定单页，请优先压缩排版和内容，确保最终只有 1 页')
    if fill_ratio > 1.0 or 'overflow' in issue_set:
        actions.append('压缩溢出')
        prompt_lines.append('- 当前内容溢出或过满，请缩短低相关 bullet、降低段落密度，并保留最匹配 JD 的经历')
    if 0 < fill_ratio < 0.95 or 'underfilled' in issue_set:
        actions.append('补足页面')
        prompt_lines.append('- 当前页面偏空，请优先补充与 JD 相关的项目/实习要点，适度展开高价值 bullet')
    if 'pdf_missing' in issue_set:
        actions.append('修复 PDF 输出')
        prompt_lines.append('- PDF 缺失，请先修复 LaTeX 编译或输出路径，再重新做视觉 QA')
    if 'render_failed' in issue_set:
        actions.append('修复截图渲染')
        prompt_lines.append('- PDF 截图渲染失败，请检查 PDF 是否可打开、渲染工具是否可用')

    if status == 'skipped':
        actions.append('补齐截图环境')
        prompt_lines.append('- 本轮跳过截图渲染，请补齐 pdftoppm 后再次运行视觉 QA；内容可暂按填充率和页数判断')
    elif status == 'pass' and not actions:
        actions.append('保持通过状态')
        prompt_lines.append('- 当前视觉 QA 已通过，请保持单页、填充率和整体结构稳定，只做必要的轻微措辞或排版优化')
    elif not actions:
        actions.append('人工复核截图')
        prompt_lines.append('- QA 状态未给出明确问题，请先查看截图，围绕可读性、拥挤度和留白进行人工复核')

    if fill_ratio:
        prompt_lines.append(f'- 当前填充率约为 {fill_ratio * 100:.1f}%')
    if page_count:
        prompt_lines.append(f'- 当前页数为 {page_count} 页')
    prompt_lines.append('- 不要编造经历或数据；只基于已有资料筛选、改写和调优')

    return {
        'summary': '；'.join(actions),
        'actions': actions,
        'prompt': '\n'.join(prompt_lines),
    }


def _build_resume_quality_report(
    *,
    pdf_exists: bool,
    visual_review: dict,
    version_count: int = 0,
    context: dict | None = None,
) -> dict:
    """Aggregate delivery readiness signals for one generated resume."""
    review = visual_review if isinstance(visual_review, dict) else {}
    context = context if isinstance(context, dict) else {}
    status = str(review.get('status') or '').strip()
    issues = [str(i) for i in (review.get('issues') or []) if str(i).strip()]
    issue_set = set(issues)
    fill_ratio = _normalize_fill_ratio_value(
        review.get('fill_ratio')
        or context.get('fill_ratio')
        or 0.0
    )
    try:
        page_count = int(review.get('page_count') or context.get('pages') or 0)
    except (TypeError, ValueError):
        page_count = 0
    agent_feedback = review.get('agent_feedback') if isinstance(review.get('agent_feedback'), dict) else {}
    has_screenshot = bool(review.get('screenshot_path') or review.get('screenshots'))

    checks = []

    def add_check(key: str, label: str, check_status: str, detail: str) -> None:
        checks.append({
            'key': key,
            'label': label,
            'status': check_status,
            'detail': detail,
        })

    add_check(
        'pdf',
        'PDF 输出',
        'pass' if pdf_exists else 'fail',
        '已生成可预览 PDF' if pdf_exists else '未找到 PDF，需要先修复编译或输出路径',
    )

    if not status:
        add_check('visual_review', '视觉 QA', 'pending', '尚未运行视觉 QA')
    elif status == 'pass':
        add_check('visual_review', '视觉 QA', 'pass', review.get('message') or '视觉 QA 已通过')
    elif status == 'skipped':
        add_check('visual_review', '视觉 QA', 'warning', review.get('message') or '截图环境缺失，需补齐后复检')
    elif status == 'error':
        add_check('visual_review', '视觉 QA', 'fail', review.get('message') or '视觉 QA 执行失败')
    else:
        add_check('visual_review', '视觉 QA', 'warning', review.get('message') or '视觉 QA 需要人工复核')

    if page_count:
        add_check(
            'single_page',
            '单页约束',
            'pass' if page_count == 1 else 'fail',
            f'当前 {page_count} 页' if page_count != 1 else '当前为 1 页',
        )
    else:
        add_check('single_page', '单页约束', 'pending', '页数尚未检测')

    if fill_ratio:
        fill_status = 'pass' if 0.95 <= fill_ratio <= 1.0 else ('fail' if fill_ratio > 1.0 else 'warning')
        if fill_ratio > 1.0:
            fill_detail = f'填充率 {fill_ratio * 100:.1f}%，内容可能溢出'
        elif fill_ratio < 0.95:
            fill_detail = f'填充率 {fill_ratio * 100:.1f}%，页面偏空'
        else:
            fill_detail = f'填充率 {fill_ratio * 100:.1f}%，处于理想范围'
        add_check('fill_ratio', '页面填充率', fill_status, fill_detail)
    else:
        add_check('fill_ratio', '页面填充率', 'pending', '填充率尚未检测')

    if has_screenshot:
        add_check('screenshot', '截图资产', 'pass', '已生成可视化截图，可用于人工复核')
    elif status:
        add_check('screenshot', '截图资产', 'warning', '缺少截图资产，建议重新运行视觉 QA')
    else:
        add_check('screenshot', '截图资产', 'pending', '等待视觉 QA 生成截图')

    add_check(
        'version_history',
        '版本记录',
        'pass' if version_count > 0 else 'warning',
        f'已有 {version_count} 个版本快照' if version_count > 0 else '尚未形成版本快照，建议在关键修改后保存版本',
    )
    add_check(
        'agent_feedback',
        'Agent 反馈',
        'pass' if agent_feedback.get('prompt') else 'warning',
        '已生成下一轮优化提示' if agent_feedback.get('prompt') else '缺少可直接用于重生成的 QA 反馈',
    )

    has_fail = any(c['status'] == 'fail' for c in checks) or 'pdf_missing' in issue_set or 'render_failed' in issue_set
    has_warning = any(c['status'] in {'warning', 'pending'} for c in checks)
    ready = (
        pdf_exists
        and status == 'pass'
        and page_count == 1
        and 0.95 <= fill_ratio <= 1.0
        and not has_fail
    )

    if ready:
        report_status = 'ready'
        label = '可投递'
        summary = 'PDF 单页、填充率理想且视觉 QA 通过'
        next_action = '投递前人工复核'
    elif has_fail:
        report_status = 'fix'
        label = '需修复'
        if not pdf_exists or 'pdf_missing' in issue_set:
            next_action = '修复编译或 PDF 输出'
        elif fill_ratio > 1.0 or page_count > 1:
            next_action = '按 QA 反馈重生成'
        elif 'render_failed' in issue_set:
            next_action = '修复截图渲染后复检'
        else:
            next_action = '修复失败项后复检'
        summary = '存在会阻断投递的质量问题'
    elif has_warning:
        report_status = 'review'
        label = '需复核'
        next_action = '运行视觉 QA' if not status else '查看截图并按 QA 反馈优化'
        summary = '简历可继续推进，但仍有复核项'
    else:
        report_status = 'review'
        label = '需复核'
        next_action = '人工复核'
        summary = '质量状态不完整，建议人工复核'

    return {
        'status': report_status,
        'label': label,
        'summary': summary,
        'next_action': next_action,
        'fill_ratio': fill_ratio,
        'fill_label': f'{fill_ratio * 100:.1f}%' if fill_ratio else '',
        'page_count': page_count,
        'pdf_exists': bool(pdf_exists),
        'version_count': int(version_count or 0),
        'issues': issues,
        'checks': checks,
    }


def _build_resume_workflow_status(
    *,
    pdf_exists: bool,
    visual_review: dict,
    quality_report: dict,
    version_count: int = 0,
    context: dict | None = None,
) -> dict:
    """Describe the user-facing resume production loop as ordered steps."""
    review = visual_review if isinstance(visual_review, dict) else {}
    report = quality_report if isinstance(quality_report, dict) else {}
    context = context if isinstance(context, dict) else {}
    review_status = str(review.get('status') or '').strip()
    report_status = str(report.get('status') or '').strip()
    agent_feedback = review.get('agent_feedback') if isinstance(review.get('agent_feedback'), dict) else {}
    engine = str(context.get('engine') or '').strip()

    def step(key: str, label: str, status: str, detail: str, action: str = '') -> dict:
        return {
            'key': key,
            'label': label,
            'status': status,
            'detail': detail,
            'action': action,
        }

    steps = [
        step(
            'generate',
            '生成 PDF',
            'pass' if pdf_exists else 'pending',
            f'已通过 {engine or "本地流程"} 生成 PDF' if pdf_exists else '等待输入 JD 后启动本地 Agent 生成',
            '' if pdf_exists else '开始生成',
        )
    ]

    if not review_status:
        steps.append(step('visual_review', '视觉检查', 'pending', '尚未运行视觉 QA', '运行视觉 QA'))
    elif review_status == 'pass':
        steps.append(step('visual_review', '视觉检查', 'pass', review.get('message') or '视觉 QA 已通过'))
    elif review_status == 'error':
        steps.append(step('visual_review', '视觉检查', 'fail', review.get('message') or '视觉 QA 执行失败', '修复 QA 后复检'))
    else:
        steps.append(step('visual_review', '视觉检查', 'warning', review.get('message') or '视觉 QA 需要复核', '查看截图'))

    if agent_feedback.get('prompt'):
        feedback_status = 'pass'
        feedback_detail = '已生成可直接用于下一轮重生成的 QA 反馈'
        feedback_action = ''
    elif review_status:
        feedback_status = 'warning'
        feedback_detail = '已有 QA 状态，但缺少可直接复用的反馈提示'
        feedback_action = '补齐 QA 反馈'
    else:
        feedback_status = 'pending'
        feedback_detail = '等待视觉 QA 生成反馈'
        feedback_action = '先运行视觉 QA'
    steps.append(step('feedback_loop', '反馈迭代', feedback_status, feedback_detail, feedback_action))

    steps.append(step(
        'version',
        '版本快照',
        'pass' if version_count > 0 else 'warning',
        f'已有 {version_count} 个版本，可回滚和对比' if version_count > 0 else '建议在关键修改后保存版本快照',
        '' if version_count > 0 else '保存版本',
    ))

    if report_status == 'ready':
        delivery_status = 'pass'
        delivery_detail = report.get('summary') or '简历已达到可投递状态'
        delivery_action = report.get('next_action') or '投递前人工复核'
    elif report_status == 'fix':
        delivery_status = 'fail'
        delivery_detail = report.get('summary') or '存在阻断投递的问题'
        delivery_action = report.get('next_action') or '按 QA 反馈修复'
    elif report_status:
        delivery_status = 'warning'
        delivery_detail = report.get('summary') or '仍有需要复核的项目'
        delivery_action = report.get('next_action') or '继续复核'
    else:
        delivery_status = 'pending'
        delivery_detail = '等待质量报告'
        delivery_action = '生成质量报告'
    steps.append(step('delivery', '投递准备', delivery_status, delivery_detail, delivery_action))

    status_order = {'pass': 1.0, 'warning': 0.55, 'pending': 0.0, 'fail': 0.0}
    progress = round(sum(status_order.get(s['status'], 0.0) for s in steps) / len(steps) * 100)
    active_step = next((s for s in steps if s['status'] != 'pass'), steps[-1])

    if not pdf_exists:
        workflow_status = 'not_started'
        label = '待生成'
    elif any(s['status'] == 'fail' for s in steps):
        workflow_status = 'blocked'
        label = '需修复'
    elif all(s['status'] == 'pass' for s in steps):
        workflow_status = 'complete'
        label = '闭环完成'
    elif report_status == 'ready':
        workflow_status = 'ready'
        label = '可投递'
    else:
        workflow_status = 'in_progress'
        label = '进行中'

    return {
        'status': workflow_status,
        'label': label,
        'progress': progress,
        'active_step': active_step,
        'next_action': active_step.get('action') or report.get('next_action') or '',
        'steps': steps,
    }


def _agent_user_phase(key: str, label: str, status: str, detail: str = '') -> dict:
    return {
        'key': key,
        'label': label,
        'status': status,
        'detail': detail,
    }


def _build_agent_user_status(payload: dict) -> dict:
    """Build a safe, user-facing status summary for an Agent job."""
    status = str(payload.get('status') or '').strip()
    step = str(payload.get('step') or '').strip()
    progress = int(payload.get('progress') or 0)
    task_summary = payload.get('task_summary') if isinstance(payload.get('task_summary'), dict) else {}
    result = payload.get('result') if isinstance(payload.get('result'), dict) else {}
    quality = result.get('quality_report') if isinstance(result.get('quality_report'), dict) else {}
    workflow = result.get('workflow_status') if isinstance(result.get('workflow_status'), dict) else {}
    visual = result.get('visual_review') if isinstance(result.get('visual_review'), dict) else {}

    company = str(task_summary.get('company') or payload.get('company') or result.get('company') or '').strip()
    role = str(task_summary.get('role') or payload.get('role') or result.get('role') or '').strip()
    agent = str(task_summary.get('agent') or payload.get('agent') or '').strip()
    language = str(task_summary.get('language') or payload.get('language') or result.get('language') or '').strip()
    input_bits = [
        company or '',
        role or '',
        '含面经' if task_summary.get('has_interview') else '',
        '含确认方案' if task_summary.get('has_selection_plan') else '',
        '含修改意见' if task_summary.get('has_feedback') else '',
    ]
    input_summary = ' · '.join(bit for bit in input_bits if bit) or '未命名任务'

    has_pdf = bool(result.get('pdf_path'))
    has_output = bool(result.get('output_dir'))
    has_quality = bool(quality)
    has_visual = bool(visual)
    workflow_status = str(workflow.get('status') or '').strip()
    quality_status = str(quality.get('status') or '').strip()

    if status in {'queued', 'running'}:
        headline = '本地 Agent 正在处理'
        detail = '正在匹配经历、改写内容、编译 PDF，并准备进入视觉复检'
        next_action = '等待完成，或在需要时取消任务'
        action_kind = 'wait'
    elif status == 'completed':
        headline = 'Agent 已生成结果'
        detail = quality.get('summary') or workflow.get('next_action') or '结果已生成，可继续质量检查、编辑或查看 PDF'
        next_action = workflow.get('next_action') or quality.get('next_action') or ('查看 PDF' if has_pdf else '查看质量报告')
        action_kind = 'quality' if quality_status in {'fix', 'review'} or workflow_status in {'blocked', 'in_progress'} else 'open_result'
    elif status == 'cancelled':
        headline = '任务已取消'
        detail = '可以按上次输入重试，也可以先修改 JD、面经或生成方向'
        next_action = '重试或修改输入'
        action_kind = 'retry'
    elif status == 'failed':
        headline = 'Agent 任务失败'
        error_code = str(payload.get('error_code') or result.get('error_code') or '').strip()
        detail = (
            f'失败代码：{error_code}。建议查看最近日志，修复 CLI、模型 Key 或 LaTeX 环境后重试'
            if error_code else
            '建议查看最近日志，修复 CLI、模型 Key 或 LaTeX 环境后重试'
        )
        next_action = '查看日志后重试'
        action_kind = 'retry'
    else:
        headline = '等待 Agent 状态'
        detail = '正在读取本地任务状态'
        next_action = '刷新状态'
        action_kind = 'refresh'

    agent_phase_status = 'pending'
    if status == 'queued':
        agent_phase_status = 'active'
    elif status == 'running':
        agent_phase_status = 'active'
    elif status in {'completed'}:
        agent_phase_status = 'pass'
    elif status in {'failed', 'cancelled'}:
        agent_phase_status = 'fail' if status == 'failed' else 'warning'

    pdf_phase_status = 'pending'
    if has_pdf:
        pdf_phase_status = 'pass'
    elif status == 'completed':
        pdf_phase_status = 'fail'
    elif status == 'running':
        pdf_phase_status = 'active' if progress >= 55 else 'pending'
    elif status in {'failed', 'cancelled'}:
        pdf_phase_status = 'fail' if status == 'failed' else 'warning'

    quality_phase_status = 'pending'
    if quality_status == 'ready':
        quality_phase_status = 'pass'
    elif quality_status == 'fix':
        quality_phase_status = 'fail'
    elif quality_status:
        quality_phase_status = 'warning'
    elif has_visual:
        quality_phase_status = 'warning'
    elif status == 'running' and progress >= 75:
        quality_phase_status = 'active'

    delivery_phase_status = 'pending'
    if workflow_status == 'complete':
        delivery_phase_status = 'pass'
    elif workflow_status in {'ready', 'in_progress'}:
        delivery_phase_status = 'warning'
    elif workflow_status == 'blocked':
        delivery_phase_status = 'fail'

    phases = [
        _agent_user_phase('input', '输入已保存', 'pass', input_summary),
        _agent_user_phase('agent', 'Agent 执行', agent_phase_status, step or status),
        _agent_user_phase('pdf', 'PDF 生成', pdf_phase_status, '可查看 PDF' if has_pdf else '等待 PDF 输出'),
        _agent_user_phase('quality', '视觉与质量检查', quality_phase_status, quality.get('label') or visual.get('message') or ''),
        _agent_user_phase('delivery', '交付准备', delivery_phase_status, workflow.get('label') or ''),
    ]

    return {
        'headline': headline,
        'detail': detail,
        'next_action': str(next_action or ''),
        'action_kind': action_kind,
        'input_summary': input_summary,
        'agent_label': 'Claude Code' if agent == 'claude' else ('Codex' if agent == 'codex' else agent),
        'language': language,
        'progress': progress,
        'phases': phases,
        'artifacts': {
            'has_output_dir': has_output,
            'output_dir': str(result.get('output_dir') or ''),
            'has_pdf': has_pdf,
            'pdf_path': str(result.get('pdf_path') or ''),
            'has_quality_report': has_quality,
            'has_visual_review': has_visual,
            'version_count': int(result.get('version_count') or 0),
        },
    }


def _enrich_agent_job_payload(payload: dict) -> dict:
    """Add gallery-derived quality/workflow fields and a safe user-facing summary."""
    if not isinstance(payload, dict):
        return payload
    enriched = dict(payload)
    result = enriched.get('result') if isinstance(enriched.get('result'), dict) else None
    if not result:
        enriched['user_status'] = _build_agent_user_status(enriched)
        return enriched
    if result.get('quality_report') and result.get('workflow_status'):
        enriched['user_status'] = _build_agent_user_status(enriched)
        return enriched

    dir_name = str(result.get('output_dir') or '').strip()
    if not dir_name or '..' in dir_name or '/' in dir_name or dir_name.startswith('.'):
        enriched['user_status'] = _build_agent_user_status(enriched)
        return enriched
    out_dir = _output_dir() / dir_name
    if not out_dir.exists() or not out_dir.is_dir():
        enriched['user_status'] = _build_agent_user_status(enriched)
        return enriched

    context = _load_generation_context(out_dir)
    visual_review = result.get('visual_review') if isinstance(result.get('visual_review'), dict) else {}
    if not visual_review:
        visual_review = context.get('visual_review') if isinstance(context.get('visual_review'), dict) else {}
    if not visual_review:
        review_json = out_dir / 'visual_review' / 'visual_review.json'
        if review_json.exists():
            try:
                loaded_review = json.loads(review_json.read_text(encoding='utf-8'))
                if isinstance(loaded_review, dict):
                    visual_review = loaded_review
            except Exception:
                visual_review = {}
    visual_review = _with_visual_review_screenshot_path(out_dir, visual_review)

    language = result.get('language') or context.get('language')
    try:
        _, _, pdf_path = _resolve_resume_paths(out_dir, language)
    except ValueError:
        _, _, pdf_path = _resolve_resume_paths(out_dir)
    pdf_exists = pdf_path.exists()
    version_count = _get_version_count(out_dir)
    quality_report = result.get('quality_report') if isinstance(result.get('quality_report'), dict) else {}
    if not quality_report:
        quality_report = _build_resume_quality_report(
            pdf_exists=pdf_exists,
            visual_review=visual_review,
            version_count=version_count,
            context=context,
        )
    workflow_status = result.get('workflow_status') if isinstance(result.get('workflow_status'), dict) else {}
    if not workflow_status:
        workflow_status = _build_resume_workflow_status(
            pdf_exists=pdf_exists,
            visual_review=visual_review,
            quality_report=quality_report,
            version_count=version_count,
            context=context,
        )

    result = dict(result)
    if pdf_exists and not result.get('pdf_path'):
        try:
            result['pdf_path'] = str(pdf_path.relative_to(_output_dir()))
        except ValueError:
            result['pdf_path'] = str(pdf_path)
    result['visual_review'] = visual_review
    result['quality_report'] = quality_report
    result['workflow_status'] = workflow_status
    result['version_count'] = version_count
    enriched['result'] = result
    enriched['user_status'] = _build_agent_user_status(enriched)
    return enriched


def _resolve_output_language(out_dir: Path, explicit_language: str | None = None) -> str:
    if explicit_language is not None and str(explicit_language).strip():
        return normalize_language(explicit_language)
    return infer_language_from_output_dir(out_dir)


def _resolve_resume_paths(out_dir: Path, explicit_language: str | None = None) -> tuple[str, Path, Path]:
    language = _resolve_output_language(out_dir, explicit_language)
    tex_name, pdf_name = resolve_resume_filenames(language)
    return language, out_dir / tex_name, out_dir / pdf_name


def render_imported_resume_tex(structured: dict, language: str = 'zh') -> str:
    language = normalize_language(language)
    basic = structured.get('basic', {})
    name_parts = [basic.get('name_zh', '').strip(), basic.get('name_en', '').strip()]
    name = ' '.join([p for p in name_parts if p]).strip() or '候选人'
    email = basic.get('email', '').strip() or 'your.email@example.com'
    phone = basic.get('phone', '').strip() or '(+86) 000-0000-0000'

    use_zh = language == 'zh'
    lines = [
        '% !TEX TS-program = xelatex',
        '% !TEX encoding = UTF-8 Unicode',
        '',
        r'\documentclass{resume}',
    ]
    if use_zh:
        lines.append(r'\usepackage{zh_CN-Adobefonts_external}')
    lines.extend([
        r'\usepackage{linespacing_fix}',
        r'\usepackage{cite}',
        '',
        r'\begin{document}',
        r'\begin{Form}',
        '',
        r'\pagenumbering{gobble}',
        '',
        rf'\name{{{_tex_escape(name)}}}',
        '',
        r'\basicInfo{',
        rf'\email{{{_tex_escape(email)}}} \textperiodcentered \phone{{{_tex_escape(phone)}}}',
        r'}',
        r'\vspace{-8pt}',
        '',
        rf'\section{{{"教育背景" if use_zh else "Education"}}}',
    ])

    education = structured.get('education') or []
    for edu in education:
        school = _tex_escape(edu.get('school', '') or '学校')
        degree = _tex_escape(edu.get('degree', '') or '')
        major = _tex_escape(edu.get('major', '') or '')
        dept = _tex_escape(edu.get('department', '') or '')
        date_range = _tex_escape(_format_resume_range(edu.get('time_start', ''), edu.get('time_end', '')))
        lines.append(rf'\datedsubsection{{\textbf{{{school}}} \quad \normalsize {degree}}}{{{date_range}}}')
        lines.append(rf'\textit{{{major} \quad {dept}}}')
        lines.append('')

    lines.append(rf'\section{{{"实习经历" if use_zh else "Experience"}}}')
    experiences = structured.get('experiences') or []
    for exp in experiences:
        company = _tex_escape(exp.get('company', '') or '公司')
        city = _tex_escape(exp.get('city', '') or '')
        role = _tex_escape(exp.get('role', '') or '岗位')
        dept = _tex_escape(exp.get('department', '') or '')
        date_range = _tex_escape(_format_resume_range(exp.get('time_start', ''), exp.get('time_end', '')))
        lines.append(rf'\datedsubsection{{\textbf{{{company}}} \quad \normalsize {city}}}{{{date_range}}}')
        lines.append(rf'\role{{{role}}}{{{dept}}}')
        lines.append(r'\vspace{-6pt}')
        lines.append(r'\begin{itemize}')
        bullets = exp.get('bullets') or []
        if not bullets and exp.get('notes'):
            bullets = [exp.get('notes', '')]
        for bullet in bullets[:4]:
            b = _tex_escape(str(bullet).strip())
            if b:
                lines.append(rf'    \item {b}')
        lines.append(r'\end{itemize}')
        lines.append(r'\vspace{-2pt}')
        lines.append('')

    awards = structured.get('awards') or []
    if awards:
        lines.append(rf'\section{{{"获奖情况" if use_zh else "Honors and Awards"}}}')
        for award in awards[:3]:
            title = _tex_escape(award.get('name', ''))
            date = _tex_escape(_normalize_ym(award.get('date', '')))
            lines.append(rf'\datedline{{\textit{{{title}}}}}{{{date}}}')
        lines.append('')

    skills = structured.get('skills') or {}
    lines.append(rf'\section{{{"技能" if use_zh else "Skills"}}}')
    lines.append(r'\begin{itemize}[parsep=0.5ex]')
    if use_zh:
        lines.append(rf'    \item \textbf{{编程与技术：}} {_tex_escape(skills.get("tech", "") or "")}')
        lines.append(rf'    \item \textbf{{工具：}} {_tex_escape(skills.get("software", "") or "")} \quad \textbf{{语言：}} {_tex_escape(skills.get("languages", "") or "")}')
    else:
        lines.append(rf'    \item \textbf{{Programming \& Technical:}} {_tex_escape(skills.get("tech", "") or "")}')
        lines.append(rf'    \item \textbf{{Tools:}} {_tex_escape(skills.get("software", "") or "")} \quad \textbf{{Languages:}} {_tex_escape(skills.get("languages", "") or "")}')
    lines.append(r'\end{itemize}')
    lines.append('')
    lines.append(r'\end{Form}')
    lines.append(r'\end{document}')
    return '\n'.join(lines)


def _to_profile_payload_from_import(structured: dict) -> dict:
    basic = structured.get('basic') or {}
    education = structured.get('education') or []
    awards = structured.get('awards') or []
    skills = structured.get('skills') or {}
    return {
        'basic': {
            'name_zh': basic.get('name_zh', ''),
            'name_en': basic.get('name_en', ''),
            'email': basic.get('email', ''),
            'phone': basic.get('phone', ''),
            'linkedin': '',
            'github': '',
            'website': '',
        },
        'education': [{
            'school': e.get('school', ''),
            'degree': e.get('degree', ''),
            'major': e.get('major', ''),
            'department': e.get('department', ''),
            'time_start': _normalize_ym(e.get('time_start', '')),
            'time_end': _normalize_ym(e.get('time_end', '')),
            'gpa': e.get('gpa', ''),
            'rank': e.get('rank', ''),
            'courses': e.get('courses', ''),
        } for e in education],
        'awards': [{
            'name': a.get('name', ''),
            'issuer': a.get('issuer', ''),
            'date': _normalize_ym(a.get('date', '')),
        } for a in awards],
        'projects': [],
        'publications': [],
        'skills': {
            'tech': skills.get('tech', ''),
            'software': skills.get('software', ''),
            'languages': skills.get('languages', ''),
        },
        'directions': {'primary': '', 'secondary': ''},
    }


def _persist_imported_data(structured: dict) -> list[str]:
    payload = _to_profile_payload_from_import(structured)
    _profile_path().parent.mkdir(parents=True, exist_ok=True)
    _profile_path().write_text(render_profile(payload), encoding='utf-8')

    written_files = []
    for exp in (structured.get('experiences') or []):
        bullets = exp.get('bullets') or []
        work_items = []
        for idx, bullet in enumerate(bullets, 1):
            title = str(bullet).strip()[:22] or f'工作内容 {idx}'
            work_items.append({'title': title, 'desc': str(bullet).strip()})
        filename = save_experience_form({
            'company': exp.get('company', '导入经历'),
            'city': exp.get('city', ''),
            'department': exp.get('department', ''),
            'role': exp.get('role', '岗位'),
            'time_start': _normalize_ym(exp.get('time_start', '')),
            'time_end': _normalize_ym(exp.get('time_end', '')),
            'tags': exp.get('tags', ''),
            'work_items': work_items or [{'title': '工作内容 1', 'desc': exp.get('notes', '')}],
            'notes': exp.get('notes', ''),
        })
        written_files.append(filename)
    return written_files


def _compile_resume_dir(out_dir: Path, language: str = 'zh') -> dict:
    language = normalize_language(language)
    tex_filename, pdf_filename = resolve_resume_filenames(language)
    xelatex_bin = _find_xelatex()
    env = os.environ.copy()
    xelatex_dir = str(Path(xelatex_bin).parent) if xelatex_bin != 'xelatex' else ''
    if xelatex_dir:
        env['PATH'] = xelatex_dir + ':' + env.get('PATH', '')

    result = _sp.run(
        [xelatex_bin, '-interaction=nonstopmode', '--synctex=1', tex_filename],
        cwd=str(out_dir),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    pdf_path = out_dir / pdf_filename
    log_path = out_dir / f'{Path(tex_filename).stem}.log'
    log_tail = ''
    if log_path.exists():
        lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
        log_tail = '\n'.join(lines[-30:])

    if result.returncode != 0 or not pdf_path.exists():
        return {
            'success': False,
            'pages': 0,
            'fill_rate': 0,
            'error': f'编译失败 (exit code {result.returncode})',
            'log_tail': log_tail,
        }

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

    fill_rate = 0.0
    try:
        fill_check = str(PROJECT_ROOT / 'tools' / 'page_fill_check.py')
        fr_result = _sp.run(
            ['python3', fill_check, str(out_dir), xelatex_bin, tex_filename],
            capture_output=True, text=True, timeout=60, env=env,
        )
        m = re.search(r'填充率[：:]\s*([\d.]+)%', fr_result.stdout)
        if m:
            fill_rate = float(m.group(1))
    except Exception:
        pass

    return {'success': True, 'pages': pages, 'fill_rate': fill_rate, 'error': '', 'log_tail': log_tail}


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
        'campus_experiences': [],
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
            if section == '校园经历' and '校园' in subsection:
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

    elif section == '校园经历':
        kv = _parse_kv_block(code_lines)
        if '组织/活动名称' in kv or '组织' in kv or '活动名称' in kv:
            time_str = kv.get('时间', '')
            item = {
                'name': kv.get('组织/活动名称', kv.get('活动名称', kv.get('组织', ''))),
                'role': kv.get('角色', ''),
                'time_start': '',
                'time_end': '',
                'highlights': kv.get('成果要点', kv.get('成果', '')),
                'tags': kv.get('标签', ''),
            }
            if '--' in time_str:
                parts = time_str.split('--')
                item['time_start'] = parts[0].strip()
                item['time_end'] = parts[1].strip()
            data['campus_experiences'].append(item)

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

    # 校园经历
    campus_items = data.get('campus_experiences', [])
    if campus_items:
        campus_sections = ['## 校园经历\n\n<!-- 学生会、社团、志愿活动、挑战赛组织等，按时间倒序填写 -->']
        for i, item in enumerate(campus_items):
            time_str = f"{item.get('time_start', '')} -- {item.get('time_end', '')}"
            highlights = item.get('highlights', '')
            if isinstance(highlights, list):
                highlights = '；'.join(str(x).strip() for x in highlights if str(x).strip())
            campus_sections.append(f"""### 校园经历 {i + 1}

```
组织/活动名称：{item.get('name', '')}
角色：{item.get('role', '')}
时间：{time_str}
成果要点：{highlights}
标签：{item.get('tags', '')}
```""")
        sections.append('\n\n'.join(campus_sections) + '\n\n---')
    else:
        sections.append("""## 校园经历

<!-- 学生会、社团、志愿活动、挑战赛组织等，按时间倒序填写 -->
<!-- 格式：组织/活动名称、角色、时间、成果要点、标签 -->

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


LOCAL_SKILL_CATALOG = {
    'tech': [
        ('Python', [r'\bPython\b']),
        ('SQL', [r'\bSQL\b']),
        ('Pandas', [r'\bPandas\b']),
        ('NumPy', [r'\bNumPy\b']),
        ('Selenium', [r'\bSelenium\b']),
        ('Stata', [r'\bStata\b']),
        ('R', [r'\bR\b']),
        ('C', [r'(?<![A-Za-z])C(?![A-Za-z+#])']),
        ('C++', [r'\bC\+\+\b', r'C\+\+']),
        ('JavaScript', [r'\bJavaScript\b']),
        ('TypeScript', [r'\bTypeScript\b']),
        ('HTML', [r'\bHTML\b']),
        ('CSS', [r'\bCSS\b']),
        ('React', [r'\bReact\b']),
        ('Electron', [r'\bElectron\b']),
        ('LaTeX', [r'\bLaTeX\b']),
        ('XeLaTeX', [r'\bXeLaTeX\b']),
        ('Node.js', [r'\bNode\.js\b', r'\bNodeJS\b']),
        ('API 对接', [r'\bAPI\b', r'接口对接']),
        ('Prompt Engineering', [r'\bPrompt\b', r'提示词']),
        ('Function Call', [r'\bFunction\s+Call\b']),
        ('AI Agent', [r'\bAgent\b', r'智能体']),
        ('机器学习', [r'机器学习', r'\bMachine\s+Learning\b']),
        ('深度学习', [r'深度学习', r'\bDeep\s+Learning\b']),
        ('NLP', [r'\bNLP\b', r'自然语言处理']),
        ('LLM', [r'\bLLM\b', r'大模型']),
        ('数据分析', [r'数据分析', r'\bData\s+Analysis\b']),
        ('量化研究', [r'量化研究', r'量化']),
    ],
    'software': [
        ('Cursor', [r'\bCursor\b']),
        ('Codex', [r'\bCodex\b']),
        ('Claude Code', [r'\bClaude\s+Code\b']),
        ('Coze', [r'\bCoze\b']),
        ('n8n', [r'\bn8n\b']),
        ('Microsoft Office', [r'\bMicrosoft\s+Office\b']),
        ('Excel', [r'\bExcel\b']),
        ('PowerPoint', [r'\bPowerPoint\b', r'\bPPT\b']),
        ('Word', [r'\bWord\b']),
        ('Visio', [r'\bVisio\b']),
        ('Figma', [r'\bFigma\b']),
        ('Notion', [r'\bNotion\b']),
        ('Tableau', [r'\bTableau\b']),
        ('Power BI', [r'\bPower\s+BI\b']),
        ('Git', [r'\bGit\b']),
        ('GitHub', [r'\bGitHub\b']),
        ('Docker', [r'\bDocker\b']),
        ('Bloomberg', [r'\bBloomberg\b']),
        ('Wind', [r'\bWind\b']),
        ('CSMAR', [r'\bCSMAR\b']),
        ('Choice', [r'\bChoice\b']),
    ],
}


def _existing_skill_values(profile: dict) -> set[str]:
    skills = profile.get('skills') if isinstance(profile.get('skills'), dict) else {}
    values = set()
    for value in skills.values():
        for item in re.split(r'[,，;；\n]', str(value or '')):
            item = item.strip()
            if item:
                values.add(item.lower())
    return values


def _safe_read_text(path: Path, limit: int = 200_000) -> str:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return ''
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    return text[:limit]


def _iter_skill_suggestion_sources() -> list[Path]:
    sources: list[Path] = []
    try:
        profile_path = _profile_path()
        if profile_path.exists():
            sources.append(profile_path)
    except Exception:
        pass
    try:
        sources.extend(sorted(p for p in _experiences_dir().glob('*.md') if p.is_file()))
    except Exception:
        pass
    try:
        for folder in sorted(p for p in _work_materials_dir().iterdir() if p.is_dir()):
            for path in sorted(folder.iterdir()):
                if path.is_file() and path.suffix.lower() in {'.md', '.txt', '.json', '.csv'}:
                    sources.append(path)
    except Exception:
        pass
    try:
        out_dirs = sorted((p for p in _output_dir().iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        for folder in out_dirs[:8]:
            for name in ('resume-zh_CN.tex', 'resume-en.tex', 'generation_context.json'):
                path = folder / name
                if path.is_file():
                    sources.append(path)
    except Exception:
        pass
    return sources


def build_local_skill_suggestions() -> dict:
    """Suggest profile skills by matching known skill tokens in local user-owned content."""
    profile = parse_profile()
    existing = existing_skill_values_from_profile(profile)
    sources = _iter_skill_suggestion_sources()
    corpus = '\n'.join(_safe_read_text(path) for path in sources)
    suggestions = derive_skills_from_text(corpus, existing=existing)
    derived_count = count_skill_values(suggestions)

    return {
        'success': True,
        'suggestions': suggestions,
        'derived_count': derived_count,
        'source_count': len(sources),
        'summary': f'从 {len(sources)} 个本地资料文件中提取 {derived_count} 个技能候选；请只应用你确认真实掌握的项目',
    }


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

    使用 pymupdf 优先提取首页文本（CJK 可靠），
    pypdf 仅用于读取 PDF 对象字典中的元数据。
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

    # --- 用 pypdf 读取元数据字典（不涉及字体，不乱码） ---
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(content))
        meta = reader.metadata
        if meta:
            if meta.title:
                result['title'] = meta.title
            if meta.author:
                result['authors'] = meta.author
    except Exception:
        pass

    # --- 用 pymupdf 提取首页文本（CJK 可靠） ---
    first_page_text = ''
    try:
        try:
            import pymupdf as fitz   # >= 1.24.0
        except ImportError:
            import fitz              # < 1.24.0
        doc = fitz.open(stream=content, filetype='pdf')
        if len(doc) > 0:
            first_page_text = doc[0].get_text() or ''
        doc.close()
    except Exception:
        # pymupdf 不可用时，回退 pypdf 提取文本
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            if len(reader.pages) > 0:
                first_page_text = reader.pages[0].extract_text() or ''
        except Exception:
            pass

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

def _create_version_snapshot(out_dir: Path, fill_rate: float = 0, pages: int = 1, language: str | None = None) -> int:
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

    _, tex_src, _ = _resolve_resume_paths(out_dir, language)
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


def _set_version_snapshot_note(out_dir: Path, version_num: int, note: str) -> None:
    clean_note = str(note or '').strip()
    if not clean_note:
        return
    versions_json = out_dir / 'versions' / 'versions.json'
    if not versions_json.exists():
        return
    try:
        versions = json.loads(versions_json.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(versions, list):
        return
    changed = False
    for item in versions:
        if isinstance(item, dict) and item.get('version') == version_num:
            item['note'] = clean_note
            changed = True
            break
    if changed:
        versions_json.write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding='utf-8')


def _create_manual_version_snapshot(
    out_dir: Path,
    *,
    note: str = '',
    language: str | None = None,
    context: dict | None = None,
    visual_review: dict | None = None,
) -> dict:
    """Create a user-triggered gallery version snapshot and return refreshed UI payload."""
    context = context if isinstance(context, dict) else _load_generation_context(out_dir)
    resolved_language = _resolve_output_language(out_dir, language or context.get('language'))
    _, tex_path, pdf_path = _resolve_resume_paths(out_dir, resolved_language)
    if not tex_path.exists():
        raise FileNotFoundError(f'找不到 {tex_path.name}，无法保存版本')

    review = (
        _with_visual_review_screenshot_path(out_dir, visual_review)
        if isinstance(visual_review, dict) and visual_review
        else _load_visual_review_for_output(out_dir, context)
    )
    fill_ratio = _normalize_fill_ratio_value(
        context.get('fill_ratio')
        or review.get('fill_ratio')
        or 0.0
    )
    try:
        page_count = int(review.get('page_count') or context.get('page_count') or 1)
    except (TypeError, ValueError):
        page_count = 1

    version_num = _create_version_snapshot(
        out_dir,
        fill_rate=round(fill_ratio * 100, 1) if fill_ratio else 0,
        pages=page_count,
        language=resolved_language,
    )
    _set_version_snapshot_note(out_dir, version_num, note or '手动保存版本')

    version_count = _get_version_count(out_dir)
    quality_report = _build_resume_quality_report(
        pdf_exists=pdf_path.exists(),
        visual_review=review,
        version_count=version_count,
        context=context,
    )
    workflow_status = _build_resume_workflow_status(
        pdf_exists=pdf_path.exists(),
        visual_review=review,
        quality_report=quality_report,
        version_count=version_count,
        context=context,
    )
    return {
        'success': True,
        'output_dir': out_dir.name,
        'version': version_num,
        'version_count': version_count,
        'language': resolved_language,
        'pdf_path': str(pdf_path.relative_to(_output_dir())) if pdf_path.exists() else '',
        'visual_review': review,
        'quality_report': quality_report,
        'workflow_status': workflow_status,
    }


def _safe_zip_name(value: str, fallback: str = 'resume_delivery') -> str:
    cleaned = re.sub(r'[^\w\u4e00-\u9fff.-]+', '_', str(value or '').strip(), flags=re.UNICODE)
    cleaned = cleaned.strip('._')
    return cleaned or fallback


def _parse_resume_dir_metadata(dir_name: str) -> dict:
    parts = str(dir_name or '').split('_')
    company = parts[0] if len(parts) >= 1 else str(dir_name or '')
    role = parts[1] if len(parts) >= 2 else ''
    date = parts[2] if len(parts) >= 3 else ''
    if date and len(date) == 8 and date.isdigit():
        date = f'{date[:4]}/{date[4:6]}/{date[6:]}'
    return {
        'company': company,
        'role': role,
        'date': date,
    }


def _build_delivery_package(out_dir: Path) -> tuple[bytes, str, dict]:
    """Build a self-contained delivery archive for one generated resume."""
    context = _load_generation_context(out_dir)
    fallback_meta = _parse_resume_dir_metadata(out_dir.name)
    language = _resolve_output_language(out_dir, context.get('language'))
    _, _, pdf_path = _resolve_resume_paths(out_dir, language)
    review = _load_visual_review_for_output(out_dir, context)
    version_count = _get_version_count(out_dir)
    quality_report = _build_resume_quality_report(
        pdf_exists=pdf_path.exists(),
        visual_review=review,
        version_count=version_count,
        context=context,
    )
    workflow_status = _build_resume_workflow_status(
        pdf_exists=pdf_path.exists(),
        visual_review=review,
        quality_report=quality_report,
        version_count=version_count,
        context=context,
    )

    safe_context = {
        'company': context.get('company') or fallback_meta.get('company') or '',
        'role': context.get('role') or fallback_meta.get('role') or '',
        'language': language,
        'engine': context.get('engine') or '',
        'generated_at': context.get('generated_at') or fallback_meta.get('date') or '',
        'fill_ratio': _normalize_fill_ratio_value(context.get('fill_ratio') or review.get('fill_ratio') or 0.0),
        'output_dir': out_dir.name,
    }
    manifest = {
        'package_type': 'resume_delivery',
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'resume': safe_context,
        'quality': {
            'status': quality_report.get('status') or '',
            'label': quality_report.get('label') or '',
            'summary': quality_report.get('summary') or '',
            'next_action': quality_report.get('next_action') or '',
        },
        'workflow': {
            'status': workflow_status.get('status') or '',
            'label': workflow_status.get('label') or '',
            'progress': workflow_status.get('progress') or 0,
        },
        'files': [],
        'privacy': {
            'excluded': ['generation_context.json', 'full_jd_text', 'full_interview_text', 'api_keys'],
            'note': '交付包不直接包含 generation_context.json，避免把完整 JD、面经或敏感配置作为元数据导出',
        },
    }

    candidate_files: list[Path] = []
    for name in (
        'resume-zh_CN.pdf',
        'resume-en.pdf',
        'resume-zh_CN.tex',
        'resume-en.tex',
        'resume.cls',
        'zh_CN-Adobefonts_external.sty',
        'linespacing_fix.sty',
        'generation_log.md',
    ):
        path = out_dir / name
        if path.exists() and path.is_file():
            candidate_files.append(path)

    review_dir = out_dir / 'visual_review'
    if review_dir.exists():
        for path in sorted(review_dir.glob('*')):
            if path.is_file() and path.suffix.lower() in {'.json', '.png', '.jpg', '.jpeg', '.webp'}:
                candidate_files.append(path)

    versions_dir = out_dir / 'versions'
    if versions_dir.exists():
        for path in sorted(versions_dir.glob('*')):
            if path.is_file() and path.suffix.lower() in {'.json', '.tex'}:
                candidate_files.append(path)

    buffer = io.BytesIO()
    root_name = _safe_zip_name(out_dir.name)
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        manifest['files'].extend([
            f'{root_name}/delivery_manifest.json',
            f'{root_name}/quality_report.json',
            f'{root_name}/workflow_status.json',
        ])
        generated_files = {
            'quality_report.json': quality_report,
            'workflow_status.json': workflow_status,
        }
        for filename, payload in generated_files.items():
            zf.writestr(
                f'{root_name}/{filename}',
                json.dumps(payload, ensure_ascii=False, indent=2),
            )

        for path in candidate_files:
            try:
                rel = path.resolve().relative_to(out_dir.resolve())
            except ValueError:
                continue
            arcname = f'{root_name}/{rel.as_posix()}'
            zf.write(path, arcname)
            manifest['files'].append(arcname)

        zf.writestr(
            f'{root_name}/delivery_manifest.json',
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

    company = safe_context.get('company') or out_dir.name
    role = safe_context.get('role') or 'resume'
    filename = f'{_safe_zip_name(company)}_{_safe_zip_name(role)}_delivery.zip'
    return buffer.getvalue(), filename, manifest


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


def _binary_available(binary: str) -> tuple[bool, str]:
    if not binary:
        return False, ''
    candidate = Path(binary).expanduser()
    if candidate.is_absolute() and candidate.exists():
        return True, str(candidate)
    resolved = shutil.which(binary)
    return bool(resolved), resolved or binary


def _profile_readiness(profile: dict) -> tuple[str, str, str, bool]:
    basic = profile.get('basic') if isinstance(profile.get('basic'), dict) else {}
    skills = profile.get('skills') if isinstance(profile.get('skills'), dict) else {}
    has_name = bool(str(basic.get('name_zh') or basic.get('name_en') or '').strip())
    has_basic = has_name and all(str(basic.get(k) or '').strip() for k in ('email', 'phone'))
    has_education = bool(profile.get('education'))
    has_skills = any(str(skills.get(k) or '').strip() for k in ('tech', 'software', 'languages'))
    missing = []
    if not has_basic:
        missing.append('基本信息')
    if not has_education:
        missing.append('教育背景')
    if missing:
        if not has_basic:
            target_page = 'basic'
        else:
            target_page = 'education'
        return 'fail', '缺少' + '、'.join(missing), target_page, has_skills
    return 'pass', '姓名、联系方式和教育背景已填写', '', has_skills


def _check_output_writable() -> tuple[bool, str]:
    try:
        out_dir = _output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / '.system_check_write'
        probe.write_text(datetime.now().isoformat(), encoding='utf-8')
        probe.unlink(missing_ok=True)
        return True, str(out_dir)
    except Exception as exc:
        return False, str(exc)


def _agent_health_report(agent_health: list[dict]) -> list[dict]:
    """Normalize local Agent health for the Web UI without exposing noisy internals."""
    labels = {
        'codex': 'Codex',
        'claude': 'Claude Code',
        'claude_code': 'Claude Code',
        'claude-code': 'Claude Code',
    }
    reports: list[dict] = []
    for item in agent_health:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip() or 'agent'
        normalized = name.lower()
        error_code = str(item.get('error_code') or '').strip()
        steps: list[str] = []
        if normalized == 'codex' and error_code == 'CODEX_CONFIG_INVALID_SERVICE_TIER':
            config_path = str(item.get('config_path') or '~/.codex/config.toml')
            steps = [
                f'打开 {config_path}',
                '将 service_tier 改为 "fast" 或 "flex"',
                '保存后重新自检；也可以临时切换到 Claude Code',
            ]
        elif error_code == 'AGENT_NOT_AVAILABLE':
            executable = str(item.get('executable') or name)
            steps = [
                f'确认 {executable} CLI 已安装并在 PATH 中',
                '在终端完成登录或授权',
                '重新启动本地服务后点击重新自检',
            ]
        elif not item.get('ok'):
            steps = [
                '按详情修复本地 CLI 或配置',
                '修复后点击重新自检',
                '若存在其他可用 Agent，可先切换继续生成',
            ]

        reports.append({
            'name': name,
            'label': labels.get(normalized, name),
            'ok': bool(item.get('ok')),
            'status': item.get('status') or ('pass' if item.get('ok') else 'fail'),
            'detail': str(item.get('detail') or ''),
            'action': str(item.get('action') or ''),
            'error_code': error_code,
            'executable': str(item.get('executable') or ''),
            'path': str(item.get('path') or ''),
            'config_path': str(item.get('config_path') or ''),
            'recovery_steps': steps,
        })
    return reports


def build_system_check() -> dict:
    """Build a user-facing readiness report for ordinary local usage."""
    checks = []
    agent_health: list[dict] = []

    def add_check(
        key: str,
        label: str,
        status: str,
        detail: str,
        action: str = '',
        required: bool = False,
        target_page: str = '',
    ) -> None:
        checks.append({
            'key': key,
            'label': label,
            'status': status,
            'detail': detail,
            'action': action,
            'required': required,
            'target_page': target_page,
        })

    try:
        profile = parse_profile()
    except Exception as exc:
        profile = {}
        add_check('profile', '个人资料', 'fail', f'读取 profile.md 失败：{exc}', '打开资料页修复', True, 'basic')
    if profile:
        profile_status, profile_detail, profile_target_page, profile_has_skills = _profile_readiness(profile)
        profile_action = ''
        if profile_status != 'pass':
            profile_action = '完善资料'
        add_check(
            'profile',
            '个人资料',
            profile_status,
            profile_detail,
            profile_action,
            True,
            profile_target_page,
        )
        if profile_status == 'pass' and not profile_has_skills:
            derived_skills = build_local_skill_suggestions()
            derived_count = int(derived_skills.get('derived_count') or 0)
            if derived_count > 0:
                add_check(
                    'profile_skills',
                    '技能上下文',
                    'pass',
                    f'已从本地资料识别 {derived_count} 项技能，生成时会自动使用；可选择保存到档案',
                    '查看/保存',
                    False,
                    'skills',
                )
                checks[-1]['derived'] = True
                checks[-1]['derived_count'] = derived_count
            else:
                add_check(
                    'profile_skills',
                    '技能标签',
                    'warning',
                    '技能栏为空，且暂未从本地资料识别出可用技能；建议补充或导入经历材料',
                    '从资料提取',
                    False,
                    'skills',
                )
    elif not any(c['key'] == 'profile' for c in checks):
        add_check('profile', '个人资料', 'fail', '尚未创建或填写 profile.md', '完善资料', True, 'basic')

    try:
        exp_count = len([p for p in _experiences_dir().glob('*.md') if p.is_file()])
    except Exception:
        exp_count = 0
    project_count = len(profile.get('projects') or []) if isinstance(profile, dict) else 0
    campus_count = len(profile.get('campus_experiences') or []) if isinstance(profile, dict) else 0
    total_content = exp_count + project_count + campus_count
    add_check(
        'content_library',
        '经历素材',
        'pass' if total_content > 0 else 'warning',
        f'已有 {exp_count} 段实习/工作、{project_count} 个项目、{campus_count} 段校园经历'
        if total_content > 0 else '经历库为空，生成质量会明显受限',
        '' if total_content > 0 else '补充经历',
        False,
        'experiences' if total_content <= 0 else '',
    )

    xelatex_bin = _find_xelatex()
    has_xelatex, xelatex_path = _binary_available(xelatex_bin)
    add_check(
        'xelatex',
        'LaTeX 编译',
        'pass' if has_xelatex else 'fail',
        f'已找到 {xelatex_path}' if has_xelatex else '未找到 xelatex，无法稳定生成 PDF',
        '' if has_xelatex else '安装 TinyTeX / XeLaTeX',
        True,
    )

    try:
        from tools.generate_resume import _find_pdftoppm
        pdftoppm_path = _find_pdftoppm()
    except Exception:
        pdftoppm_path = shutil.which('pdftoppm')
    add_check(
        'pdftoppm',
        '视觉截图',
        'pass' if pdftoppm_path else 'warning',
        f'已找到 {pdftoppm_path}' if pdftoppm_path else '未找到 pdftoppm；视觉 QA 可运行但会跳过截图',
        '' if pdftoppm_path else '安装 poppler / pdftoppm',
        False,
    )

    out_writable, out_detail = _check_output_writable()
    add_check(
        'output_writable',
        '输出目录',
        'pass' if out_writable else 'fail',
        f'可写入 {out_detail}' if out_writable else f'输出目录不可写：{out_detail}',
        '' if out_writable else '检查目录权限',
        True,
    )

    try:
        from tools.agent_adapters.codex import CodexAdapter
        from tools.agent_adapters.claude_code import ClaudeCodeAdapter
        agent_health = [
            CodexAdapter(PROJECT_ROOT).health_check(),
            ClaudeCodeAdapter(PROJECT_ROOT).health_check(),
        ]
    except Exception as exc:
        agent_health = [{
            'ok': False,
            'status': 'fail',
            'name': 'agent',
            'detail': f'本地 Agent 检查失败：{exc}',
            'action': '检查本地 Agent CLI',
        }]
    agent_names = []
    unavailable_agents = []
    unhealthy_agents = []
    for item in agent_health:
        if item.get('ok'):
            agent_names.append('Codex' if item.get('name') == 'codex' else 'Claude Code')
        elif item.get('error_code') == 'AGENT_NOT_AVAILABLE':
            if item.get('detail'):
                unavailable_agents.append(str(item.get('detail')))
        elif item.get('detail'):
            unhealthy_agents.append(str(item.get('detail')))
    if agent_names:
        agent_status = 'pass'
        agent_detail = '可用：' + ' / '.join(agent_names)
        if unhealthy_agents:
            agent_detail += '；以下 Agent 可稍后修复，不影响自动选择：' + '；'.join(unhealthy_agents[:2])
        agent_action = ''
    else:
        agent_status = 'fail'
        all_issues = unhealthy_agents + unavailable_agents
        agent_detail = '；'.join(all_issues[:2]) if all_issues else '未找到 Codex 或 Claude Code CLI，后台 Agent 无法启动'
        agent_action = '安装或登录本地 Agent CLI'
    add_check(
        'local_agent',
        '本地 Agent',
        agent_status,
        agent_detail,
        agent_action,
        True,
    )

    try:
        gallery_count = len(list_gallery_resumes())
    except Exception:
        gallery_count = 0
    add_check(
        'gallery',
        '简历画廊',
        'pass' if gallery_count > 0 else 'warning',
        f'已有 {gallery_count} 份生成结果' if gallery_count > 0 else '尚无生成结果，可先用一段 JD 试跑',
        '' if gallery_count > 0 else '生成第一份简历',
        False,
        'generate' if gallery_count <= 0 else '',
    )

    required_failures = [c for c in checks if c['required'] and c['status'] == 'fail']
    warnings = [c for c in checks if c['status'] == 'warning']
    if required_failures:
        status = 'blocked'
        label = '需先修复'
        summary = '存在会阻断生成或后台 Agent 的本地问题'
        next_action = required_failures[0]['action'] or '修复阻断项'
    elif warnings:
        status = 'attention'
        label = '可用，建议完善'
        summary = '核心链路可运行，但仍有影响体验或质量的项目'
        next_action = warnings[0]['action'] or '继续完善'
    else:
        status = 'ready'
        label = '系统就绪'
        summary = '本地生成、编译、视觉 QA 和 Agent 后台能力已就绪'
        next_action = '粘贴 JD 开始生成'

    score_map = {'pass': 1.0, 'warning': 0.5, 'fail': 0.0}
    readiness = round(sum(score_map.get(c['status'], 0.0) for c in checks) / max(1, len(checks)) * 100)
    return {
        'status': status,
        'label': label,
        'summary': summary,
        'next_action': next_action,
        'readiness': readiness,
        'checked_at': datetime.now().isoformat(timespec='seconds'),
        'checks': checks,
        'agent_health': _agent_health_report(agent_health),
    }


def build_runtime_info(host: str | None = None, port: int | None = None) -> dict:
    """Build local delivery metadata for the Web UI landing guide."""
    normalized_host = (host or '').strip()
    if not normalized_host:
        normalized_host = f'127.0.0.1:{port or 8765}'
    if '://' in normalized_host:
        app_url = normalized_host.rstrip('/')
    else:
        app_url = f'http://{normalized_host.rstrip("/")}'

    launch_port = port
    if launch_port is None:
        try:
            parsed = urllib.parse.urlparse(app_url)
            launch_port = parsed.port or 8765
        except ValueError:
            launch_port = 8765

    launch_command = f'python3 web/server.py --port {launch_port or 8765}'
    return {
        'success': True,
        'app_url': app_url,
        'project_root': str(PROJECT_ROOT),
        'data_dir': str(DATA_DIR),
        'output_dir': str(_output_dir()),
        'launch_command': launch_command,
        'health_url': f'{app_url}/api/system-check',
        'agent_jobs_url': f'{app_url}/api/agent/jobs',
        'docs': {
            'readme': str(PROJECT_ROOT / 'README.md'),
            'setup': str(PROJECT_ROOT / 'SETUP.md'),
        },
        'steps': [
            {
                'key': 'start',
                'title': '启动本地工作台',
                'detail': '用固定命令启动服务，浏览器访问当前地址即可继续使用',
                'action': '复制启动命令',
            },
            {
                'key': 'check',
                'title': '完成系统自检',
                'detail': '确认资料、XeLaTeX、视觉截图和本地 Agent 都处于可用状态',
                'action': '重新自检',
            },
            {
                'key': 'generate',
                'title': '粘贴 JD 生成简历',
                'detail': '选择 Codex 或 Claude Code，让后台 Agent 完成匹配、改写和编译',
                'action': '开始生成',
            },
            {
                'key': 'loop',
                'title': '视觉复检与版本留存',
                'detail': '用质量报告修正间距、溢出和填充率，再保存可投递版本',
                'action': '查看画廊',
            },
        ],
    }


def _status_rank(status: str) -> int:
    return {
        'complete': 5,
        'ready': 4,
        'in_progress': 3,
        'review': 2,
        'attention': 2,
        'blocked': 1,
        'fix': 1,
    }.get(str(status or '').strip(), 0)


def _latest_resume_for_workbench(resumes: list[dict]) -> dict | None:
    if not resumes:
        return None
    return resumes[0]


def _resume_workbench_action(resume: dict) -> dict:
    workflow = resume.get('workflow_status') if isinstance(resume.get('workflow_status'), dict) else {}
    quality = resume.get('quality_report') if isinstance(resume.get('quality_report'), dict) else {}
    next_action = str(workflow.get('next_action') or quality.get('next_action') or '').strip()
    workflow_status = str(workflow.get('status') or '').strip()
    quality_status = str(quality.get('status') or '').strip()
    dir_name = str(resume.get('dir_name') or '').strip()

    if next_action and ('视觉' in next_action or '复检' in next_action):
        action_type = 'visual_review'
        label = '运行视觉 QA'
    elif next_action and '保存版本' in next_action:
        action_type = 'save_version'
        label = '保存版本'
    elif next_action and ('重生成' in next_action or '修复' in next_action):
        action_type = 'regen'
        label = '按 QA 重生成'
    elif workflow_status == 'complete' or quality_status == 'ready':
        action_type = 'quality_report'
        label = '打开质量报告'
    else:
        action_type = 'quality_report'
        label = next_action or '查看质量报告'

    return {
        'type': action_type,
        'label': label,
        'target_dir': dir_name,
    }


def _safe_resume_summary(resume: dict | None) -> dict | None:
    if not isinstance(resume, dict):
        return None
    quality = resume.get('quality_report') if isinstance(resume.get('quality_report'), dict) else {}
    workflow = resume.get('workflow_status') if isinstance(resume.get('workflow_status'), dict) else {}
    visual = resume.get('visual_review') if isinstance(resume.get('visual_review'), dict) else {}
    return {
        'company': resume.get('company') or '',
        'role': resume.get('role') or '',
        'dir_name': resume.get('dir_name') or '',
        'date': resume.get('date') or '',
        'pdf_path': resume.get('pdf_path') or '',
        'language': resume.get('language') or '',
        'quality_status': quality.get('status') or '',
        'quality_label': quality.get('label') or '',
        'quality_summary': quality.get('summary') or '',
        'workflow_status': workflow.get('status') or '',
        'workflow_label': workflow.get('label') or '',
        'workflow_progress': workflow.get('progress') or 0,
        'next_action': workflow.get('next_action') or quality.get('next_action') or '',
        'fill_label': quality.get('fill_label') or '',
        'page_count': quality.get('page_count') or visual.get('page_count') or 0,
        'version_count': resume.get('version_count') or quality.get('version_count') or 0,
        'action': _resume_workbench_action(resume),
    }


def build_workbench_status() -> dict:
    """Build a single next-step status for ordinary workbench usage."""
    system_check = build_system_check()
    system_checks = [
        check for check in system_check.get('checks', [])
        if isinstance(check, dict)
    ]
    safe_system_checks = [
        {
            'key': str(check.get('key') or ''),
            'label': str(check.get('label') or ''),
            'status': str(check.get('status') or ''),
            'detail': str(check.get('detail') or ''),
            'action': str(check.get('action') or ''),
            'required': bool(check.get('required')),
            'target_page': str(check.get('target_page') or ''),
        }
        for check in system_checks
    ]
    system_blockers = [
        check for check in safe_system_checks
        if check.get('required') and check.get('status') == 'fail'
    ]
    system_warnings = [
        check for check in safe_system_checks
        if check.get('status') == 'warning'
    ]
    resumes = list_gallery_resumes()
    latest_resume = _latest_resume_for_workbench(resumes)

    jobs: list[dict] = []
    try:
        from tools.agent_orchestrator import list_agent_jobs
        _, job_payload = list_agent_jobs(limit=6)
        jobs = job_payload.get('jobs') if isinstance(job_payload.get('jobs'), list) else []
    except Exception:
        jobs = []

    running_job = next((job for job in jobs if job.get('recoverable')), None)
    unfinished_job = running_job or next((job for job in jobs if job.get('status') in {'failed', 'cancelled'}), None)

    if system_check.get('status') == 'blocked':
        primary = next(
            (
                check for check in system_blockers
            ),
            {},
        )
        status = 'blocked'
        label = '先修复本地环境'
        summary = system_check.get('summary') or '存在会阻断生成的本地问题'
        action = {
            'type': 'system_check',
            'label': primary.get('action') or system_check.get('next_action') or '查看自检',
            'target_page': primary.get('target_page') or '',
            'check_key': primary.get('key') or '',
        }
    elif running_job:
        status = 'running'
        label = '后台 Agent 正在运行'
        summary = '当前有本地 Agent 任务未结束，建议先恢复查看状态'
        action = {
            'type': 'resume_agent_job',
            'label': '恢复查看',
            'job_id': running_job.get('job_id') or '',
        }
    elif unfinished_job:
        status = 'recover'
        label = '有未完成任务'
        summary = '最近一次 Agent 任务未完成，可查看详情或按上次输入重试'
        action = {
            'type': 'agent_recovery',
            'label': '查看详情',
            'job_id': unfinished_job.get('job_id') or '',
        }
    elif latest_resume:
        resume_summary = _safe_resume_summary(latest_resume) or {}
        workflow_status = resume_summary.get('workflow_status') or ''
        quality_status = resume_summary.get('quality_status') or ''
        if workflow_status == 'complete' or quality_status == 'ready':
            status = 'ready'
            label = '最近简历可投递'
            summary = resume_summary.get('quality_summary') or '最近一份简历已完成核心质量检查'
        else:
            status = 'in_progress'
            label = '继续最近简历 Loop'
            summary = resume_summary.get('quality_summary') or '最近一份简历仍有待完成步骤'
        action = resume_summary.get('action') or {'type': 'quality_report', 'label': '查看质量报告'}
    else:
        status = 'empty'
        label = '从第一份 JD 开始'
        summary = '粘贴目标岗位 JD，让后台 Agent 生成第一份可复检简历'
        action = {
            'type': 'generate',
            'label': '开始生成',
            'target_page': 'generate',
        }

    latest = _safe_resume_summary(latest_resume)
    return {
        'success': True,
        'status': status,
        'label': label,
        'summary': summary,
        'action': action,
        'checked_at': datetime.now().isoformat(timespec='seconds'),
        'system': {
            'status': system_check.get('status') or '',
            'label': system_check.get('label') or '',
            'readiness': system_check.get('readiness') or 0,
            'next_action': system_check.get('next_action') or '',
            'blockers': system_blockers,
            'warnings': system_warnings[:3],
        },
        'agent': {
            'running_job': running_job,
            'latest_unfinished_job': unfinished_job,
            'recent_count': len(jobs),
        },
        'gallery': {
            'count': len(resumes),
            'latest_resume': latest,
            'complete_count': sum(
                1
                for item in resumes
                if _status_rank((item.get('workflow_status') or {}).get('status') if isinstance(item.get('workflow_status'), dict) else '') >= 5
            ),
            'needs_action_count': sum(
                1
                for item in resumes
                if ((item.get('workflow_status') or {}).get('status') if isinstance(item.get('workflow_status'), dict) else '') != 'complete'
            ),
        },
    }


def _web_index_has_tokens(tokens: list[str]) -> tuple[bool, list[str]]:
    try:
        text = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
    except OSError:
        return False, tokens
    missing = [token for token in tokens if token not in text]
    return not missing, missing


def _product_check(
    key: str,
    label: str,
    ok: bool,
    detail: str,
    *,
    required: bool = True,
    action: str = '',
    evidence: dict | None = None,
) -> dict:
    return {
        'key': key,
        'label': label,
        'status': 'pass' if ok else 'fail',
        'detail': detail,
        'required': required,
        'action': action,
        'evidence': evidence or {},
    }


def build_product_readiness(host: str | None = None, port: int | None = None) -> dict:
    """Verify the product-level Agent workbench requirements as an acceptance gate."""
    runtime_info = build_runtime_info(host=host, port=port)
    system_check = build_system_check()
    workbench_status = build_workbench_status()
    resumes = list_gallery_resumes()
    latest_resume = _latest_resume_for_workbench(resumes)
    latest_summary = _safe_resume_summary(latest_resume)

    checks: list[dict] = []

    gui_ok, gui_missing = _web_index_has_tokens([
        'id="jd-text"',
        'id="gen-agent"',
        'id="home-gallery-content"',
        'id="pdf-modal-iframe"',
        'id="editor-pdf-iframe"',
        'id="qa-modal-img"',
    ])
    checks.append(_product_check(
        'gui_input_preview',
        '图形界面输入与预览',
        gui_ok,
        '已检测到 JD 输入、Agent 选择、画廊、PDF 预览、编辑预览和视觉 QA 截图入口'
        if gui_ok else '缺少关键 UI 节点：' + '、'.join(gui_missing),
        action='' if gui_ok else '修复 Web UI 入口',
        evidence={'missing_tokens': gui_missing},
    ))

    runtime_steps = runtime_info.get('steps') if isinstance(runtime_info.get('steps'), list) else []
    runtime_step_keys = [str(step.get('key') or '') for step in runtime_steps if isinstance(step, dict)]
    runtime_ok = bool(runtime_info.get('app_url')) and bool(runtime_info.get('launch_command')) and runtime_step_keys == ['start', 'check', 'generate', 'loop']
    checks.append(_product_check(
        'ordinary_user_path',
        '普通用户稳定使用路径',
        runtime_ok,
        '已提供启动、自检、生成、视觉复检与留版的固定路径'
        if runtime_ok else '运行指引不完整，普通用户无法按固定步骤完成闭环',
        action='' if runtime_ok else '补齐运行指引',
        evidence={
            'app_url': runtime_info.get('app_url') or '',
            'launch_command': runtime_info.get('launch_command') or '',
            'step_keys': runtime_step_keys,
        },
    ))

    local_agent = next(
        (check for check in system_check.get('checks', []) if isinstance(check, dict) and check.get('key') == 'local_agent'),
        {},
    )
    agent_jobs_url = str(runtime_info.get('agent_jobs_url') or '')
    agent_ok = local_agent.get('status') == 'pass' and bool(agent_jobs_url)
    checks.append(_product_check(
        'local_agent_backend',
        '本地 Agent 后台运行',
        agent_ok,
        local_agent.get('detail') or ('Agent jobs API 已配置' if agent_jobs_url else '缺少 Agent jobs API'),
        action='' if agent_ok else (local_agent.get('action') or '配置本地 Agent'),
        evidence={'agent_jobs_url': agent_jobs_url, 'system_check': local_agent},
    ))

    system_ok = str(system_check.get('status') or '') in {'ready', 'attention'}
    workbench_action = workbench_status.get('action') if isinstance(workbench_status.get('action'), dict) else {}
    generation_ok = system_ok and bool(workbench_action)
    checks.append(_product_check(
        'generation_entry',
        '生成入口与下一步状态',
        generation_ok,
        f"系统状态 {system_check.get('label') or system_check.get('status')}，工作台下一步：{workbench_action.get('label') or ''}"
        if generation_ok else '系统自检或工作台下一步不可用',
        action='' if generation_ok else (system_check.get('next_action') or '重新自检'),
        evidence={
            'system_status': system_check.get('status') or '',
            'workbench_status': workbench_status.get('status') or '',
            'action': workbench_action,
        },
    ))

    quality = latest_resume.get('quality_report') if isinstance(latest_resume, dict) and isinstance(latest_resume.get('quality_report'), dict) else {}
    workflow = latest_resume.get('workflow_status') if isinstance(latest_resume, dict) and isinstance(latest_resume.get('workflow_status'), dict) else {}
    visual = latest_resume.get('visual_review') if isinstance(latest_resume, dict) and isinstance(latest_resume.get('visual_review'), dict) else {}
    feedback = visual.get('agent_feedback') if isinstance(visual.get('agent_feedback'), dict) else {}
    loop_ok = bool(latest_resume) and visual.get('status') == 'pass' and bool(feedback.get('prompt'))
    checks.append(_product_check(
        'visual_feedback_loop',
        '生成-视觉检查-反馈迭代 Loop',
        loop_ok,
        '最近简历已完成视觉 QA，并生成可复用的下一轮反馈提示'
        if loop_ok else '缺少已通过的视觉 QA 或可复用的 Agent 反馈提示',
        action='' if loop_ok else '运行视觉 QA',
        evidence={
            'latest_resume': latest_summary or {},
            'visual_status': visual.get('status') or '',
            'has_agent_feedback': bool(feedback.get('prompt')),
        },
    ))

    version_count = int((latest_resume or {}).get('version_count') or quality.get('version_count') or 0) if isinstance(latest_resume, dict) else 0
    versions_ui_ok, versions_missing = _web_index_has_tokens([
        'id="versions-drawer"',
        'id="versions-list"',
        'version-snapshot',
    ])
    version_ok = bool(latest_resume) and version_count > 0 and versions_ui_ok
    checks.append(_product_check(
        'version_management',
        '版本管理',
        version_ok,
        f'最近简历已有 {version_count} 个版本快照，版本抽屉和保存入口可用'
        if version_ok else '缺少版本快照或版本管理 UI/API 入口',
        action='' if version_ok else '保存版本',
        evidence={'version_count': version_count, 'missing_ui_tokens': versions_missing},
    ))

    quality_ok = bool(latest_resume) and quality.get('status') == 'ready' and workflow.get('status') == 'complete'
    checks.append(_product_check(
        'quality_gate',
        '质量检查与可投递状态',
        quality_ok,
        quality.get('summary') or ('最近简历质量状态完整' if quality_ok else '最近简历尚未达到可投递质量状态'),
        action='' if quality_ok else (quality.get('next_action') or '打开质量报告'),
        evidence={
            'quality_status': quality.get('status') or '',
            'workflow_status': workflow.get('status') or '',
            'fill_label': quality.get('fill_label') or '',
            'page_count': quality.get('page_count') or 0,
        },
    ))

    delivery_ok = False
    delivery_detail = '尚无可验证的交付包'
    delivery_evidence: dict = {}
    if isinstance(latest_resume, dict) and latest_resume.get('dir_name'):
        try:
            out_dir = _resolve_gallery_output_dir(str(latest_resume.get('dir_name')))
            _, _, delivery_manifest = _build_delivery_package(out_dir)
            delivery_quality = delivery_manifest.get('quality') if isinstance(delivery_manifest.get('quality'), dict) else {}
            delivery_workflow = delivery_manifest.get('workflow') if isinstance(delivery_manifest.get('workflow'), dict) else {}
            delivery_files = delivery_manifest.get('files') if isinstance(delivery_manifest.get('files'), list) else []
            delivery_ok = (
                delivery_quality.get('status') == 'ready'
                and delivery_workflow.get('status') == 'complete'
                and any(str(name).endswith('.pdf') for name in delivery_files)
                and any(str(name).endswith('quality_report.json') for name in delivery_files)
                and any(str(name).endswith('workflow_status.json') for name in delivery_files)
            )
            delivery_detail = (
                f'交付包可构建，包含 {len(delivery_files)} 个文件，质量状态 {delivery_quality.get("label") or delivery_quality.get("status")}'
                if delivery_ok else '交付包可构建但缺少 ready/complete 状态或关键文件'
            )
            delivery_evidence = {
                'quality': delivery_quality,
                'workflow': delivery_workflow,
                'file_count': len(delivery_files),
            }
        except Exception as exc:
            delivery_detail = f'交付包构建失败：{exc}'
            delivery_evidence = {'error': str(exc)}
    checks.append(_product_check(
        'delivery_package',
        '可交付包',
        delivery_ok,
        delivery_detail,
        action='' if delivery_ok else '导出交付包',
        evidence=delivery_evidence,
    ))

    diagnostics_ok = bool(runtime_info.get('success')) and bool(system_check.get('checks')) and bool(workbench_status.get('success'))
    checks.append(_product_check(
        'diagnostics',
        '诊断与支持材料',
        diagnostics_ok,
        '诊断包可导出运行信息、系统自检、工作台状态、任务摘要和画廊摘要'
        if diagnostics_ok else '诊断信息不完整',
        required=False,
        action='' if diagnostics_ok else '导出诊断包',
        evidence={
            'runtime': bool(runtime_info.get('success')),
            'system_checks': len(system_check.get('checks') or []),
            'workbench_success': bool(workbench_status.get('success')),
        },
    ))

    required_failures = [check for check in checks if check.get('required') and check.get('status') != 'pass']
    optional_failures = [check for check in checks if not check.get('required') and check.get('status') != 'pass']
    if required_failures:
        status = 'blocked'
        label = '产品链路未就绪'
        summary = f'还有 {len(required_failures)} 个必需产品化环节未通过'
        next_action = required_failures[0].get('action') or '修复产品化阻断项'
    elif optional_failures:
        status = 'attention'
        label = '核心可交付，建议完善'
        summary = '必需产品链路已通过，但仍有辅助支持项可完善'
        next_action = optional_failures[0].get('action') or '完善辅助项'
    else:
        status = 'ready'
        label = '产品就绪'
        summary = '图形界面、后台 Agent、视觉 QA loop、版本、质量检查和交付包均已通过验收'
        next_action = '交付使用'

    score_map = {'pass': 1.0, 'fail': 0.0}
    readiness = round(sum(score_map.get(c['status'], 0.0) for c in checks) / max(1, len(checks)) * 100)
    return {
        'success': True,
        'status': status,
        'label': label,
        'summary': summary,
        'next_action': next_action,
        'readiness': readiness,
        'checked_at': datetime.now().isoformat(timespec='seconds'),
        'checks': checks,
        'latest_resume': latest_summary,
        'requirements': {
            'gui_input_preview': next(c for c in checks if c['key'] == 'gui_input_preview')['status'] == 'pass',
            'local_agent_backend': next(c for c in checks if c['key'] == 'local_agent_backend')['status'] == 'pass',
            'visual_feedback_loop': next(c for c in checks if c['key'] == 'visual_feedback_loop')['status'] == 'pass',
            'version_management': next(c for c in checks if c['key'] == 'version_management')['status'] == 'pass',
            'quality_gate': next(c for c in checks if c['key'] == 'quality_gate')['status'] == 'pass',
            'ordinary_user_path': next(c for c in checks if c['key'] == 'ordinary_user_path')['status'] == 'pass',
            'delivery_package': next(c for c in checks if c['key'] == 'delivery_package')['status'] == 'pass',
        },
    }


def _safe_agent_job_summary(job: dict | None) -> dict | None:
    if not isinstance(job, dict):
        return None
    task_summary = job.get('task_summary') if isinstance(job.get('task_summary'), dict) else {}
    return {
        'job_id': job.get('job_id') or '',
        'status': job.get('status') or '',
        'agent': job.get('agent') or task_summary.get('agent') or '',
        'task_type': job.get('task_type') or '',
        'company': job.get('company') or task_summary.get('company') or '',
        'role': job.get('role') or task_summary.get('role') or '',
        'language': job.get('language') or task_summary.get('language') or '',
        'step': job.get('step') or '',
        'progress': job.get('progress') or 0,
        'created_at': job.get('created_at') or '',
        'started_at': job.get('started_at') or '',
        'completed_at': job.get('completed_at') or '',
        'updated_at': job.get('updated_at') or '',
        'error_code': job.get('error_code') or '',
        'recoverable': bool(job.get('recoverable')),
        'terminal': bool(job.get('terminal')),
        'task_summary': {
            'agent': task_summary.get('agent') or job.get('agent') or '',
            'company': task_summary.get('company') or job.get('company') or '',
            'role': task_summary.get('role') or job.get('role') or '',
            'language': task_summary.get('language') or job.get('language') or '',
            'has_interview': bool(task_summary.get('has_interview')),
            'has_selection_plan': bool(task_summary.get('has_selection_plan')),
            'has_feedback': bool(task_summary.get('has_feedback')),
        },
    }


def _sanitize_workbench_status_for_export(status: dict) -> dict:
    sanitized = dict(status or {})
    agent = sanitized.get('agent') if isinstance(sanitized.get('agent'), dict) else {}
    sanitized['agent'] = {
        'running_job': _safe_agent_job_summary(agent.get('running_job')),
        'latest_unfinished_job': _safe_agent_job_summary(agent.get('latest_unfinished_job')),
        'recent_count': agent.get('recent_count') or 0,
    }
    return sanitized


def build_diagnostics_payload(host: str | None = None, port: int | None = None) -> dict:
    """Build a safe diagnostics snapshot for product support and local debugging."""
    runtime_info = build_runtime_info(host=host, port=port)
    system_check = build_system_check()
    workbench_status = _sanitize_workbench_status_for_export(build_workbench_status())
    product_readiness = build_product_readiness(host=host, port=port)
    gallery = [_safe_resume_summary(item) for item in list_gallery_resumes()[:12]]
    gallery = [item for item in gallery if item]

    jobs: list[dict] = []
    try:
        from tools.agent_orchestrator import list_agent_jobs
        _, job_payload = list_agent_jobs(limit=12)
        jobs = job_payload.get('jobs') if isinstance(job_payload.get('jobs'), list) else []
    except Exception:
        jobs = []
    safe_jobs = [item for item in (_safe_agent_job_summary(job) for job in jobs) if item]

    return {
        'package_type': 'resume_generator_pro_diagnostics',
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'runtime_info': runtime_info,
        'system_check': system_check,
        'workbench_status': workbench_status,
        'product_readiness': product_readiness,
        'agent_jobs': safe_jobs,
        'gallery_resumes': gallery,
        'privacy': {
            'excluded': [
                'generation_context.json',
                'full_jd_text',
                'full_interview_text',
                'agent_prompt.md',
                'stdout.log',
                'stderr.log',
                'api_keys',
            ],
            'note': '诊断包只包含运行状态和质量摘要，不包含完整 JD、面经、Agent prompt、原始日志或模型密钥',
        },
    }


def _build_diagnostics_package(host: str | None = None, port: int | None = None) -> tuple[bytes, str, dict]:
    payload = build_diagnostics_payload(host=host, port=port)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    root_name = f'resume_generator_pro_diagnostics_{timestamp}'
    files = {
        'diagnostics.json': payload,
        'runtime_info.json': payload['runtime_info'],
        'system_check.json': payload['system_check'],
        'workbench_status.json': payload['workbench_status'],
        'product_readiness.json': payload['product_readiness'],
        'agent_jobs.json': {'jobs': payload['agent_jobs']},
        'gallery_resumes.json': {'resumes': payload['gallery_resumes']},
    }
    manifest = {
        'package_type': payload['package_type'],
        'created_at': payload['created_at'],
        'files': [f'{root_name}/{name}' for name in files],
        'privacy': payload['privacy'],
    }
    readme = '\n'.join([
        'Resume Generator Pro diagnostics package',
        '',
        'This archive contains safe runtime and workflow summaries for local troubleshooting.',
        'It intentionally excludes full JD/interview text, generation_context.json, agent prompts, raw logs, and API keys.',
        '',
        'Start with diagnostics.json or system_check.json.',
        '',
    ])

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f'{root_name}/README.txt', readme)
        for name, value in files.items():
            zf.writestr(
                f'{root_name}/{name}',
                json.dumps(value, ensure_ascii=False, indent=2),
            )
        zf.writestr(
            f'{root_name}/manifest.json',
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    return buffer.getvalue(), f'{root_name}.zip', manifest


def _selected_agent_health(agent_name: str) -> dict:
    """Return health for the exact Agent selected by the user."""
    normalized = str(agent_name or 'codex').strip().lower()
    if normalized in {'auto', 'automatic'}:
        fallback = _healthy_agent_fallback('auto')
        if fallback:
            return {
                'ok': True,
                'status': 'pass',
                'name': fallback['agent'],
                'detail': f'自动选择：{fallback["label"]}；{fallback["detail"]}',
                'action': '',
                'selected_agent': 'auto',
            }
        return {
            'ok': False,
            'status': 'fail',
            'name': 'auto',
            'detail': '没有可用的本地 Agent',
            'action': '安装或登录本地 Agent CLI',
            'error_code': 'NO_HEALTHY_AGENT',
        }
    try:
        from tools.agent_adapters.codex import CodexAdapter
        from tools.agent_adapters.claude_code import ClaudeCodeAdapter
        if normalized == 'codex':
            health = CodexAdapter(PROJECT_ROOT).health_check()
        elif normalized in {'claude', 'claude_code', 'claude-code'}:
            health = ClaudeCodeAdapter(PROJECT_ROOT).health_check()
        else:
            return {
                'ok': False,
                'status': 'fail',
                'name': normalized,
                'detail': f'未知本地 Agent：{agent_name}',
                'action': '选择可用 Agent',
                'error_code': 'UNKNOWN_AGENT',
            }
    except Exception as exc:
        return {
            'ok': False,
            'status': 'fail',
            'name': normalized,
            'detail': f'所选 Agent 检查失败：{exc}',
            'action': '修复本地 Agent',
            'error_code': 'AGENT_HEALTH_CHECK_FAILED',
        }
    return health if isinstance(health, dict) else {
        'ok': False,
        'status': 'fail',
        'name': normalized,
        'detail': '所选 Agent 未返回有效健康状态',
        'action': '修复本地 Agent',
        'error_code': 'AGENT_HEALTH_CHECK_INVALID',
    }


def _agent_display_label(agent_name: str) -> str:
    normalized = str(agent_name or '').strip().lower()
    if normalized in {'auto', 'automatic'}:
        return '自动选择'
    if normalized == 'codex':
        return 'Codex'
    if normalized in {'claude', 'claude_code', 'claude-code'}:
        return 'Claude Code'
    return agent_name or 'Agent'


def _healthy_agent_fallback(selected_agent: str) -> dict | None:
    """Find a healthy alternative Agent the UI can switch to directly."""
    selected = str(selected_agent or 'codex').strip().lower()
    candidates = [
        ('codex', 'Codex'),
        ('claude', 'Claude Code'),
    ]
    for agent_value, label in candidates:
        if agent_value == selected or (selected in {'claude_code', 'claude-code'} and agent_value == 'claude'):
            continue
        health = _selected_agent_health(agent_value)
        if health.get('ok'):
            return {
                'agent': agent_value,
                'label': label,
                'detail': health.get('detail') or f'{label} 可用',
            }
    return None


def build_agent_run_preflight_failure(data: dict | None = None) -> dict | None:
    """Return a structured blocking payload when an Agent job should not start."""
    system_check = build_system_check()
    checks = system_check.get('checks') if isinstance(system_check.get('checks'), list) else []
    checks = list(checks)
    selected_agent_failure = None

    if isinstance(data, dict):
        selected_agent = str(data.get('agent') or 'auto').strip()
        selected_health = _selected_agent_health(selected_agent)
        if not selected_health.get('ok'):
            fallback = _healthy_agent_fallback(selected_agent)
            selected_agent_failure = {
                'key': 'selected_agent',
                'label': f'所选 Agent（{_agent_display_label(selected_agent)}）',
                'status': 'fail',
                'detail': selected_health.get('detail') or '所选本地 Agent 当前不可用',
                'action': selected_health.get('action') or '修复本地 Agent',
                'required': True,
                'target_page': '',
                'agent': selected_health.get('name') or selected_agent,
                'error_code': selected_health.get('error_code') or 'SELECTED_AGENT_UNHEALTHY',
            }
            if fallback:
                selected_agent_failure.update({
                    'fallback_agent': fallback['agent'],
                    'fallback_label': fallback['label'],
                    'fallback_detail': fallback['detail'],
                    'fallback_action': f'切换到 {fallback["label"]}',
                })
            checks.append(selected_agent_failure)
            system_check = {
                **system_check,
                'status': 'blocked',
                'label': '需先修复',
                'summary': '所选后台 Agent 当前不可用',
                'next_action': selected_agent_failure['action'],
                'checks': checks,
            }

    required_failures = [
        c for c in checks
        if isinstance(c, dict) and c.get('required') and c.get('status') == 'fail'
    ]
    if not required_failures:
        return None
    first = selected_agent_failure or required_failures[0]
    label = first.get('label') or '本地自检'
    detail = first.get('detail') or '存在阻断项'
    return {
        'error': f'启动前检查未通过：{label} - {detail}',
        'error_code': 'SELECTED_AGENT_UNHEALTHY' if selected_agent_failure else 'SYSTEM_CHECK_BLOCKED',
        'system_check': system_check,
        'blocking_check': first,
    }


# ─── 简历画廊 ─────────────────────────────────────────────────

def list_gallery_resumes() -> list:
    """扫描 output/ 目录，列出已生成的简历"""
    resumes = []
    if not _output_dir().exists():
        return resumes

    for d in sorted(_output_dir().iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith('.'):
            continue
        context_path = d / 'generation_context.json'
        context = {}
        if context_path.exists():
            try:
                context = json.loads(context_path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                context = {}

        language = context.get('language', infer_language_from_output_dir(d))
        try:
            _, preferred_pdf_name = resolve_resume_filenames(language)
        except ValueError:
            language = infer_language_from_output_dir(d)
            _, preferred_pdf_name = resolve_resume_filenames(language)

        preferred_pdf = d / preferred_pdf_name
        if preferred_pdf.exists():
            pdf = preferred_pdf
        else:
            pdf_files = list(d.glob('*.pdf'))
            if not pdf_files:
                continue
            pdf = sorted(pdf_files, reverse=True)[0]

        parsed_meta = _parse_resume_dir_metadata(d.name)
        parsed_company = parsed_meta.get('company') or d.name
        parsed_role = parsed_meta.get('role') or ''
        company = context.get('company', '') or parsed_company
        role = context.get('role', '') or parsed_role
        date = parsed_meta.get('date') or ''
        rel_path = str(pdf.relative_to(_output_dir()))
        visual_review = context.get('visual_review') if isinstance(context.get('visual_review'), dict) else {}
        if not visual_review:
            review_json = d / 'visual_review' / 'visual_review.json'
            if review_json.exists():
                try:
                    loaded_review = json.loads(review_json.read_text(encoding='utf-8'))
                    if isinstance(loaded_review, dict):
                        visual_review = loaded_review
                except Exception:
                    visual_review = {}
        visual_review = _with_visual_review_screenshot_path(d, visual_review)
        version_count = _get_version_count(d)
        quality_report = _build_resume_quality_report(
            pdf_exists=pdf.exists(),
            visual_review=visual_review,
            version_count=version_count,
            context=context,
        )
        workflow_status = _build_resume_workflow_status(
            pdf_exists=pdf.exists(),
            visual_review=visual_review,
            quality_report=quality_report,
            version_count=version_count,
            context=context,
        )

        resumes.append({
            'company': company,
            'role': role,
            'date': date,
            'dir_name': d.name,
            'pdf_name': pdf.name,
            'pdf_path': rel_path,
            'size': pdf.stat().st_size,
            'version_count': version_count,
            'jd_text': context.get('jd_text', '') or '',
            'interview_text': context.get('interview_text', '') or '',
            'interview_notes': context.get('interview_notes', '') or '',
            'jd_excerpt': _summarize_generation_text(context.get('jd_text', ''), 88),
            'interview_excerpt': _summarize_generation_text(context.get('interview_text', ''), 66),
            'engine': context.get('engine', ''),
            'ai_provider': context.get('ai_provider', ''),
            'ai_model': context.get('ai_model', ''),
            'visual_review': visual_review,
            'quality_report': quality_report,
            'workflow_status': workflow_status,
            'language': language,
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
        elif path.startswith('/assets/'):
            self._serve_asset(path)
        elif path == '/api/persons':
            self._get_persons()
        elif path == '/api/profile':
            self._get_profile()
        elif path == '/api/profile/skill-suggestions':
            self._get_profile_skill_suggestions()
        elif path == '/api/model-config':
            self._get_model_config()
        elif path == '/api/runtime-info':
            self._get_runtime_info()
        elif path == '/api/system-check':
            self._get_system_check()
        elif path == '/api/workbench-status':
            self._get_workbench_status()
        elif path == '/api/product-readiness':
            self._get_product_readiness()
        elif path == '/api/diagnostics-package':
            self._export_diagnostics_package()
        elif path == '/api/extra-info':
            self._get_extra_info()
        elif path == '/api/experiences':
            self._get_experiences()
        elif path.startswith('/api/experiences/') and path.endswith('/content'):
            filename = urllib.parse.unquote(path[len('/api/experiences/'):-len('/content')])
            self._get_experience_content(filename)
        elif path == '/api/gallery':
            self._get_gallery()
        elif path.startswith('/api/gallery/') and path.endswith('/delivery-package'):
            dir_name = urllib.parse.unquote(path[len('/api/gallery/'):-len('/delivery-package')])
            self._export_gallery_delivery_package(dir_name)
        elif path.startswith('/api/gallery/pdf/'):
            rel_path = urllib.parse.unquote(path[len('/api/gallery/pdf/'):])
            self._serve_gallery_pdf(rel_path)
        elif path.startswith('/api/gallery/asset/'):
            rel_path = urllib.parse.unquote(path[len('/api/gallery/asset/'):])
            self._serve_gallery_asset(rel_path)
        elif path.startswith('/gallery/asset/'):
            rel_path = urllib.parse.unquote(path[len('/gallery/asset/'):])
            self._serve_gallery_asset(rel_path)
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
        elif path == '/api/applications':
            self._get_applications()
        elif path == '/api/applications/export':
            self._export_applications()
        elif path == '/monitor' or path == '/monitor.html':
            self._serve_monitor_html()
        elif path == '/api/monitor/logs':
            self._get_monitor_logs()
        elif path == '/api/agent/jobs':
            self._list_agent_jobs()
        elif path.startswith('/api/agent/jobs/') and path.endswith('/log'):
            job_id = urllib.parse.unquote(path[len('/api/agent/jobs/'):-len('/log')])
            self._get_agent_job_log(job_id)
        elif path.startswith('/api/agent/jobs/'):
            job_id = urllib.parse.unquote(path[len('/api/agent/jobs/'):])
            self._get_agent_job(job_id)
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
        elif path == '/api/profile/ai-suggest':
            self._suggest_profile_with_ai()
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
        elif path == '/api/generate-plan':
            self._generate_resume_plan()
        elif path == '/api/generate':
            self._generate_resume()
        elif path == '/api/agent/run':
            self._run_agent_job()
        elif path == '/api/import-resume/create-empty':
            self._create_import_draft()
        elif path == '/api/import-resume/parse':
            self._parse_import_resume()
        elif path == '/api/import-resume/confirm-compile':
            self._confirm_import_compile()
        elif path == '/api/editor/regenerate':
            self._regenerate_resume()
        elif path == '/api/editor/save':
            self._save_editor_tex()
        elif path == '/api/editor/saveas':
            self._saveas_editor_tex()
        elif path == '/api/editor/compile':
            self._compile_editor_tex()
        elif path.startswith('/api/gallery/') and path.endswith('/visual-review'):
            dir_name = urllib.parse.unquote(path[len('/api/gallery/'):-len('/visual-review')])
            self._rerun_gallery_visual_review(dir_name)
        elif path.startswith('/api/gallery/') and path.endswith('/version-snapshot'):
            dir_name = urllib.parse.unquote(path[len('/api/gallery/'):-len('/version-snapshot')])
            self._create_gallery_version_snapshot(dir_name)
        elif path.startswith('/api/gallery/') and path.endswith('/snapshot'):
            dir_name = urllib.parse.unquote(path[len('/api/gallery/'):-len('/snapshot')])
            self._create_gallery_version_snapshot(dir_name)
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
        elif path == '/api/applications':
            self._create_application()
        elif path == '/api/applications/sync-feishu':
            self._send_error_json('飞书同步尚未实现', 501)
        elif path == '/api/monitor/clear':
            self._clear_monitor_logs()
        elif path.startswith('/api/agent/jobs/') and path.endswith('/cancel'):
            job_id = urllib.parse.unquote(path[len('/api/agent/jobs/'):-len('/cancel')])
            self._cancel_agent_job(job_id)
        elif path.startswith('/api/memory/suggestions/') and path.endswith('/apply'):
            job_id = urllib.parse.unquote(path[len('/api/memory/suggestions/'):-len('/apply')])
            self._apply_memory_suggestions(job_id)
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
        elif path.startswith('/api/applications/'):
            app_id_str = urllib.parse.unquote(path[len('/api/applications/'):])
            self._delete_application_record(app_id_str)
        else:
            self.send_error(404)

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > MAX_UPLOAD_SIZE:
            self._send_error_json('请求过大', 413)
            return
        if path.startswith('/api/applications/'):
            app_id_str = urllib.parse.unquote(path[len('/api/applications/'):])
            self._update_application(app_id_str)
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

    def _serve_asset(self, path):
        rel_path = urllib.parse.unquote(path.lstrip('/'))
        asset_path = (WEB_DIR / rel_path).resolve()
        assets_root = (WEB_DIR / 'assets').resolve()
        try:
            asset_path.relative_to(assets_root)
        except ValueError:
            self.send_error(404)
            return
        if not asset_path.is_file():
            self.send_error(404)
            return

        content = asset_path.read_bytes()
        content_type = mimetypes.guess_type(str(asset_path))[0] or 'application/octet-stream'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
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

    def _get_profile_skill_suggestions(self):
        try:
            self._send_json(build_local_skill_suggestions())
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_model_config(self):
        try:
            self._send_json(get_model_config())
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_system_check(self):
        try:
            self._send_json(build_system_check())
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_workbench_status(self):
        try:
            self._send_json(build_workbench_status())
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_product_readiness(self):
        try:
            host = self.headers.get('Host') or ''
            port = int(getattr(self.server, 'server_port', 8765) or 8765)
            self._send_json(build_product_readiness(host=host, port=port))
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _export_diagnostics_package(self):
        try:
            host = self.headers.get('Host') or ''
            port = int(getattr(self.server, 'server_port', 8765) or 8765)
            content, filename, _ = _build_diagnostics_package(host=host, port=port)
            quoted = urllib.parse.quote(filename)
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', len(content))
            self.send_header('Content-Disposition', f"attachment; filename*=UTF-8''{quoted}")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_runtime_info(self):
        try:
            host = self.headers.get('Host') or ''
            port = int(getattr(self.server, 'server_port', 8765) or 8765)
            self._send_json(build_runtime_info(host=host, port=port))
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

    def _suggest_profile_with_ai(self):
        """POST /api/profile/ai-suggest — 用 AI 生成资料补充建议，不直接写入 profile.md"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8')) if body else {}
            profile = data.get('profile') if isinstance(data.get('profile'), dict) else parse_profile()
            source_text = data.get('source_text', '')
            jd_text = data.get('jd_text', '')
            suggestion = ai_suggest_profile_completion(profile, source_text=source_text, jd_text=jd_text)
            self._send_json({'success': True, **suggestion})
        except RuntimeError as e:
            self._send_error_json(str(e), 400)
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

    def _serve_gallery_asset(self, rel_path):
        try:
            if '..' in rel_path or rel_path.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            asset_path = _output_dir() / rel_path
            if not asset_path.exists() or not asset_path.is_file():
                self._send_error_json('文件不存在', 404)
                return
            try:
                asset_path.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            content_type = mimetypes.guess_type(str(asset_path))[0] or 'application/octet-stream'
            allowed = {'image/png', 'image/jpeg', 'image/webp', 'application/json', 'text/plain'}
            if content_type not in allowed:
                self._send_error_json('不支持的文件类型', 403)
                return
            content = asset_path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.send_header('Content-Disposition', f'inline; filename="{asset_path.name}"')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _rerun_gallery_visual_review(self, dir_name):
        try:
            if not dir_name or '..' in dir_name or '/' in dir_name or dir_name.startswith('.'):
                self._send_error_json('非法路径', 403)
                return
            out_dir = _output_dir() / dir_name
            try:
                out_dir.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return
            if not out_dir.exists() or not out_dir.is_dir():
                self._send_error_json('目录不存在', 404)
                return

            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length else b'{}'
            data = json.loads(body.decode('utf-8') or '{}')
            context = _load_generation_context(out_dir)
            requested_language = data.get('language') or context.get('language')
            language = _resolve_output_language(out_dir, requested_language)
            _, _, pdf_path = _resolve_resume_paths(out_dir, language)

            existing_review = context.get('visual_review') if isinstance(context.get('visual_review'), dict) else {}
            if not existing_review:
                review_json = out_dir / 'visual_review' / 'visual_review.json'
                if review_json.exists():
                    try:
                        loaded_review = json.loads(review_json.read_text(encoding='utf-8'))
                        if isinstance(loaded_review, dict):
                            existing_review = loaded_review
                    except Exception:
                        existing_review = {}

            compile_result = {'success': True, 'log_tail': ''}
            should_compile = bool(data.get('compile')) or not pdf_path.exists()
            if should_compile:
                compile_result = _compile_resume_dir(out_dir, language=language)
                if not compile_result.get('success'):
                    self._send_json({
                        'success': False,
                        'output_dir': dir_name,
                        'language': language,
                        'error': compile_result.get('error', '编译失败'),
                        'log_tail': compile_result.get('log_tail', ''),
                    })
                    return

            metrics = _measure_resume_metrics(
                out_dir,
                language,
                context=context,
                existing_review=existing_review,
                compile_result=compile_result,
            )
            fill_ratio = metrics.get('fill_ratio', 0.0)
            page_count = int(metrics.get('page_count') or 0)
            from tools.generate_resume import run_visual_review
            visual_review = run_visual_review(
                out_dir,
                pdf_filename=pdf_path.name,
                fill_ratio=fill_ratio,
                page_count=page_count,
            )
            visual_review['reviewed_at'] = datetime.now().isoformat(timespec='seconds')
            visual_review['agent_feedback'] = _build_visual_review_agent_feedback(visual_review)
            review_json = out_dir / 'visual_review' / 'visual_review.json'
            review_json.write_text(json.dumps(visual_review, ensure_ascii=False, indent=2), encoding='utf-8')
            enriched_review = _with_visual_review_screenshot_path(out_dir, visual_review)

            context.update({
                'language': language,
                'fill_ratio': fill_ratio,
                'page_count': page_count,
                'pages': page_count,
                'visual_review': visual_review,
                'visual_review_updated_at': visual_review['reviewed_at'],
            })
            _save_generation_context(out_dir, context)
            version_count = _get_version_count(out_dir)
            quality_report = _build_resume_quality_report(
                pdf_exists=pdf_path.exists(),
                visual_review=enriched_review,
                version_count=version_count,
                context=context,
            )
            workflow_status = _build_resume_workflow_status(
                pdf_exists=pdf_path.exists(),
                visual_review=enriched_review,
                quality_report=quality_report,
                version_count=version_count,
                context=context,
            )

            self._send_json({
                'success': True,
                'output_dir': dir_name,
                'pdf_path': str(pdf_path.relative_to(_output_dir())),
                'language': language,
                'pages': page_count,
                'fill_ratio': fill_ratio,
                'visual_review': enriched_review,
                'quality_report': quality_report,
                'workflow_status': workflow_status,
                'version_count': version_count,
                'log_tail': compile_result.get('log_tail', ''),
            })
        except json.JSONDecodeError:
            self._send_error_json('请求 JSON 格式错误', 400)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _create_gallery_version_snapshot(self, dir_name):
        try:
            out_dir = _resolve_gallery_output_dir(dir_name)
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length else b'{}'
            data = json.loads(body.decode('utf-8') or '{}')
            payload = _create_manual_version_snapshot(
                out_dir,
                note=str(data.get('note') or '').strip()[:160],
                language=data.get('language'),
            )
            self._send_json(payload)
        except ValueError as e:
            self._send_error_json(str(e), 403)
        except FileNotFoundError as e:
            self._send_error_json(str(e), 404)
        except json.JSONDecodeError:
            self._send_error_json('请求 JSON 格式错误', 400)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _export_gallery_delivery_package(self, dir_name):
        """GET /api/gallery/{dir}/delivery-package — 下载单份简历交付包"""
        try:
            out_dir = _resolve_gallery_output_dir(dir_name)
            content, filename, _ = _build_delivery_package(out_dir)
            quoted = urllib.parse.quote(filename)
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', len(content))
            self.send_header(
                'Content-Disposition',
                f"attachment; filename*=UTF-8''{quoted}",
            )
            self.end_headers()
            self.wfile.write(content)
        except ValueError as e:
            self._send_error_json(str(e), 403)
        except FileNotFoundError as e:
            self._send_error_json(str(e), 404)
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
            current_language = _resolve_output_language(target_dir, context.get('language'))
            _create_version_snapshot(
                target_dir,
                fill_rate=context.get('fill_ratio', 0) * 100,
                language=current_language,
            )

            # Generate into a temp dir (generate_resume always creates new dir)
            result = generate_resume(
                jd_text,
                interview_text,
                company=company,
                role=role,
                person_id=get_active_person_id(),
                prefer_ai=prefer_ai,
                feedback=feedback,
                language=current_language,
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
            new_ver = _create_version_snapshot(
                target_dir,
                fill_rate=fill_ratio * 100,
                language=current_language,
            )
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
            _, tex_path, pdf_path = _resolve_resume_paths(target_dir, current_language)
            tex_content = tex_path.read_text(encoding='utf-8') if tex_path.exists() else ''

            self._send_json({
                'success': True,
                'content': tex_content,
                'version': new_ver,
                'fill_ratio': fill_ratio,
                'version_count': _get_version_count(target_dir),
                'language': current_language,
                'pdf_name': pdf_path.name,
                'tex_name': tex_path.name,
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_error_json(str(e), 500)

    def _generate_resume_plan(self):
        """生成可确认的简历方案，不写文件、不编译 PDF"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            jd_text = data.get('jd', '').strip()
            if not jd_text:
                self._send_json({'error': '请输入 JD 内容'}, status=400)
                return

            try:
                language = normalize_language(data.get('language'))
            except ValueError as exc:
                self._send_json({'error': str(exc), 'error_code': 'INVALID_LANGUAGE'}, status=400)
                return

            mode = str(data.get('mode', 'platform_key')).strip() or 'platform_key'
            ai_config_override = None
            if mode == 'byok':
                ai_config_override = _build_byok_ai_config_override(data)
                if ai_config_override is None:
                    self._send_json({'error': 'BYOK_INVALID', 'error_code': 'BYOK_INVALID'}, status=400)
                    return

            import sys
            tools_dir = str(PROJECT_ROOT / 'tools')
            if tools_dir not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))

            from tools.generate_resume import build_resume_plan

            requested_person_id = str(data.get('person_id', '')).strip()
            if _auth_billing_enabled():
                user_id, auth_error = _extract_auth_context(self.headers)
                if auth_error:
                    self._send_json({'error': auth_error, 'error_code': auth_error}, status=401)
                    return
                person_id = requested_person_id or get_active_person_id()
                if user_id != f'owner:{person_id}':
                    self._send_json({'error': 'PERSON_NOT_AUTHORIZED', 'error_code': 'PERSON_NOT_AUTHORIZED'}, status=403)
                    return
            else:
                person_id = get_active_person_id()

            result = build_resume_plan(
                jd_text,
                data.get('interview', '').strip(),
                company=data.get('company', '').strip(),
                role=data.get('role', '').strip(),
                person_id=person_id,
                prefer_ai=bool(data.get('prefer_ai')),
                feedback=data.get('feedback', '').strip(),
                language=language,
                ai_config_override=ai_config_override,
            )
            self._send_json(result, status=200 if result.get('success') else 400)

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

    def _run_agent_job(self):
        """创建本地 coding-agent 生成任务并立即返回 job 状态"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8')) if body else {}

            preflight_failure = build_agent_run_preflight_failure(data)
            if preflight_failure:
                self._send_json(preflight_failure, status=409)
                return

            from tools.agent_orchestrator import start_agent_job

            status, result = start_agent_job(
                data,
                active_person_id=get_active_person_id(),
            )
            self._send_json(result, status=status)

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_error_json(str(e), 500)

    def _list_agent_jobs(self):
        """GET /api/agent/jobs — 查询最近本地 agent 任务，不返回完整 JD"""
        try:
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            try:
                limit = int(params.get('limit', ['8'])[0] or 8)
            except ValueError:
                limit = 8
            from tools.agent_orchestrator import list_agent_jobs
            status, result = list_agent_jobs(limit=limit)
            self._send_json(result, status=status)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_agent_job(self, job_id):
        """GET /api/agent/jobs/{job_id} — 查询本地 agent 任务状态"""
        try:
            from tools.agent_orchestrator import get_agent_job
            status, result = get_agent_job(job_id)
            result = _enrich_agent_job_payload(result)
            self._send_json(result, status=status)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _get_agent_job_log(self, job_id):
        """GET /api/agent/jobs/{job_id}/log?since_seq=N — 查询增量日志"""
        try:
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            try:
                since_seq = int(params.get('since_seq', ['0'])[0] or 0)
            except ValueError:
                since_seq = 0
            from tools.agent_orchestrator import get_agent_job_log
            status, result = get_agent_job_log(job_id, since_seq=since_seq)
            self._send_json(result, status=status)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _cancel_agent_job(self, job_id):
        """POST /api/agent/jobs/{job_id}/cancel — 取消本地 agent 任务"""
        try:
            from tools.agent_orchestrator import cancel_agent_job
            status, result = cancel_agent_job(job_id)
            self._send_json(result, status=status)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _apply_memory_suggestions(self, job_id):
        """POST /api/memory/suggestions/{job_id}/apply — 确认写入长期记忆建议"""
        try:
            from tools.agent_orchestrator import apply_memory_suggestions
            status, result = apply_memory_suggestions(job_id)
            self._send_json(result, status=status)
        except Exception as e:
            self._send_error_json(str(e), 500)

    # ─── LaTeX Editor API ──────────────────────────────────────

    def _get_editor_tex(self, dir_name):
        """读取 output 目录中的 .tex 文件内容"""
        try:
            if not dir_name or '..' in dir_name or dir_name.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            out_dir = _output_dir() / dir_name
            language, tex_path, pdf_path = _resolve_resume_paths(out_dir)
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
                'filename': tex_path.name,
                'dir_name': dir_name,
                'language': language,
                'pdf_name': pdf_path.name,
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
            out_dir = _output_dir() / dir_name
            _, tex_path, _ = _resolve_resume_paths(out_dir)
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
            req_language = data.get('language')
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
            source_path = _output_dir() / source_dir if source_dir else None
            inferred_language = req_language
            if not inferred_language and source_path and source_path.exists():
                inferred_language = infer_language_from_output_dir(source_path)
            language = normalize_language(inferred_language)
            tex_name, _ = resolve_resume_filenames(language)
            # Copy latex support files from source (fallback to template)
            src_dir = source_path
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
            tex_path = new_path / tex_name
            tex_path.write_text(tex_content, encoding='utf-8')
            context = _load_generation_context(new_path)
            context['language'] = language
            _save_generation_context(new_path, context)
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
            req_language = data.get('language')
            if not dir_name or '..' in dir_name or dir_name.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            out_dir = _output_dir() / dir_name
            language, tex_path, pdf_path = _resolve_resume_paths(out_dir, req_language)
            tex_name, _ = resolve_resume_filenames(language)
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
                [xelatex_bin, '-interaction=nonstopmode', '--synctex=1', tex_name],
                cwd=str(out_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )

            log_path = out_dir / f'{Path(tex_name).stem}.log'

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
                    ['python3', fill_check, str(out_dir), xelatex_bin, tex_name],
                    capture_output=True, text=True, timeout=60, env=env,
                )
                m = re.search(r'填充率[：:]\s*([\d.]+)%', fr_result.stdout)
                if m:
                    fill_rate = float(m.group(1))
            except Exception:
                pass

            # 7. Create version snapshot
            version_num = _create_version_snapshot(out_dir, fill_rate, pages, language=language)

            self._send_json({
                'success': True,
                'pages': pages,
                'fill_rate': fill_rate,
                'errors': '',
                'log_tail': log_tail if pages > 1 else '',
                'version_count': _get_version_count(out_dir),
                'pdf_name': pdf_path.name,
                'tex_name': tex_name,
                'language': language,
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
            req_language = data.get('language')
            if not dir_name or '..' in dir_name or dir_name.startswith('/'):
                self._send_error_json('非法路径', 403)
                return
            out_dir = _output_dir() / dir_name
            try:
                out_dir.resolve().relative_to(_output_dir().resolve())
            except ValueError:
                self._send_error_json('非法路径', 403)
                return

            _, tex_file_path, pdf_file = _resolve_resume_paths(out_dir, req_language)
            tex_file = tex_file_path.name

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

    def _create_import_draft(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8')) if body else {}
            company = str(data.get('company', '')).strip()
            role = str(data.get('role', '')).strip()
            try:
                language = normalize_language(data.get('language'))
            except ValueError as e:
                self._send_error_json(str(e), 400)
                return
            dir_name = create_import_draft_dir(company, role, language=language)
            self._send_json({'success': True, 'dir_name': dir_name})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _parse_import_resume(self):
        try:
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self._send_error_json('请使用 multipart/form-data 格式上传')
                return

            content_length = int(self.headers.get('Content-Length', 0))
            environ = {
                'REQUEST_METHOD': 'POST',
                'CONTENT_TYPE': content_type,
                'CONTENT_LENGTH': str(content_length),
            }
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=environ)
            file_item = form['file'] if 'file' in form else None
            if isinstance(file_item, list):
                file_item = file_item[0] if file_item else None
            if file_item is None or not getattr(file_item, 'filename', ''):
                self._send_error_json('未找到上传文件', 400)
                return

            filename = sanitize_filename(file_item.filename)
            content = file_item.file.read()
            text = extract_text_from_upload(filename, content)
            if not text.strip():
                self._send_error_json('文件内容为空或无法解析', 400)
                return
            # AI 优先解析，正则兜底
            engine = 'regex'
            try:
                parsed = _ai_parse_resume_text(text)
                if parsed:
                    engine = 'ai'
            except Exception:
                parsed = {}
            if not parsed:
                parsed = parse_resume_text_to_structured(text)
                if engine != 'ai':
                    engine = 'regex'
            self._send_json({'success': True, 'filename': filename, 'structured': parsed, 'engine': engine})
        except ValueError as e:
            self._send_error_json(str(e), 400)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _confirm_import_compile(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            structured = data.get('structured')
            if not isinstance(structured, dict):
                self._send_error_json('structured 格式错误', 400)
                return

            company = str(data.get('company', '')).strip()
            role = str(data.get('role', '')).strip()
            dir_name = str(data.get('dir_name', '')).strip()
            req_language_raw = data.get('language')
            if dir_name:
                if '..' in dir_name or '/' in dir_name or dir_name.startswith('.'):
                    self._send_error_json('非法路径', 403)
                    return
                out_dir = _output_dir() / dir_name
                try:
                    out_dir.resolve().relative_to(_output_dir().resolve())
                except ValueError:
                    self._send_error_json('非法路径', 403)
                    return
                if not out_dir.exists():
                    self._send_error_json('目标目录不存在', 404)
                    return
            else:
                req_language = normalize_language(req_language_raw)
                dir_name = create_import_draft_dir(company, role, language=req_language)
                out_dir = _output_dir() / dir_name

            inferred_language = infer_language_from_output_dir(out_dir)
            if req_language_raw not in (None, ''):
                req_language = normalize_language(req_language_raw)
                if req_language != inferred_language:
                    self._send_error_json('language 与草稿目录不一致，请新建对应语言草稿目录', 400)
                    return
                final_language = req_language
            else:
                final_language = inferred_language

            tex_name, pdf_name = resolve_resume_filenames(final_language)
            written_files = _persist_imported_data(structured)
            tex_content = render_imported_resume_tex(structured, language=final_language)
            (out_dir / tex_name).write_text(tex_content, encoding='utf-8')

            context = _load_generation_context(out_dir)
            context.update({
                'company': company,
                'role': role,
                'engine': 'import',
                'jd_text': '',
                'interview_text': '',
                'language': final_language,
                'generated_at': datetime.now().isoformat(timespec='seconds'),
                'written_experiences': written_files,
            })
            _save_generation_context(out_dir, context)

            compile_result = _compile_resume_dir(out_dir, language=final_language)
            if not compile_result.get('success'):
                self._send_json({
                    'success': False,
                    'output_dir': dir_name,
                    'error': compile_result.get('error', '编译失败'),
                    'log_tail': compile_result.get('log_tail', ''),
                })
                return

            _create_version_snapshot(
                out_dir,
                compile_result.get('fill_rate', 0.0),
                compile_result.get('pages', 1),
                language=final_language,
            )
            self._send_json({
                'success': True,
                'output_dir': dir_name,
                'pdf_path': f'{dir_name}/{pdf_name}',
                'language': final_language,
                'pages': compile_result.get('pages', 1),
                'fill_rate': compile_result.get('fill_rate', 0.0),
                'log_tail': compile_result.get('log_tail', ''),
            })
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

    # ─── 投递记录 ─────────────────────────────────────────────

    def _get_applications(self):
        """GET /api/applications — 投递记录列表"""
        try:
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            status = params.get('status', [None])[0]
            limit = int(params.get('limit', [200])[0])
            apps = ext_get_applications(limit=limit, status=status)
            self._send_json({'applications': apps})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _export_applications(self):
        """GET /api/applications/export — 导出全部投递记录"""
        try:
            apps = ext_get_applications(limit=10000)
            self._send_json({'applications': apps, 'exported_at': datetime.now().isoformat()})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _create_application(self):
        """POST /api/applications — 新增投递记录"""
        try:
            data = self._ext_read_body()
            app_id = ext_create_application(
                company=data.get('company', ''),
                role=data.get('role', ''),
                url=data.get('url', ''),
                platform=data.get('platform', ''),
                resume_dir=data.get('resume_dir', ''),
                status=data.get('status', '投递'),
                notes=data.get('notes', ''),
                fill_id=data.get('fill_id'),
            )
            self._send_json({'id': app_id, 'success': True})
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _update_application(self, app_id_str: str):
        """PUT /api/applications/{id} — 更新投递记录"""
        try:
            app_id = int(app_id_str)
        except ValueError:
            self._send_error_json('无效的 ID', 400)
            return
        try:
            data = self._ext_read_body()
            success = ext_update_application(app_id, **data)
            if success:
                self._send_json({'success': True})
            else:
                self._send_error_json('记录不存在', 404)
        except Exception as e:
            self._send_error_json(str(e), 500)

    def _delete_application_record(self, app_id_str: str):
        """DELETE /api/applications/{id} — 删除投递记录"""
        try:
            app_id = int(app_id_str)
        except ValueError:
            self._send_error_json('无效的 ID', 400)
            return
        try:
            success = ext_delete_application(app_id)
            if success:
                self._send_json({'success': True})
            else:
                self._send_error_json('记录不存在', 404)
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
    parser.add_argument('--host', default='127.0.0.1', help='绑定地址 (默认 127.0.0.1，容器内用 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8765, help='服务端口 (默认 8765)')
    parser.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    args = parser.parse_args()

    # 自动迁移到多人模式
    _maybe_migrate()

    # 确保目录存在
    _experiences_dir().mkdir(parents=True, exist_ok=True)
    _work_materials_dir().mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), ResumeHandler)
    url = f'http://{"localhost" if args.host == "127.0.0.1" else args.host}:{args.port}'
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
