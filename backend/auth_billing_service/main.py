from __future__ import annotations

import hashlib
import hmac
import os
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
from backend.auth_billing_service.services.session_service import SessionNotFoundError, SessionService

app = FastAPI(title=settings.app_name)
_auth_service = AuthService(redis_url=settings.redis_url)
_session_service = SessionService(max_active_sessions=3)
_migration_service = MigrationService(
    data_dir=Path(__file__).resolve().parents[2] / 'data',
    owner_repository=InMemoryOwnerRepository(),
)
_entitlement_service = EntitlementService()


def reset_runtime_state_for_tests() -> None:
    _auth_service.reset()
    _session_service.reset()
    _migration_service.reset()
    _entitlement_service.reset()


def _service_secret() -> str:
    return os.getenv('AUTH_BILLING_SERVICE_SECRET', 'dev-secret')


def _verify_service_signature(request: Request, *, action: str, payload: dict) -> tuple[str, str]:
    user_id = request.headers.get('X-Auth-User-Id', '').strip()
    request_id = request.headers.get('X-Service-Request-Id', '').strip() or str(payload.get('request_id', '')).strip()
    reservation_id = request.headers.get('X-Service-Reservation-Id', '').strip()
    idempotency_key = request.headers.get('X-Service-Idempotency-Key', '').strip()
    result = request.headers.get('X-Service-Result', '').strip()
    timestamp = request.headers.get('X-Service-Timestamp', '').strip()
    signature = request.headers.get('X-Service-Signature', '').strip()

    if not user_id or not request_id or not timestamp or not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='missing service signature')

    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid service signature') from exc
    if abs(int(time.time()) - ts) > 300:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='expired service signature')

    message = f'{action}|{user_id}|{request_id}|{reservation_id}|{idempotency_key}|{result}|{timestamp}'
    expected = hmac.new(_service_secret().encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid service signature')

    return user_id, request_id


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

    return {
        'finalized': decision.finalized,
        'consumed': decision.consumed,
        'released': decision.released,
        'remaining': decision.remaining,
    }
