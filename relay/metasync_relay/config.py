"""Relay configuration, from the environment only (DESIGN-METADATA.md
section 10: secrets and settings live in env, never in files or code).

Protocol v2: the relay has NO database access. The guest-list check
lives at the token exchange; the relay verifies access tokens offline
against the issuer (pool) public key and checks the token's node_id
against the presented node key locally. The only required setting is
therefore RELAY_POOL_PUBLIC_KEY — without it no token can be verified,
so the relay refuses to start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .protocol import DEFAULT_ANSWER_CAP_BYTES
from .tokens import parse_pool_public_key


class ConfigError(Exception):
    """A required setting is missing or malformed. The message names the
    variable, never its value."""


@dataclass(frozen=True)
class RelayConfig:
    host: str = "127.0.0.1"
    port: int = 8791
    # This relay's canonical origin (section 10 hello binding), e.g.
    # "wss://relay.example:8791". REQUIRED for protocol v2: without
    # it the identity binding would be derived from client-supplied
    # Host / X-Forwarded-Proto headers, which is the impersonation
    # hole the binding exists to close — so a relay with no origin
    # configured refuses v2 hellos and serves v1 only (gate M5).
    relay_origin: str = ""
    issuer_id: str = ""
    revocations_url: str = ""
    revocations_interval_seconds: float = 300.0
    # Answer admission, byte-based (section 10 budgets).
    answer_cap_bytes: int = DEFAULT_ANSWER_CAP_BYTES  # total per query
    answer_node_query_cap_bytes: int = 2 * 1024 * 1024  # per node per query
    answer_bytes_per_hour: int = 128 * 1024 * 1024  # per node, rolling hour
    queries_per_hour: int = 600
    window_seconds: float = 6.0
    grace_seconds: float = 10.0
    send_timeout_seconds: float = 5.0
    send_queue_messages: int = 128
    # Byte bound on a connection's send queue, alongside the message
    # count: 128 queued maximum-size frames would pin ~1GB behind one
    # slow asker, so buffered bytes are capped too.
    send_queue_bytes: int = 16 * 1024 * 1024
    max_violations: int = 10
    violation_decay_seconds: float = 3600.0
    reauth_grace_seconds: float = 30.0
    # In-band re-auth rate limit, per connection: a healthy client
    # re-auths about once per token lifetime (24h). Anything near
    # this ceiling is a spinning or hostile client (gate M4: an
    # unfixed client re-sending its cached token re-auths once per
    # round trip, at two Ed25519 verifications per attempt here).
    reauth_max_per_window: int = 20
    reauth_window_seconds: float = 600.0
    counters_log_seconds: float = 60.0

    @classmethod
    def from_env(cls, env: dict | None = None) -> RelayConfig:
        env = os.environ if env is None else env

        def _int(name: str, default: int) -> int:
            raw = env.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                raise ConfigError(f"{name} must be an integer") from None

        def _float(name: str, default: float) -> float:
            raw = env.get(name)
            if raw is None or raw == "":
                return default
            try:
                return float(raw)
            except ValueError:
                raise ConfigError(f"{name} must be a number") from None

        return cls(
            host=env.get("RELAY_HOST") or "127.0.0.1",
            port=_int("RELAY_PORT", 8791),
            relay_origin=(env.get("RELAY_ORIGIN") or "").strip(),
            issuer_id=(env.get("RELAY_ISSUER_ID") or "").strip(),
            revocations_url=(env.get("RELAY_REVOCATIONS_URL") or "").strip(),
            revocations_interval_seconds=_float(
                "RELAY_REVOCATIONS_INTERVAL_SECONDS", 300.0
            ),
            answer_cap_bytes=_int("RELAY_ANSWER_CAP_BYTES", DEFAULT_ANSWER_CAP_BYTES),
            answer_node_query_cap_bytes=_int(
                "RELAY_ANSWER_NODE_QUERY_CAP_BYTES", 2 * 1024 * 1024
            ),
            answer_bytes_per_hour=_int(
                "RELAY_ANSWER_BYTES_PER_HOUR", 128 * 1024 * 1024
            ),
            queries_per_hour=_int("RELAY_QUERIES_PER_HOUR", 600),
            window_seconds=_float("RELAY_WINDOW_SECONDS", 6.0),
            grace_seconds=_float("RELAY_GRACE_SECONDS", 10.0),
            send_timeout_seconds=_float("RELAY_SEND_TIMEOUT_SECONDS", 5.0),
            send_queue_messages=_int("RELAY_SEND_QUEUE_MESSAGES", 128),
            send_queue_bytes=_int("RELAY_SEND_QUEUE_BYTES", 16 * 1024 * 1024),
            max_violations=_int("RELAY_MAX_VIOLATIONS", 10),
            violation_decay_seconds=_float("RELAY_VIOLATION_DECAY_SECONDS", 3600.0),
            reauth_grace_seconds=_float("RELAY_REAUTH_GRACE_SECONDS", 30.0),
            reauth_max_per_window=_int("RELAY_REAUTH_MAX_PER_WINDOW", 20),
            reauth_window_seconds=_float("RELAY_REAUTH_WINDOW_SECONDS", 600.0),
            counters_log_seconds=_float("RELAY_COUNTERS_LOG_SECONDS", 60.0),
        )


def require_pool_public_key(env: dict | None = None):
    """Parse RELAY_POOL_PUBLIC_KEY or raise ConfigError. Fail closed:
    without the pool key no token can be verified, so nothing may run."""
    env = os.environ if env is None else env
    raw = env.get("RELAY_POOL_PUBLIC_KEY")
    if not raw:
        raise ConfigError("RELAY_POOL_PUBLIC_KEY is not set")
    key = parse_pool_public_key(raw)
    if key is None:
        raise ConfigError("RELAY_POOL_PUBLIC_KEY is not a valid Ed25519 public key")
    return key


def issuer_public_key(env: dict | None = None, pool_public_key=None):
    """The revocation list's issuer key: RELAY_ISSUER_PUBLIC_KEY when
    set, otherwise the pool key (in v1 the pool IS the issuer: it signs
    tokens, attestations, and revocation lists with one key)."""
    env = os.environ if env is None else env
    raw = env.get("RELAY_ISSUER_PUBLIC_KEY")
    if raw:
        key = parse_pool_public_key(raw)
        if key is None:
            raise ConfigError(
                "RELAY_ISSUER_PUBLIC_KEY is not a valid Ed25519 public key"
            )
        return key
    return pool_public_key
