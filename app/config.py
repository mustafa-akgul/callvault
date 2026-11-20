"""Configuration from environment variables. Secrets are only ever read from the env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _url(user: str, password: str, host: str, port: str, db: str) -> str:
    return f"postgresql://{quote(user)}:{quote(password)}@{host}:{port}/{quote(db)}"


@dataclass(frozen=True)
class Settings:
    host: str
    port: str
    database: str
    admin_user: str
    admin_password: str
    owner_password: str
    worker_password: str
    user_password: str
    manifest_path: str
    lease_seconds: float
    heartbeat_seconds: float
    max_attempts: int
    poll_seconds: float

    @property
    def admin_url(self) -> str:
        # Superuser; used only by `migrate` to create roles and the database.
        return _url(self.admin_user, self.admin_password, self.host, self.port, "postgres")

    @property
    def owner_url(self) -> str:
        return _url("app_owner", self.owner_password, self.host, self.port, self.database)

    @property
    def worker_url(self) -> str:
        return _url("app_worker", self.worker_password, self.host, self.port, self.database)

    @property
    def user_url(self) -> str:
        return _url("app_user", self.user_password, self.host, self.port, self.database)


def load() -> Settings:
    return Settings(
        host=_env("PGHOST", "localhost"),
        port=_env("PGPORT", "5432"),
        database=_env("PGDATABASE", "callvault"),
        admin_user=_env("POSTGRES_ADMIN_USER", "postgres"),
        admin_password=os.environ.get("POSTGRES_ADMIN_PASSWORD", ""),
        owner_password=_env("APP_OWNER_PASSWORD"),
        worker_password=_env("APP_WORKER_PASSWORD"),
        user_password=_env("APP_USER_PASSWORD"),
        manifest_path=_env("MANIFEST_PATH", "samples/manifest.json"),
        lease_seconds=float(_env("LEASE_SECONDS", "60")),
        heartbeat_seconds=float(_env("HEARTBEAT_SECONDS", "15")),
        max_attempts=int(_env("MAX_ATTEMPTS", "5")),
        poll_seconds=float(_env("POLL_SECONDS", "0.5")),
    )
