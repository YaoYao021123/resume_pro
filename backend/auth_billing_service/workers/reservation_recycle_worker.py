from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.auth_billing_service.services.entitlement_service import EntitlementService

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


def _has_success_pending_finalize(pending_finalize_jobs: list[dict], reservation_id: str) -> bool:
    for job in pending_finalize_jobs:
        if (
            str(job.get('reservation_id', '')).strip() == reservation_id
            and str(job.get('result', '')).strip() == 'success'
            and str(job.get('status', 'pending')).strip() != 'done'
        ):
            return True
    return False


def run_reservation_recycle_once(
    *,
    entitlement_service: EntitlementService,
    pending_finalize_jobs: list[dict],
    recycle_jobs: list[dict],
    now: datetime | None = None,
    release_func=None,
) -> dict[str, int]:
    current = _as_utc(now or datetime.now(timezone.utc))
    release = release_func or (lambda *, reservation_id: entitlement_service.release_reservation(reservation_id))

    recycled = 0
    queued = 0
    retried = 0
    dead_letter = 0

    for reservation in entitlement_service.list_expired_reservations(now=current):
        if _has_success_pending_finalize(pending_finalize_jobs, reservation.reservation_id):
            continue
        if entitlement_service.has_success_finalize_event(reservation.reservation_id):
            continue

        existing_job = None
        has_dead_letter = False
        for job in recycle_jobs:
            if str(job.get('reservation_id', '')).strip() != reservation.reservation_id:
                continue
            job_status = str(job.get('status', 'pending')).strip()
            if job_status == 'dead_letter':
                has_dead_letter = True
                break
            if job_status in {'pending', 'retrying'}:
                existing_job = job
                break

        if has_dead_letter:
            continue

        if existing_job is None:
            existing_job = {
                'reservation_id': reservation.reservation_id,
                'status': 'pending',
                'retry_count': 0,
                'next_retry_at': current.isoformat(),
                'last_error': '',
                'created_at': current.isoformat(),
            }
            recycle_jobs.append(existing_job)
            queued += 1

        next_retry_at = _parse_next_retry(existing_job.get('next_retry_at'), current)
        if next_retry_at > current:
            continue

        try:
            changed = bool(release(reservation_id=reservation.reservation_id))
            existing_job['status'] = 'done'
            existing_job['done_at'] = current.isoformat()
            existing_job['last_error'] = ''
            if changed:
                recycled += 1
        except Exception as exc:
            retry_count = int(existing_job.get('retry_count', 0)) + 1
            existing_job['retry_count'] = retry_count
            existing_job['last_error'] = str(exc)
            if retry_count >= len(RETRY_DELAYS_SECONDS):
                existing_job['status'] = 'dead_letter'
                existing_job['dead_letter_at'] = current.isoformat()
                dead_letter += 1
                from backend.auth_billing_service.main import record_dead_letter_metric

                record_dead_letter_metric(source='reservation_recycle_worker')
            else:
                existing_job['status'] = 'pending'
                existing_job['next_retry_at'] = (current + timedelta(seconds=RETRY_DELAYS_SECONDS[retry_count - 1])).isoformat()
                retried += 1

    return {
        'recycled': recycled,
        'queued': queued,
        'retried': retried,
        'dead_letter': dead_letter,
    }
