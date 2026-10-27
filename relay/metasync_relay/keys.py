"""Connect-time guest-list peek (DESIGN-METADATA.md section 10, Q4).

The relay's only database access: given a verified token's key_id and
node_id plus the node public key presented in hello, confirm that a
current portal_keys row exists, is valid, and is bound to that node.
The semantics mirror SIN's own door check (src/sin/index.ts): the row is
looked up by id, validity is the section 2.2 rule from migration 0003
(revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())), and
the binding must match on both bound_node_id and the bound public key
(migrations 0003/0004).

The SELECT names only the columns the yes/no needs. It never selects
the account column (user_id): the relay decides pass/fail, it does not
learn whose node this is. The store is read-only; there is no write
path anywhere in this package.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from .tokens import decode_base64url

log = logging.getLogger("metasync_relay.keys")

_SELECT_BOUND_KEY = (
    "SELECT bound_node_id, bound_node_public_key, expires_at, revoked_at "
    "FROM portal_keys WHERE id = %s LIMIT 1"
)


class KeyStore(Protocol):
    """The guest-list interface the relay depends on. Tests fake it."""

    def is_bound_and_valid(
        self, key_id: str, node_id: str, node_public_key: bytes
    ) -> bool:
        """True only when the key row exists, is currently valid, and is
        bound to exactly this node id and public key."""
        ...


def row_is_bound_and_valid(
    row: tuple | None,
    node_id: str,
    node_public_key: bytes,
    now: datetime | None = None,
) -> bool:
    """Pure decision over one portal_keys row shaped as
    (bound_node_id, bound_node_public_key, expires_at, revoked_at).
    Kept separate from the SQL so the semantics are unit-testable."""
    if row is None:
        return False
    if now is None:
        now = datetime.now(UTC)
    bound_node_id, bound_public_key, expires_at, revoked_at = row
    if revoked_at is not None:
        return False
    if expires_at is not None and expires_at <= now:
        return False
    if not bound_public_key or bound_node_id != node_id:
        return False
    raw = decode_base64url(bound_public_key, 32)
    return raw is not None and raw == node_public_key


class PostgresKeyStore:
    """KeyStore backed by the SIN/portal Postgres database (read-only).

    A fresh connection per check: hellos are connection-setup events,
    not hot path, and holding no pool keeps the relay's state story
    exactly "presence in memory, nothing else". psycopg is imported
    lazily so tests and tooling can import this module without the
    driver installed.
    """

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for the key store")
        self._database_url = database_url

    def is_bound_and_valid(
        self, key_id: str, node_id: str, node_public_key: bytes
    ) -> bool:
        import psycopg

        try:
            with psycopg.connect(
                self._database_url, connect_timeout=5
            ) as conn, conn.cursor() as cur:
                cur.execute(_SELECT_BOUND_KEY, (key_id,))
                row = cur.fetchone()
        except Exception:  # noqa: BLE001 - fail closed on ANY db failure
            # A database blip refuses new connections but never lets
            # anyone in. The class is logged, never the key.
            log.warning("key_store_error", extra={"error_class": "db"})
            return False
        return row_is_bound_and_valid(row, node_id, node_public_key)
