"""Relay configuration, from the environment only (DESIGN-METADATA.md
section 10: secrets and settings live in env, never in files or code).

The relay refuses to start without RELAY_POOL_PUBLIC_KEY and
DATABASE_URL: both are load-bearing for the connect-time door, and a
relay that cannot check the guest list must not open the room.
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
    answer_cap_bytes: int = DEFAULT_ANSWER_CAP_BYTES
    queries_per_hour: int = 600
    answers_per_hour: int = 2000
    window_seconds: float = 6.0
    grace_seconds: float = 10.0
    send_timeout_seconds: float = 5.0
    max_violations: int = 10

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
            answer_cap_bytes=_int("RELAY_ANSWER_CAP_BYTES", DEFAULT_ANSWER_CAP_BYTES),
            queries_per_hour=_int("RELAY_QUERIES_PER_HOUR", 600),
            answers_per_hour=_int("RELAY_ANSWERS_PER_HOUR", 2000),
            window_seconds=_float("RELAY_WINDOW_SECONDS", 6.0),
            grace_seconds=_float("RELAY_GRACE_SECONDS", 10.0),
            send_timeout_seconds=_float("RELAY_SEND_TIMEOUT_SECONDS", 5.0),
            max_violations=_int("RELAY_MAX_VIOLATIONS", 10),
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


def require_database_url(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    url = env.get("DATABASE_URL")
    if not url:
        raise ConfigError("DATABASE_URL is not set")
    return url
