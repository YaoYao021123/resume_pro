from __future__ import annotations

from datetime import datetime, timedelta, timezone

RETRY_DELAYS_SECONDS = [60, 5 * 60, 15 * 60, 60 * 60, 6 * 60 * 60]


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_next_retry(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback
    return _as_utc(parsed)


def run_finalize_retry_once(*, jobs: list[dict], finalize_func, now: datetime | None = None) -> dict[str, int]:
    current = _as_utc(now or datetime.now(timezone.utc))
    processed = 0
    done = 0
    dead_letter = 0
    retried = 0

    for job in jobs:
        status = str(job.get('status', 'pending'))
        if status not in {'pending', 'retrying'}:
            continue
        next_retry_at = _parse_next_retry(job.get('next_retry_at'), current)
        if next_retry_at > current:
            continue

        processed += 1
        try:
            finalize_func(
                user_id=job.get('user_id', ''),
                request_id=job.get('request_id', ''),
                reservation_id=job.get('reservation_id', ''),
                result=job.get('result', ''),
                idempotency_key=job.get('idempotency_key', ''),
            )
            job['status'] = 'done'
            job['done_at'] = current.isoformat()
            job['last_error'] = ''
            done += 1
        except Exception as exc:
            retry_count = int(job.get('retry_count', 0)) + 1
            job['retry_count'] = retry_count
            job['last_error'] = str(exc)
            if retry_count >= len(RETRY_DELAYS_SECONDS):
                job['status'] = 'dead_letter'
                job['dead_letter_at'] = current.isoformat()
                dead_letter += 1
                from backend.auth_billing_service.main import record_dead_letter_metric

                record_dead_letter_metric(source='finalize_retry_worker')
                continue
            job['status'] = 'pending'
            job['next_retry_at'] = (current + timedelta(seconds=RETRY_DELAYS_SECONDS[retry_count - 1])).isoformat()
            retried += 1

    return {
        'processed': processed,
        'done': done,
        'retried': retried,
        'dead_letter': dead_letter,
    }
