from __future__ import annotations

from fastapi import FastAPI, HTTPException, status

from backend.auth_billing_service.config import settings
from backend.auth_billing_service.schemas import ErrorResponse, HealthResponse, LoginRequest
from backend.auth_billing_service.services.auth_service import AuthService, InvalidTargetError, ThrottledError
from backend.auth_billing_service.services.session_service import SessionNotFoundError, SessionService

app = FastAPI(title=settings.app_name)
_auth_service = AuthService(redis_url=settings.redis_url)
_session_service = SessionService(max_active_sessions=3)


def reset_runtime_state_for_tests() -> None:
    _auth_service.reset()
    _session_service.reset()


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
