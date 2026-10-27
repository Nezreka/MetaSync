"""Guest-list semantics: the mirror of SIN's door check over portal_keys."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta

import pytest
from conftest import NodeIdentity
from metasync_relay.keys import PostgresKeyStore, row_is_bound_and_valid
from metasync_relay.tokens import encode_base64url

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


@pytest.fixture()
def node() -> NodeIdentity:
    return NodeIdentity()


def _row(node, **overrides):
    row = {
        "bound_node_id": node.node_id,
        "bound_public_key": encode_base64url(node.public_raw),
        "expires_at": None,
        "revoked_at": None,
    }
    row.update(overrides)
    return (row["bound_node_id"], row["bound_public_key"], row["expires_at"], row["revoked_at"])


def test_bound_valid_row_passes(node):
    assert row_is_bound_and_valid(_row(node), node.node_id, node.public_raw, NOW)


def test_missing_row_fails(node):
    assert not row_is_bound_and_valid(None, node.node_id, node.public_raw, NOW)


def test_revoked_row_fails(node):
    row = _row(node, revoked_at=NOW - timedelta(days=1))
    assert not row_is_bound_and_valid(row, node.node_id, node.public_raw, NOW)


def test_expired_row_fails(node):
    row = _row(node, expires_at=NOW - timedelta(seconds=1))
    assert not row_is_bound_and_valid(row, node.node_id, node.public_raw, NOW)


def test_unexpired_row_passes(node):
    row = _row(node, expires_at=NOW + timedelta(days=1))
    assert row_is_bound_and_valid(row, node.node_id, node.public_raw, NOW)


def test_unbound_row_fails(node):
    row = _row(node, bound_node_id=None, bound_public_key=None)
    assert not row_is_bound_and_valid(row, node.node_id, node.public_raw, NOW)


def test_pending_only_row_fails(node):
    # A pending binding (requested, not yet approved) is not a binding.
    row = (None, None, None, None)
    assert not row_is_bound_and_valid(row, node.node_id, node.public_raw, NOW)


def test_bound_to_another_node_fails(node):
    other = NodeIdentity()
    row = _row(other)
    assert not row_is_bound_and_valid(row, node.node_id, node.public_raw, NOW)


def test_bound_node_with_wrong_presented_key_fails(node):
    other = NodeIdentity()
    assert not row_is_bound_and_valid(_row(node), node.node_id, other.public_raw, NOW)


def test_malformed_bound_public_key_fails(node):
    row = _row(node, bound_public_key="!!!not-base64!!!")
    assert not row_is_bound_and_valid(row, node.node_id, node.public_raw, NOW)


class _FakeCursor:
    def __init__(self, row, record):
        self._row = row
        self._record = record

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self._record.append((sql, params))

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, row, record):
        self._row = row
        self._record = record

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._row, self._record)


def _install_fake_psycopg(monkeypatch, row, record, raises=False):
    fake = types.ModuleType("psycopg")

    def connect(url, connect_timeout=None):
        if raises:
            raise RuntimeError("db down")
        record.append(("connect", url))
        return _FakeConnection(row, record)

    fake.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", fake)


def test_postgres_store_selects_status_columns_only(monkeypatch, node):
    record: list = []
    _install_fake_psycopg(monkeypatch, _row(node), record)
    store = PostgresKeyStore("postgres://example/db")
    assert store.is_bound_and_valid("key-1", node.node_id, node.public_raw)
    sql_calls = [entry for entry in record if entry[0].startswith("SELECT")]
    assert len(sql_calls) == 1
    sql, params = sql_calls[0]
    assert params == ("key-1",)
    # The peek must never learn whose key this is.
    assert "user_id" not in sql
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP"):
        assert verb not in sql.upper()


def test_postgres_store_fails_closed_on_db_error(monkeypatch, node):
    record: list = []
    _install_fake_psycopg(monkeypatch, None, record, raises=True)
    store = PostgresKeyStore("postgres://example/db")
    assert not store.is_bound_and_valid("key-1", node.node_id, node.public_raw)
