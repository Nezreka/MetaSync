"""Node persistence: two small JSON documents in the node's own state dir.

- settings.json: the two CONNECTIONS, kept distinct by design —
    soulsync: {url, api_key}          (step A: connect to YOUR SoulSync)
    pools:    [{id, name, url, api_key, role, enabled}]
                                       (step B: join a pool; SIN is one pool)
  plus dry_run and the UI admin password hash.
- state.json: runtime state — last report per pool, cached pool index,
  canonical playlists cache, the SoulSync playlist ids this node manages,
  MusicBrainz caches, recent errors.

Both files are written 0600 (they contain API keys). SQLite stays the
sketch's long-term option; versioned JSON is enough for the MVP.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class Store:
    def __init__(self, state_dir: str | Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.settings_path = self.dir / "settings.json"
        self.state_path = self.dir / "state.json"
        self.settings: dict[str, Any] = {
            "soulsync": {"url": "", "api_key": ""},
            "pools": [],
            "dry_run": True,
            "ui_password_hash": None,
            # MetaSync's own local API key (SoulSync -> MetaSync). Only a
            # SHA-256 hash is stored; the raw key is shown exactly once.
            "local_api_key_hash": None,
            "local_api_key_prefix": None,
            "local_api_key_created_at": None,
            **_read_json(self.settings_path),
        }
        self.state: dict[str, Any] = {
            "last_report": {},       # pool_id -> {week, at, signal_count}
            "last_preview": None,    # last computed report payload (per first pool)
            "previews": {},          # pool_id -> last computed payload
            "last_refresh_at": None,
            "index_cache": {},       # pool_id -> {fetched_at, week, trending, most_wanted, breakouts}
            "playlists_cache": {},   # pool_id -> {fetched_at, week, playlists}
            "managed_playlists": {},  # pool_id -> {key -> {playlist_id, name, content_hash, track_count, last_sync_at, last_error}}
            "built_definitions": {},  # pool_id -> canonical playlists built in admin mode
            "release_to_group": {},  # MusicBrainz release mbid -> release-group mbid
            "mb_cache": {},          # MusicBrainz URL path -> JSON
            "pool_auth": {},         # pool_id -> exchange/token/binding status (token is state, 0600)
            "pairing_session": None, # the single local one-click pairing session, if any
            "pairing_attempts": [],  # timestamps used to rate-limit pairing requests
            "last_pairing": None,    # non-secret summary of the last completed pairing
            "last_errors": [],       # [{at, leg, message}] newest first, capped
            **_read_json(self.state_path),
        }

    def save_settings(self) -> None:
        with self._lock:
            _write_json(self.settings_path, self.settings)

    def save_state(self) -> None:
        with self._lock:
            _write_json(self.state_path, self.state)

    def record_error(self, leg: str, err: BaseException | str) -> None:
        from datetime import datetime, timezone

        message = str(err)
        entry = {"at": datetime.now(timezone.utc).isoformat(), "leg": leg, "message": message}
        self.state["last_errors"] = [entry, *self.state.get("last_errors", [])][:20]

    # -- UI secret (Flask session signing), generated once -----------------
    def ui_secret(self) -> str:
        file = self.dir / "ui_secret"
        if file.exists():
            return file.read_text(encoding="utf-8").strip()
        secret = secrets.token_hex(32)
        file.write_text(secret + "\n", encoding="utf-8")
        os.chmod(file, 0o600)
        return secret


def default_pool_from_env(env: dict[str, str]) -> dict[str, Any] | None:
    """Optional bootstrap: seed the SIN pool from env (role always node)."""
    url = env.get("SIN_URL", "").rstrip("/")
    key = env.get("SIN_INGEST_KEY", "")
    if not url:
        return None
    return {
        "id": "sin",
        "name": "SIN",
        "url": url,
        "api_key": key,
        "role": "node",
        "enabled": True,
    }
