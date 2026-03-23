from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


class LoginRequest(BaseModel):
    channel: str
    target: str
    code: str


class ErrorResponse(BaseModel):
    detail: str
