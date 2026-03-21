from __future__ import annotations

from fastapi import FastAPI, HTTPException, status

from backend.auth_billing_service.config import settings
from backend.auth_billing_service.schemas import ErrorResponse, HealthResponse, LoginRequest

app = FastAPI(title=settings.app_name)


@app.get('/health', response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status='ok')


@app.post('/auth/login', response_model=None, responses={401: {'model': ErrorResponse}})
def auth_login_placeholder(_: LoginRequest):
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail='auth routes are not ready yet',
    )
