from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.agent_adapters.codex import CodexAdapter
from tools.agent_adapters.claude_code import ClaudeCodeAdapter
from tools.language_utils import normalize_language
from tools.person_manager import get_active_person_id, get_person_profile_path, list_persons


PROJECT_ROOT = Path(__file__).resolve().parent.parent
JOB_ROOT = PROJECT_ROOT / '.tmp' / 'agent_jobs'
DEFAULT_TIMEOUT_SEC = int(os.getenv('RESUME_AGENT_TIMEOUT_SEC', '600'))
MAX_EVENTS_RETURNED = 200

_processes: dict[str, Any] = {}
_process_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _safe_job_id(job_id: str) -> str:
    clean = ''.join(ch for ch in str(job_id) if ch.isalnum() or ch in {'_', '-'})
    if not clean or clean != job_id:
        raise ValueError('INVALID_JOB_ID')
    return clean


def _job_dir(job_id: str) -> Path:
    return JOB_ROOT / _safe_job_id(job_id)


def _status_path(job_dir: Path) -> Path:
    return job_dir / 'status.json'


def _events_path(job_dir: Path) -> Path:
    return job_dir / 'events.jsonl'


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default
    return default


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _event(job_dir: Path, level: str, message: str, *, data: dict | None = None) -> dict:
    events_file = _events_path(job_dir)
    seq = 1
    if events_file.exists():
        try:
            with events_file.open('rb') as fh:
                seq = sum(1 for _ in fh) + 1
        except Exception:
            seq = int(time.time() * 1000)
    entry = {
        'seq': seq,
        'time': _now_iso(),
        'level': level,
        'message': message,
    }
    if data is not None:
        entry['data'] = data
    with events_file.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return entry


def _update_status(job_dir: Path, **updates) -> dict:
    status = _read_json(_status_path(job_dir), {})
    status.update(updates)
    status['updated_at'] = _now_iso()
    _write_json(_status_path(job_dir), status)
    return status


def _adapter_for(agent: str):
    agent = (agent or 'codex').strip().lower()
    if agent in {'auto', 'automatic'}:
        return _auto_adapter()
    if agent == 'codex':
        return CodexAdapter(PROJECT_ROOT)
    if agent in {'claude', 'claude_code', 'claude-code'}:
        return ClaudeCodeAdapter(PROJECT_ROOT)
    raise ValueError('UNSUPPORTED_AGENT')


def _auto_adapter():
    issues: list[str] = []
    for adapter in (CodexAdapter(PROJECT_ROOT), ClaudeCodeAdapter(PROJECT_ROOT)):
        health = adapter.health_check()
        if health.get('ok'):
            return adapter
        if health.get('detail'):
            issues.append(str(health.get('detail')))
    detail = '；'.join(issues[:2]) or '没有可用的本地 Agent'
    raise ValueError(f'NO_HEALTHY_AGENT: {detail}')


def _resolve_person_id(requested: str | None) -> str | None:
    person_id = (requested or '').strip() or get_active_person_id()
    if person_id is None:
        return None
    if '/' in person_id or '\\' in person_id or '..' in person_id:
        raise ValueError('INVALID_PERSON_ID')
    valid_ids = {p.get('id') for p in list_persons()}
    if valid_ids and person_id not in valid_ids:
        raise ValueError('PERSON_NOT_FOUND')
    get_person_profile_path(person_id)
    return person_id


def _active_running_jobs() -> list[dict]:
    jobs: list[dict] = []
    if not JOB_ROOT.exists():
        return jobs
    for status_file in JOB_ROOT.glob('*/status.json'):
        status = _read_json(status_file, {})
        if status.get('status') in {'queued', 'running'}:
            if time.time() - status_file.stat().st_mtime > DEFAULT_TIMEOUT_SEC:
                _event(status_file.parent, 'error', '任务状态已过期，标记为失败')
                _update_status(
                    status_file.parent,
                    status='failed',
                    step='stale',
                    progress=100,
                    error='任务状态已过期',
                    error_code='AGENT_STALE_JOB',
                    completed_at=_now_iso(),
                )
                continue
            jobs.append(status)
    return jobs


def _cleanup_success_jobs(keep: int = 20) -> None:
    if not JOB_ROOT.exists():
        return
    completed: list[tuple[float, Path]] = []
    for status_file in JOB_ROOT.glob('*/status.json'):
        status = _read_json(status_file, {})
        if status.get('status') != 'completed':
            continue
        completed.append((status_file.stat().st_mtime, status_file.parent))
    for _, path in sorted(completed, reverse=True)[keep:]:
        try:
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
        except Exception:
            pass


def _task_summary(job_dir: Path, status: dict | None = None) -> dict:
    task = _read_json(job_dir / 'task.json', None)
    status = status or {}
    if not isinstance(task, dict):
        return {
            'agent': status.get('agent') or '',
            'company': status.get('company') or '',
            'role': status.get('role') or '',
            'language': status.get('language') or '',
            'has_interview': False,
            'has_selection_plan': False,
            'has_feedback': False,
        }
    return {
        'agent': task.get('agent') or status.get('agent') or '',
        'company': task.get('company') or status.get('company') or '',
        'role': task.get('role') or status.get('role') or '',
        'language': task.get('language') or status.get('language') or '',
        'has_interview': bool(str(task.get('interview') or '').strip()),
        'has_selection_plan': isinstance(task.get('selection_plan'), dict),
        'has_feedback': bool(str(task.get('feedback') or '').strip()),
    }


def list_agent_jobs(*, limit: int = 8) -> tuple[int, dict]:
    """Return recent local Agent jobs without exposing full prompt/JD text."""
    if not JOB_ROOT.exists():
        return 200, {'success': True, 'jobs': []}

    jobs: list[dict] = []
    for status_file in JOB_ROOT.glob('*/status.json'):
        status = _read_json(status_file, None)
        if not isinstance(status, dict):
            continue
        job_dir = status_file.parent
        item = {
            'job_id': status.get('job_id') or job_dir.name,
            'status': status.get('status') or 'unknown',
            'agent': status.get('agent') or '',
            'task_type': status.get('task_type') or '',
            'person_id': status.get('person_id') or '',
            'company': status.get('company') or '',
            'role': status.get('role') or '',
            'language': status.get('language') or '',
            'step': status.get('step') or '',
            'progress': status.get('progress') or 0,
            'created_at': status.get('created_at') or '',
            'started_at': status.get('started_at') or '',
            'completed_at': status.get('completed_at') or '',
            'updated_at': status.get('updated_at') or '',
            'error': status.get('error') or '',
            'error_code': status.get('error_code') or '',
            'recoverable': status.get('status') in {'queued', 'running'},
            'terminal': status.get('status') in {'completed', 'failed', 'cancelled'},
            'task_summary': _task_summary(job_dir, status),
            '_mtime': status_file.stat().st_mtime,
        }
        jobs.append(item)

    jobs.sort(key=lambda item: item.get('_mtime') or 0, reverse=True)
    safe_limit = max(1, min(50, int(limit or 8)))
    for item in jobs:
        item.pop('_mtime', None)
    return 200, {'success': True, 'jobs': jobs[:safe_limit]}


def _build_agent_prompt(task: dict, job_dir: Path) -> str:
    task_json = json.dumps(task, ensure_ascii=False, indent=2)
    job_dir_str = str(job_dir)
    return f"""你是本地 Resume Generator Pro 的 coding-agent worker。你只执行本任务，不做产品设计。

工作目录：
{PROJECT_ROOT}

任务数据 JSON：
```json
{task_json}
```

必须完成：
1. 读取对应 person 的 profile、experiences 和 memory 目录作为上下文
2. 调用现有 `tools.generate_resume.generate_resume`，不要重写简历业务规则
3. 将结构化结果写入 `{job_dir_str}/result.json`
4. 如果有长期记忆建议，仅写入 `{job_dir_str}/memory_suggestions.json`，不要直接修改长期 memory

请直接执行下面这段 Python。除非出现导入错误，不要改写它。

```bash
python3 - <<'PY'
import json
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path({str(PROJECT_ROOT)!r})
JOB_DIR = Path({job_dir_str!r})
TASK = json.loads({json.dumps(json.dumps(task, ensure_ascii=False))!r})

sys.path.insert(0, str(PROJECT_ROOT))

def write_event(level, message, data=None):
    events_path = JOB_DIR / 'events.jsonl'
    seq = 1
    if events_path.exists():
        try:
            seq = sum(1 for _ in events_path.open('rb')) + 1
        except Exception:
            seq = 1
    payload = {{
        'seq': seq,
        'time': __import__('datetime').datetime.now().astimezone().isoformat(timespec='seconds'),
        'level': level,
        'message': message,
    }}
    if data is not None:
        payload['data'] = data
    with events_path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + '\\n')

try:
    write_event('info', 'Agent 已开始调用现有生成引擎')
    from tools.generate_resume import generate_resume

    result = generate_resume(
        TASK.get('jd', ''),
        TASK.get('interview', ''),
        company=TASK.get('company', ''),
        role=TASK.get('role', ''),
        person_id=TASK.get('person_id'),
        prefer_ai=bool(TASK.get('prefer_ai')),
        feedback=TASK.get('feedback', ''),
        language=TASK.get('language', 'zh_CN'),
        ai_config_override=TASK.get('ai_config_override'),
        selection_plan=TASK.get('selection_plan'),
    )

    normalized = {{
        'success': bool(result.get('success')),
        'company': result.get('company') or TASK.get('company', ''),
        'role': result.get('role') or TASK.get('role', ''),
        'language': result.get('language') or TASK.get('language', 'zh_CN'),
        'output_dir': result.get('output_dir', ''),
        'pdf_path': result.get('pdf_path', ''),
        'tex_path': '',
        'generation_log': result.get('generation_log', ''),
        'fill_ratio': result.get('fill_ratio', 0),
        'visual_review': result.get('visual_review') or {{}},
        'summary': '已生成简历 PDF' if result.get('success') else result.get('error', '生成失败'),
        'engine': result.get('engine'),
        'ai_provider': result.get('ai_provider'),
        'ai_model': result.get('ai_model'),
        'error': result.get('error'),
    }}
    if normalized.get('visual_review') and normalized.get('output_dir'):
        _shots = normalized['visual_review'].get('screenshots') or []
        if _shots:
            normalized['visual_review']['screenshot_path'] = str(Path(normalized['output_dir']) / _shots[0])
    if normalized['output_dir']:
        from tools.language_utils import resolve_resume_filenames
        tex_filename, _pdf_filename = resolve_resume_filenames(normalized['language'])
        person_part = TASK.get('person_id') or ''
        base_parts = [Path('output')]
        if person_part:
            base_parts.append(Path(person_part))
        tex_base = base_parts[0]
        for part in base_parts[1:]:
            tex_base = tex_base / part
        normalized['tex_path'] = str(tex_base / normalized['output_dir'] / tex_filename)

    (JOB_DIR / 'result.json').write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding='utf-8')
    (JOB_DIR / 'memory_suggestions.json').write_text(json.dumps({{'suggestions': []}}, ensure_ascii=False, indent=2), encoding='utf-8')
    write_event('done' if normalized['success'] else 'error', normalized['summary'])
except Exception as exc:
    error = {{
        'success': False,
        'error': str(exc),
        'traceback': traceback.format_exc(),
        'summary': 'Agent 调用生成引擎失败',
    }}
    (JOB_DIR / 'result.json').write_text(json.dumps(error, ensure_ascii=False, indent=2), encoding='utf-8')
    write_event('error', 'Agent 调用生成引擎失败', {{'error': str(exc)}})
    raise
PY
```

最终要求：
- 不要修改 `data/{{person_id}}/profile.md` 或 `data/{{person_id}}/experiences/`
- 不要直接写入长期 memory
- 如果 Python 执行失败，请说明失败原因
"""


def start_agent_job(data: dict, *, active_person_id: str | None = None) -> tuple[int, dict]:
    jd = str(data.get('jd', '')).strip()
    if not jd:
        return 400, {'error': '请输入 JD 内容'}

    try:
        agent = str(data.get('agent') or 'auto').strip().lower()
        adapter = _adapter_for(agent)
        person_id = _resolve_person_id(str(data.get('person_id') or active_person_id or '').strip())
        language = normalize_language(data.get('language'))
    except ValueError as exc:
        return 400, {'error': str(exc), 'error_code': str(exc)}

    running = _active_running_jobs()
    if running:
        return 409, {
            'error': '已有本地 Agent 任务正在运行',
            'error_code': 'AGENT_JOB_RUNNING',
            'job_id': running[0].get('job_id'),
        }
    health = adapter.health_check()
    if not health.get('ok'):
        return 400, {
            'error': health.get('detail') or f'{adapter.executable} CLI 当前不可用',
            'error_code': health.get('error_code') or 'AGENT_NOT_HEALTHY',
            'agent_health': health,
        }

    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    _cleanup_success_jobs()

    job_id = f"agent_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{agent}_{uuid4().hex[:8]}"
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=False)

    task = {
        'agent': adapter.name,
        'task_type': 'resume_generate',
        'person_id': person_id,
        'jd': jd,
        'interview': str(data.get('interview', '') or ''),
        'company': str(data.get('company', '') or '').strip(),
        'role': str(data.get('role', '') or '').strip(),
        'language': language,
        'prefer_ai': bool(data.get('prefer_ai')),
        'feedback': str(data.get('feedback', '') or ''),
        'selection_plan': data.get('selection_plan') if isinstance(data.get('selection_plan'), dict) else None,
        'ai_config_override': data.get('ai_config_override') if isinstance(data.get('ai_config_override'), dict) else None,
    }
    (job_dir / 'task.json').write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding='utf-8')
    prompt = _build_agent_prompt(task, job_dir)
    prompt_path = job_dir / 'prompt.md'
    prompt_path.write_text(prompt, encoding='utf-8')
    (job_dir / 'stdout.log').touch()
    (job_dir / 'stderr.log').touch()
    (job_dir / 'memory_suggestions.json').write_text(json.dumps({'suggestions': []}, ensure_ascii=False, indent=2), encoding='utf-8')

    base_status = {
        'success': True,
        'job_id': job_id,
        'status': 'queued',
        'agent': adapter.name,
        'task_type': 'resume_generate',
        'person_id': person_id,
        'company': task['company'],
        'role': task['role'],
        'language': language,
        'step': 'queued',
        'progress': 0,
        'created_at': _now_iso(),
        'started_at': None,
        'completed_at': None,
        'error': None,
        'error_code': None,
        'status_url': f'/api/agent/jobs/{job_id}',
        'log_url': f'/api/agent/jobs/{job_id}/log',
    }
    _write_json(_status_path(job_dir), base_status)
    _event(job_dir, 'info', '已创建本地 Agent 任务')

    try:
        launch = adapter.launch(
            prompt_path=prompt_path,
            stdout_path=job_dir / 'stdout.log',
            stderr_path=job_dir / 'stderr.log',
        )
    except Exception as exc:
        _event(job_dir, 'error', 'Agent 启动失败', data={'error': str(exc)})
        _update_status(job_dir, status='failed', step='launch_failed', progress=100, error=str(exc), error_code='AGENT_LAUNCH_FAILED', completed_at=_now_iso())
        return 500, {'error': str(exc), 'error_code': 'AGENT_LAUNCH_FAILED', 'job_id': job_id}

    with _process_lock:
        _processes[job_id] = launch.process

    _event(job_dir, 'info', f'{adapter.name} 已启动', data={'command': launch.command})
    _update_status(job_dir, status='running', step='agent_running', progress=10, started_at=_now_iso(), pid=launch.process.pid)

    thread = threading.Thread(
        target=_monitor_job,
        args=(job_id, launch.process, DEFAULT_TIMEOUT_SEC),
        daemon=True,
    )
    thread.start()

    status = _read_json(_status_path(job_dir), base_status)
    return 200, status


def _tail_text(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
        return text[-limit:]
    except Exception:
        return ''


def _monitor_job(job_id: str, process, timeout_sec: int) -> None:
    job_dir = _job_dir(job_id)
    deadline = time.time() + timeout_sec
    try:
        while True:
            rc = process.poll()
            if rc is not None:
                break
            if time.time() > deadline:
                try:
                    process.terminate()
                    time.sleep(2)
                    if process.poll() is None:
                        process.kill()
                except Exception:
                    pass
                _event(job_dir, 'error', 'Agent 执行超时')
                _update_status(
                    job_dir,
                    status='failed',
                    step='timeout',
                    progress=100,
                    error='Agent 执行超过时间限制',
                    error_code='AGENT_TIMEOUT',
                    completed_at=_now_iso(),
                )
                return
            _update_status(job_dir, status='running', step='agent_running', progress=35)
            time.sleep(1)

        result = _read_json(job_dir / 'result.json', None)
        stderr_tail = _tail_text(job_dir / 'stderr.log')
        if process.returncode == 0 and isinstance(result, dict) and result.get('success'):
            _event(job_dir, 'done', 'Agent 任务完成')
            _update_status(
                job_dir,
                status='completed',
                step='completed',
                progress=100,
                result=result,
                completed_at=_now_iso(),
            )
        else:
            error = ''
            if isinstance(result, dict):
                error = result.get('error') or result.get('summary') or ''
            error = error or stderr_tail or f'Agent 退出码 {process.returncode}'
            _event(job_dir, 'error', 'Agent 任务失败', data={'error': error[-1000:]})
            _update_status(
                job_dir,
                status='failed',
                step='failed',
                progress=100,
                result=result if isinstance(result, dict) else None,
                error=error[-2000:],
                error_code='AGENT_FAILED',
                completed_at=_now_iso(),
            )
    finally:
        with _process_lock:
            _processes.pop(job_id, None)


def get_agent_job(job_id: str) -> tuple[int, dict]:
    try:
        job_dir = _job_dir(job_id)
    except ValueError as exc:
        return 400, {'error': str(exc), 'error_code': str(exc)}
    status = _read_json(_status_path(job_dir), None)
    if not isinstance(status, dict):
        return 404, {'error': '任务不存在', 'error_code': 'JOB_NOT_FOUND'}
    result = _read_json(job_dir / 'result.json', None)
    if isinstance(result, dict):
        status['result'] = result
    task = _read_json(job_dir / 'task.json', None)
    if isinstance(task, dict):
        status['task_summary'] = {
            'agent': task.get('agent') or status.get('agent'),
            'company': task.get('company') or '',
            'role': task.get('role') or '',
            'language': task.get('language') or status.get('language'),
            'has_interview': bool(str(task.get('interview') or '').strip()),
            'has_selection_plan': isinstance(task.get('selection_plan'), dict),
            'has_feedback': bool(str(task.get('feedback') or '').strip()),
        }
    suggestions = _read_json(job_dir / 'memory_suggestions.json', None)
    if isinstance(suggestions, dict):
        status['memory_suggestions'] = suggestions
    return 200, status


def get_agent_job_log(job_id: str, *, since_seq: int = 0) -> tuple[int, dict]:
    try:
        job_dir = _job_dir(job_id)
    except ValueError as exc:
        return 400, {'error': str(exc), 'error_code': str(exc)}
    if not job_dir.exists():
        return 404, {'error': '任务不存在', 'error_code': 'JOB_NOT_FOUND'}
    events = []
    try:
        for line in _events_path(job_dir).read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if int(item.get('seq', 0)) > since_seq:
                events.append(item)
    except FileNotFoundError:
        events = []
    except Exception as exc:
        return 500, {'error': str(exc)}
    return 200, {
        'success': True,
        'job_id': job_id,
        'events': events[-MAX_EVENTS_RETURNED:],
        'next_seq': events[-1]['seq'] if events else since_seq,
        'stdout_tail': _tail_text(job_dir / 'stdout.log', 2000),
        'stderr_tail': _tail_text(job_dir / 'stderr.log', 2000),
    }


def cancel_agent_job(job_id: str) -> tuple[int, dict]:
    try:
        job_dir = _job_dir(job_id)
    except ValueError as exc:
        return 400, {'error': str(exc), 'error_code': str(exc)}
    status = _read_json(_status_path(job_dir), None)
    if not isinstance(status, dict):
        return 404, {'error': '任务不存在', 'error_code': 'JOB_NOT_FOUND'}
    with _process_lock:
        process = _processes.get(job_id)
    if process and process.poll() is None:
        try:
            process.terminate()
        except Exception:
            try:
                os.kill(process.pid, signal.SIGTERM)
            except Exception:
                pass
    _event(job_dir, 'info', '任务已取消')
    updated = _update_status(job_dir, status='cancelled', step='cancelled', progress=100, completed_at=_now_iso())
    return 200, updated


def apply_memory_suggestions(job_id: str) -> tuple[int, dict]:
    try:
        job_dir = _job_dir(job_id)
    except ValueError as exc:
        return 400, {'error': str(exc), 'error_code': str(exc)}
    status = _read_json(_status_path(job_dir), None)
    if not isinstance(status, dict):
        return 404, {'error': '任务不存在', 'error_code': 'JOB_NOT_FOUND'}
    suggestions = _read_json(job_dir / 'memory_suggestions.json', {'suggestions': []})
    person_id = status.get('person_id') or get_active_person_id()
    memory_dir = get_person_profile_path(person_id).parent / 'memory'
    memory_dir.mkdir(parents=True, exist_ok=True)
    target = memory_dir / 'agent_suggestions.jsonl'
    record = {
        'job_id': job_id,
        'applied_at': _now_iso(),
        'suggestions': suggestions.get('suggestions', []),
    }
    with target.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    _event(job_dir, 'info', '长期记忆建议已由用户确认写入')
    try:
        path_text = str(target.relative_to(PROJECT_ROOT))
    except ValueError:
        path_text = str(target)
    return 200, {'success': True, 'path': path_text, 'count': len(record['suggestions'])}
