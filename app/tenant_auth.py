"""Tenant authentication by API key.

A caller never chooses its tenant: it presents a key, and the tenant is whatever that
key belongs to. A claimed tenant (e.g. --tenant, an X-Tenant-Id header in an HTTP
front end) is only accepted if it matches; otherwise the request is refused. This is
what stops "header spoofing": a valid key for harbor plus "--tenant acme" gets nothing.
"""

from __future__ import annotations

import hashlib
import secrets

import psycopg

from app.db import tenant_tx

KEY_PREFIX = "cv_"


class AuthError(Exception):
    pass


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def issue_key(conn: psycopg.Connection, tenant_id: str, label: str | None = None) -> str:
    """Create a key for `tenant_id` (worker role). The key is returned once and only
    its hash is stored."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    with tenant_tx(conn, tenant_id):
        exists = conn.execute("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
        if not exists:
            raise AuthError(f"unknown tenant {tenant_id!r}")
        conn.execute("INSERT INTO api_keys (key_hash, tenant_id, label) VALUES (%s, %s, %s)",
                     (hash_key(key), tenant_id, label))
    return key


def authenticate(conn: psycopg.Connection, api_key: str | None,
                 claimed_tenant: str | None = None) -> str:
    """Return the tenant the key belongs to, or raise AuthError."""
    if not api_key or not api_key.startswith(KEY_PREFIX) or len(api_key) > 200:
        raise AuthError("missing or malformed API key (set CALLVAULT_API_KEY or --api-key)")
    with conn.transaction():
        conn.execute("SELECT set_config('app.api_key_hash', %s, true)", (hash_key(api_key),))
        row = conn.execute("SELECT tenant_id FROM api_keys").fetchone()
    if row is None:
        raise AuthError("invalid or revoked API key")
    tenant = row["tenant_id"] if isinstance(row, dict) else row[0]
    if claimed_tenant is not None and claimed_tenant != tenant:
        raise AuthError("the claimed tenant does not match the API key")
    return tenant
