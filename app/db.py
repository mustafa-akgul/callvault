"""Database access helpers.

Connections are autocommit, so every unit of work is an explicit `transaction()` block.
That matters for tenant isolation: `SET LOCAL` (here `set_config(..., true)`) lives
exactly as long as the transaction that set it, so a tenant context can never stay on a
connection and leak into the next unit of work.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def set_tenant(conn: psycopg.Connection, tenant_id: str) -> None:
    """Set the tenant for the CURRENT transaction only. Must be called inside one."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("set_tenant() outside a transaction would be a no-op")
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))


@contextmanager
def tenant_tx(conn: psycopg.Connection, tenant_id: str) -> Iterator[psycopg.Connection]:
    """The one way application code reads or writes tenant data."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        # A nested call would turn this into a savepoint and inherit the outer context.
        raise RuntimeError("tenant_tx() must not be nested in another transaction")
    with conn.transaction():
        set_tenant(conn, tenant_id)
        yield conn
