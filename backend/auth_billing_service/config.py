from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = 'auth-billing-service'
    app_env: str = 'dev'
    app_host: str = '0.0.0.0'
    app_port: int = 8080
    database_url: str = 'sqlite+pysqlite:///:memory:'
    redis_url: str = 'redis://localhost:6379/0'



def load_settings() -> Settings:
    return Settings(
        app_name=os.getenv('AUTH_BILLING_APP_NAME', Settings.app_name),
        app_env=os.getenv('AUTH_BILLING_APP_ENV', Settings.app_env),
        app_host=os.getenv('AUTH_BILLING_APP_HOST', Settings.app_host),
        app_port=int(os.getenv('AUTH_BILLING_APP_PORT', str(Settings.app_port))),
        database_url=os.getenv('AUTH_BILLING_DATABASE_URL', Settings.database_url),
        redis_url=os.getenv('AUTH_BILLING_REDIS_URL', Settings.redis_url),
    )


settings = load_settings()
