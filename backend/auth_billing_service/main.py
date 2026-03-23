from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from threading import Lock
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status

from backend.auth_billing_service.config import settings
from backend.auth_billing_service.schemas import ErrorResponse, HealthResponse, LoginRequest
from backend.auth_billing_service.services.auth_service import AuthService, InvalidTargetError, ThrottledError
from backend.auth_billing_service.services.entitlement_service import EntitlementError, EntitlementService
from backend.auth_billing_service.services.migration_service import (
    InMemoryOwnerRepository,
    MigrationBootstrapError,
    MigrationService,
)
from backend.auth_billing_service.services.payment_service import PaymentConflictError, PaymentError, PaymentService
from backend.auth_billing_service.services.session_service import SessionNotFoundError, SessionService
from backend.auth_billing_service.services.byok_service import ByokConfigurationError, ByokService, ByokValidationError

app = FastAPI(title=settings.app_name)
_auth_service = AuthService(redis_url=settings.redis_url)
_session_service = SessionService(max_active_sessions=3)
_migration_service = MigrationService(
    data_dir=Path(__file__).resolve().parents[2] / 'data',
    owner_repository=InMemoryOwnerRepository(),
)
_entitlement_service = EntitlementService()
_payment_service = PaymentService()
_byok_service = ByokService()
_OBSERVABILITY_WINDOW = timedelta(minutes=5)
_FINALIZE_SUCCESS_RATE_THRESHOLD = 0.99
_INVALID_SIGNATURE_ALERT_THRESHOLD = 5
_observability_lock = Lock()
_observability_state = {
    'reserve_total': 0,
    'finalize_total': 0,
    'finalize_success_total': 0,
    'dead_letter_count': 0,
    'finalize_events': deque(),
    'invalid_signature_events': deque(),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _prune_observability_events(now: datetime) -> None:
    cutoff = now - _OBSERVABILITY_WINDOW
    finalize_events = _observability_state['finalize_events']
    while finalize_events and finalize_events[0][0] < cutoff:
        finalize_events.popleft()
    invalid_signature_events = _observability_state['invalid_signature_events']
    while invalid_signature_events and invalid_signature_events[0] < cutoff:
        invalid_signature_events.popleft()


def _record_reserve_metric() -> None:
    with _observability_lock:
        _observability_state['reserve_total'] += 1


def _record_finalize_metric(result: str) -> None:
    normalized = str(result).strip().lower()
    now = _utcnow()
    with _observability_lock:
        _observability_state['finalize_total'] += 1
        if normalized == 'success':
            _observability_state['finalize_success_total'] += 1
        _observability_state['finalize_events'].append((now, normalized == 'success'))
        _prune_observability_events(now)


def _record_invalid_signature_metric() -> None:
    now = _utcnow()
    with _observability_lock:
        _observability_state['invalid_signature_events'].append(now)
        _prune_observability_events(now)


def record_dead_letter_metric(*, source: str, count: int = 1) -> None:
    increment = max(int(count), 0)
    if increment == 0:
        return
    with _observability_lock:
        _observability_state['dead_letter_count'] += increment


def _reset_observability_state() -> None:
    with _observability_lock:
        _observability_state['reserve_total'] = 0
        _observability_state['finalize_total'] = 0
        _observability_state['finalize_success_total'] = 0
        _observability_state['dead_letter_count'] = 0
        _observability_state['finalize_events'].clear()
        _observability_state['invalid_signature_events'].clear()


def get_observability_snapshot_for_tests() -> dict:
    with _observability_lock:
        now = _utcnow()
        _prune_observability_events(now)
        finalize_events = list(_observability_state['finalize_events'])
        finalize_total_5m = len(finalize_events)
        finalize_success_5m = sum(1 for _, succeeded in finalize_events if succeeded)
        finalize_success_rate_5m = 1.0 if finalize_total_5m == 0 else finalize_success_5m / finalize_total_5m
        invalid_signature_count_5m = len(_observability_state['invalid_signature_events'])
        dead_letter_count = _observability_state['dead_letter_count']
        alerts: list[str] = []
        if finalize_total_5m > 0 and finalize_success_rate_5m < _FINALIZE_SUCCESS_RATE_THRESHOLD:
            alerts.append('finalize_success_rate_below_threshold')
        if dead_letter_count > 0:
            alerts.append('dead_letter_detected')
        if invalid_signature_count_5m > _INVALID_SIGNATURE_ALERT_THRESHOLD:
            alerts.append('invalid_signature_threshold_exceeded')
        return {
            'metrics': {
                'reserve_total': _observability_state['reserve_total'],
                'finalize_total': _observability_state['finalize_total'],
                'finalize_success_total': _observability_state['finalize_success_total'],
                'finalize_success_rate_5m': finalize_success_rate_5m,
                'dead_letter_count': dead_letter_count,
                'invalid_signature_count_5m': invalid_signature_count_5m,
            },
            'alerts': alerts,
        }


def reset_runtime_state_for_tests() -> None:
    _auth_service.reset()
    _session_service.reset()
    _migration_service.reset()
    _entitlement_service.reset()
    _payment_service.reset()
    _byok_service.reset()
    _reset_observability_state()


def _service_secret() -> str:
    secret = os.getenv('AUTH_BILLING_SERVICE_SECRET', '').strip()
    if not secret:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='service secret not configured')
    return secret


def _verify_service_signature(request: Request, *, action: str, payload: dict) -> tuple[str, str]:
    user_id = request.headers.get('X-Auth-User-Id', '').strip()
    request_id = request.headers.get('X-Service-Request-Id', '').strip() or str(payload.get('request_id', '')).strip()
    reservation_id = request.headers.get('X-Service-Reservation-Id', '').strip()
    idempotency_key = request.headers.get('X-Service-Idempotency-Key', '').strip()
    result = request.headers.get('X-Service-Result', '').strip()
    timestamp = request.headers.get('X-Service-Timestamp', '').strip()
    signature = request.headers.get('X-Service-Signature', '').strip()

    if not user_id or not request_id or not timestamp or not signature:
        _record_invalid_signature_metric()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='missing service signature')

    try:
        ts = int(timestamp)
    except ValueError as exc:
        _record_invalid_signature_metric()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid service signature') from exc
    if abs(int(time.time()) - ts) > 300:
        _record_invalid_signature_metric()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='expired service signature')

    if action == 'reserve':
        payload_request_id = str(payload.get('request_id', '')).strip()
        if payload_request_id and payload_request_id != request_id:
            _record_invalid_signature_metric()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid service signature')
    elif action == 'finalize':
        payload_reservation_id = str(payload.get('reservation_id', '')).strip()
        payload_idempotency_key = str(payload.get('idempotency_key', '')).strip()
        payload_result = str(payload.get('result', '')).strip()
        if (
            not payload_reservation_id
            or not payload_idempotency_key
            or not payload_result
            or payload_reservation_id != reservation_id
            or payload_idempotency_key != idempotency_key
            or payload_result != result
        ):
            _record_invalid_signature_metric()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid service signature')

    message = f'{action}|{user_id}|{request_id}|{reservation_id}|{idempotency_key}|{result}|{timestamp}'
    expected = hmac.new(_service_secret().encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        _record_invalid_signature_metric()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid service signature')

    return user_id, request_id


def _raise_byok_invalid(detail: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={'error_code': 'BYOK_INVALID', 'detail': detail},
    )


def _auth_billing_shared_secret() -> str:
    return os.getenv('AUTH_BILLING_SERVICE_SECRET', '').strip()


def _extract_auth_context(request: Request) -> tuple[str | None, str | None]:
    validated = request.headers.get('X-Auth-Validated', '').strip().lower()
    user_id = request.headers.get('X-Auth-User-Id', '').strip()
    if validated not in {'1', 'true', 'yes'} or not user_id:
        return None, 'AUTH_REQUIRED'

    timestamp = request.headers.get('X-Auth-Timestamp', '').strip()
    signature = request.headers.get('X-Auth-Signature', '').strip()
    secret = _auth_billing_shared_secret()
    if not secret:
        return None, 'AUTH_BILLING_MISCONFIGURED'
    if not timestamp or not signature:
        _record_invalid_signature_metric()
        return None, 'AUTH_INVALID_SIGNATURE'
    try:
        ts = int(timestamp)
    except ValueError:
        _record_invalid_signature_metric()
        return None, 'AUTH_INVALID_SIGNATURE'
    if abs(int(time.time()) - ts) > 300:
        _record_invalid_signature_metric()
        return None, 'AUTH_INVALID_SIGNATURE'

    message = f'auth|{user_id}|{timestamp}'
    expected = hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        _record_invalid_signature_metric()
        return None, 'AUTH_INVALID_SIGNATURE'
    return user_id, None


def _require_authenticated_user(request: Request) -> str:
    user_id, auth_error = _extract_auth_context(request)
    if auth_error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=auth_error)
    return str(user_id)


def _coerce_byok_request_error(exc: Exception) -> None:
    if isinstance(exc, ByokValidationError):
        _raise_byok_invalid(str(exc))
    if isinstance(exc, ByokConfigurationError):
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    raise exc


@app.on_event('startup')
def run_bootstrap_migrations() -> None:
    try:
        _migration_service.bootstrap_owner_bindings()
    except MigrationBootstrapError as exc:
        raise RuntimeError(f'failed to bootstrap owner bindings: {exc}') from exc


@app.get('/health', response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status='ok')


@app.post('/auth/send-code')
def auth_send_code(payload: dict):
    channel = payload.get('channel', '')
    target = payload.get('target', '')
    try:
        result = _auth_service.send_code(channel=channel, target=target)
    except InvalidTargetError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ThrottledError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc

    return {
        'sent': result.sent,
        'verification_backend': result.verification_backend,
    }


@app.post('/auth/login', responses={401: {'model': ErrorResponse}})
def auth_login(req: LoginRequest):
    try:
        valid = _auth_service.verify_login_code(channel=req.channel, target=req.target, code=req.code)
    except InvalidTargetError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid verification code')

    user = _auth_service.upsert_user(channel=req.channel, target=req.target)
    tokens = _session_service.issue_tokens(user_id=user.user_id)
    return {
        'access_token': tokens.access_token,
        'refresh_token': tokens.refresh_token,
        'expires_in': tokens.expires_in,
        'user': {'id': user.user_id, 'identities': user.identities},
    }


@app.post('/auth/refresh', responses={401: {'model': ErrorResponse}})
def auth_refresh(payload: dict):
    refresh_token = payload.get('refresh_token')
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='refresh_token is required')

    try:
        tokens = _session_service.rotate_refresh_token(refresh_token)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid refresh token') from exc

    return {
        'access_token': tokens.access_token,
        'refresh_token': tokens.refresh_token,
        'expires_in': tokens.expires_in,
    }


@app.post('/auth/logout')
def auth_logout(payload: dict):
    refresh_token = payload.get('refresh_token')
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='refresh_token is required')

    _session_service.revoke_by_refresh_token(refresh_token)
    return {'ok': True}




@app.post('/byok/upsert')
def byok_upsert(payload: dict, request: Request):
    user_id = _require_authenticated_user(request)
    provider = str(payload.get('provider', '')).strip().lower()
    api_key = str(payload.get('api_key', ''))
    try:
        view = _byok_service.upsert(user_id=user_id, provider=provider, api_key=api_key)
    except (ByokValidationError, ByokConfigurationError) as exc:
        _coerce_byok_request_error(exc)

    return {
        'user_id': view.user_id,
        'provider': view.provider,
        'has_active_key': view.has_active_key,
        'masked_key': view.masked_key,
        'fingerprint': view.fingerprint,
    }


@app.get('/byok/{provider}')
def byok_get(provider: str, request: Request):
    user_id = _require_authenticated_user(request)
    try:
        view = _byok_service.get(user_id=user_id, provider=provider)
    except (ByokValidationError, ByokConfigurationError) as exc:
        _coerce_byok_request_error(exc)

    return {
        'user_id': view.user_id,
        'provider': view.provider,
        'has_active_key': view.has_active_key,
        'masked_key': view.masked_key,
        'fingerprint': view.fingerprint,
    }


@app.delete('/byok/{provider}')
def byok_delete(provider: str, request: Request):
    user_id = _require_authenticated_user(request)
    try:
        deleted = _byok_service.delete(user_id=user_id, provider=provider)
        view = _byok_service.get(user_id=user_id, provider=provider)
    except (ByokValidationError, ByokConfigurationError) as exc:
        _coerce_byok_request_error(exc)

    return {
        'deleted': deleted,
        'user_id': view.user_id,
        'provider': view.provider,
        'has_active_key': view.has_active_key,
        'masked_key': view.masked_key,
        'fingerprint': view.fingerprint,
    }

@app.post('/entitlements/reserve')
def entitlement_reserve(payload: dict, request: Request):
    user_id, request_id = _verify_service_signature(request, action='reserve', payload=payload)
    mode = payload.get('mode', 'platform_key')
    person_id = payload.get('person_id')
    if not person_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='person_id is required')

    owner_id = _migration_service.get_owner_id(person_id)
    if owner_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='PERSON_NOT_AUTHORIZED')

    try:
        decision = _entitlement_service.reserve(user_id=user_id, mode=mode, request_id=request_id)
    except EntitlementError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    _record_reserve_metric()

    return {
        'allow': decision.allow,
        'reservation_id': decision.reservation_id,
        'remaining_after_reserve': decision.remaining_after_reserve,
        'reset_at': decision.reset_at,
        'error_code': decision.error_code,
    }


@app.post('/entitlements/finalize')
def entitlement_finalize(payload: dict, request: Request):
    _verify_service_signature(request, action='finalize', payload=payload)
    reservation_id = payload.get('reservation_id')
    result = payload.get('result')
    idempotency_key = payload.get('idempotency_key')
    if not reservation_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='reservation_id is required')
    if not result:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='result is required')
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='idempotency_key is required')

    try:
        decision = _entitlement_service.finalize(
            reservation_id=reservation_id,
            result=result,
            idempotency_key=idempotency_key,
        )
    except EntitlementError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    _record_finalize_metric(result=str(result))

    return {
        'finalized': decision.finalized,
        'consumed': decision.consumed,
        'released': decision.released,
        'remaining': decision.remaining,
    }


@app.post('/billing/create-order')
def billing_create_order(payload: dict):
    plan = str(payload.get('plan', '')).strip()
    channel = str(payload.get('channel', '')).strip()
    user_id = str(payload.get('user_id', 'anonymous')).strip() or 'anonymous'
    try:
        _payment_service.expire_orders()
        order = _payment_service.create_order(user_id=user_id, plan=plan, channel=channel)
    except PaymentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {
        'order_no': order.order_no,
        'plan': order.plan,
        'channel': order.channel,
        'amount_cents': order.amount_cents,
        'currency': order.currency,
        'expires_at': order.expires_at.isoformat() if order.expires_at else None,
    }


def _billing_webhook(channel: str, payload: dict, request: Request):
    signature = request.headers.get('X-Payment-Signature', '').strip()
    try:
        if not _payment_service.verify_webhook_signature(payload, signature):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid payment signature')
        _payment_service.expire_orders()
        order = _payment_service.process_webhook(channel=channel, payload=payload)
    except PaymentConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PaymentError as exc:
        if str(exc) == 'payment webhook secret not configured':
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    _entitlement_service.set_membership_active(user_id=order.user_id, active=_payment_service.is_member_active(order.user_id))

    return {
        'ok': True,
        'order_no': order.order_no,
        'status': order.status,
        'provider_trade_no': order.provider_trade_no,
    }


@app.post('/billing/webhook/wechat')
def billing_webhook_wechat(payload: dict, request: Request):
    return _billing_webhook('wechat', payload, request)


@app.post('/billing/webhook/alipay')
def billing_webhook_alipay(payload: dict, request: Request):
    return _billing_webhook('alipay', payload, request)
