"""Bootstrap roles + database (as superuser), then apply SQL migrations as app_owner.

Role layout:
  app_owner   owns the schema; runs migrations. Still subject to RLS (FORCE).
  app_worker  ingest / worker / reprocess. Read+write, no DELETE.
  app_user    API / read side. Read-only, no access to the job queue.
None of them is SUPERUSER or BYPASSRLS. Idempotent: safe to run on every start.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg
from psycopg import sql

from app.config import Settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

ROLES = ("app_owner", "app_worker", "app_user")


def bootstrap(settings: Settings) -> None:
    passwords = {
        "app_owner": settings.owner_password,
        "app_worker": settings.worker_password,
        "app_user": settings.user_password,
    }
    with psycopg.connect(settings.admin_url, autocommit=True) as conn:
        for role in ROLES:
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(
                sql.SQL(
                    "{} ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.SQL(verb), sql.Identifier(role), sql.Literal(passwords[role]))
            )
        db_exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (settings.database,)
        ).fetchone()
        if not db_exists:
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER app_owner").format(
                    sql.Identifier(settings.database)
                )
            )
    # Inside the database: lock down the public schema.
    admin_db_url = settings.admin_url.rsplit("/", 1)[0] + "/" + settings.database
    with psycopg.connect(admin_db_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(settings.database)
            )
        )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("ALTER SCHEMA public OWNER TO app_owner")
        for role in ROLES:
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(settings.database), sql.Identifier(role)
                )
            )
        conn.execute("GRANT USAGE ON SCHEMA public TO app_worker, app_user")


def apply_migrations(settings: Settings) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(settings.owner_url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        # Serialise concurrent `migrate` runs.
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('callvault.migrate'))")
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                log.info("applying migration %s", path.name)
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
    return applied


def migrate(settings: Settings) -> list[str]:
    bootstrap(settings)
    return apply_migrations(settings)
