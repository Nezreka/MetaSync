"""Node service: pull -> push -> provide -> update, role-aware.

Connections come from the settings store (edited in the UI):
- settings["soulsync"]: {url, api_key} — step A. Alone, it joins nothing.
- settings["pools"]: [{id, name, url, api_key, role, enabled}] — step B.

Failure isolation is structural: report and refresh are separate legs,
each pool is its own leg, each playlist sync is its own leg. Errors are
recorded in state and returned; nothing here takes the process (or
SoulSync) down.
"""
from __future__ import annotations

import hashlib
import re
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from .config import Config
from .identity import NodeIdentity, load_or_create_identity, token_for_week
from .meta.builder import BuildStopped, LibraryBuilder
from .meta.responder import Responder
from .meta.store import DB_FILENAME as META_DB_FILENAME
from .meta.store import MetaStore
from .musicbrainz import MusicBrainzResolver
from .playlists import build_canonical_playlists
from .pool import PoolBindingPending, PoolClient, PoolError, PoolNotAvailable, is_loopback_url
from .relay import RelayClient, RelayError, normalize_mode
from .signals import KEEP_AFTER, LATE_WINDOW, compute_signals, is_uuid_shape
from .soulsync import SoulSyncClient, SoulSyncError
from .store import Store, default_pool_from_env
from .weekutil import closed_week_start, iso_week_string

# How many complete weekly library snapshots the churn rule keeps.
_SNAPSHOT_WEEKS = 3

# A claims-store build is due again this long after the last
# completed one (the export walk is incremental; this cadence is
# what bounds how stale the node's answers can get).
_META_REBUILD_INTERVAL_H = 20


def _env_flag(raw: str) -> bool:
    return raw.lower() not in ("0", "false", "no", "off", "")


def check_pool_url(url: str, *, allow_insecure: bool = False) -> None:
    """A pool key is presented to this URL, so it must be HTTPS. Plain HTTP
    is allowed only on loopback (a development pool on this machine) or
    when the operator set METASYNC_ALLOW_INSECURE_POOLS. Raises ValueError."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("pool_url must be an http(s) URL")
    if parsed.scheme == "http" and not allow_insecure and not is_loopback_url(url):
        raise ValueError(
            "pool_url must use HTTPS: the pool key would otherwise cross the network in the clear"
        )


class PairingError(Exception):
    """A pairing request cannot proceed; details are intentionally generic."""


class PairingRateLimited(PairingError):
    pass


class NodeService:
    def __init__(self, config: Config, *, store: Store | None = None, identity: NodeIdentity | None = None,
                 soulsync: SoulSyncClient | None = None, resolver: MusicBrainzResolver | None = None,
                 pool_clients: dict[str, PoolClient] | None = None, env: dict[str, str] | None = None):
        self.config = config
        self.store = store or Store(config.state_dir)
        self.identity = identity or load_or_create_identity(config.state_dir)
        self._soulsync_override = soulsync
        self._pool_overrides = pool_clients or {}
        # Bootstrap defaults from env on a truly fresh settings file.
        settings = self.store.settings
        changed = False
        if env:
            ss = settings.get("soulsync") or {}
            if not ss.get("url") and env.get("SOULSYNC_URL"):
                settings["soulsync"] = {"url": env["SOULSYNC_URL"].rstrip("/"), "api_key": env.get("SOULSYNC_API_KEY", "")}
                changed = True
            if not settings.get("pools"):
                seeded = default_pool_from_env(env)
                if seeded:
                    settings["pools"] = [seeded]
                    changed = True
            if "METASYNC_DRY_RUN" in env or "DRY_RUN" in env:
                raw = env.get("METASYNC_DRY_RUN", env.get("DRY_RUN", ""))
                settings["dry_run"] = raw.lower() not in ("0", "false", "no", "off")
                changed = True
            if "METASYNC_SHARE_CHURN" in env:
                settings["share_churn"] = _env_flag(env["METASYNC_SHARE_CHURN"])
                changed = True
            if "METASYNC_SHARE_LISTENING" in env:
                settings["share_listening"] = _env_flag(env["METASYNC_SHARE_LISTENING"])
                changed = True
            if "METASYNC_META_ANSWERING" in env:
                settings["meta_answering"] = _env_flag(env["METASYNC_META_ANSWERING"])
                changed = True
        if changed:
            self.store.save_settings()
        self.allow_insecure_pools = bool(env and _env_flag(env.get("METASYNC_ALLOW_INSECURE_POOLS", "")))
        # The release -> group lookups used to cache their whole MusicBrainz
        # response here as well as the answer in release_to_group: for a
        # large library that was hundreds of MB rewritten on every state
        # save. Only the answer is kept now; drop the old raw copies.
        mb_cache = self.store.state.setdefault("mb_cache", {})
        stale = [k for k in mb_cache if k.startswith("/release/") and "inc=release-groups" in k]
        for key in stale:
            del mb_cache[key]
        if stale:
            self.store.save_state()
        self.resolver = resolver or MusicBrainzResolver(
            config.musicbrainz_base_url,
            config.musicbrainz_user_agent,
            min_interval_s=config.musicbrainz_min_interval_s,
            cache=mb_cache,
        )
        # The clock the background job and the scheduler read; tests pin it.
        self.clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self._report_lock = threading.Lock()
        # One report run at a time, whoever starts it (UI job or scheduler):
        # two concurrent runs would race the churn snapshots and state.
        self._run_lock = threading.Lock()
        # The metadata-network relay client (DESIGN-METADATA.md section
        # 10), created lazily by sync_relay_client when the user turns
        # the network on. None means "never started on this process".
        self._relay_client: RelayClient | None = None
        self._relay_lock = threading.Lock()
        # The metadata claims store + responder (DESIGN-METADATA.md
        # section 5), created lazily: a node that never turns the
        # metadata network on never opens the store file.
        self._meta_store: MetaStore | None = None
        self._meta_responder: Responder | None = None
        # Lazy construction of the store/responder happens under
        # this lock (a scheduler tick and a UI request must not race
        # two stores into being). It is NOT _meta_lock: that one is
        # held across whole builds, which themselves construct.
        self._meta_construct_lock = threading.RLock()
        self._meta_lock = threading.Lock()
        self._meta_build_thread: threading.Thread | None = None
        # A job record persisted as "running" across a restart is a corpse:
        # its thread died with the old process. Close it out honestly.
        stale_job = self.store.state.get("report_job") or {}
        if stale_job.get("status") == "running":
            stale_job["status"] = "error"
            stale_job["phase"] = "failed"
            stale_job["error"] = "The node restarted while the report was running. Run it again."
            stale_job["detail"] = stale_job["error"]
            stale_job["finished_at"] = _now()
            self.store.state["report_job"] = stale_job
            self.store.save_state()

    # -- connections --------------------------------------------------------
    @property
    def dry_run(self) -> bool:
        return bool(self.store.settings.get("dry_run", True))

    def soulsync(self) -> SoulSyncClient:
        if self._soulsync_override is not None:
            return self._soulsync_override
        ss = self.store.settings.get("soulsync") or {}
        if not ss.get("url"):
            raise SoulSyncError("SoulSync is not connected yet (set its URL + API key in Settings)", code="NOT_CONFIGURED")
        return SoulSyncClient(ss["url"], ss.get("api_key", ""), timeout_s=self.config.http_timeout_s)

    def pools(self) -> list[dict[str, Any]]:
        return [p for p in self.store.settings.get("pools") or [] if p.get("enabled", True)]

    def pool_client(self, pool: dict[str, Any]) -> PoolClient:
        if pool["id"] in self._pool_overrides:
            return self._pool_overrides[pool["id"]]
        auth_by_pool = self.store.state.setdefault("pool_auth", {})
        auth_state = auth_by_pool.setdefault(str(pool.get("id") or "pool"), {})
        return PoolClient(
            pool,
            timeout_s=self.config.http_timeout_s,
            identity=self.identity,
            auth_state=auth_state,
            persist=self.store.save_state,
            allow_legacy=self.allow_insecure_pools,
        )

    def test_soulsync(self) -> dict[str, Any]:
        return self.soulsync().test_connection()

    # -- pool connection status / exchange ----------------------------------
    def pool_auth_status(self, pool_id: str) -> dict[str, Any]:
        auth = dict((self.store.state.get("pool_auth") or {}).get(pool_id) or {})
        access_token = auth.pop("access_token", None)
        auth["has_access_token"] = bool(access_token)
        return auth

    def pool_public_status(self, pool: dict[str, Any]) -> dict[str, Any]:
        auth = self.pool_auth_status(str(pool.get("id") or ""))
        raw_key = str(pool.get("api_key") or "")
        return {
            "id": pool.get("id"),
            "name": pool.get("name"),
            "url": pool.get("url"),
            "role": pool.get("role") or "node",
            "enabled": bool(pool.get("enabled", True)),
            "key_prefix": auth.get("key_prefix") or (raw_key[:12] if raw_key else None),
            "has_pool_key": bool(raw_key),
            "auth_mode": auth.get("mode"),
            "binding_status": auth.get("binding_status") or ("not_checked" if raw_key else "not_connected"),
            "pending_node_fingerprint": auth.get("pending_node_fingerprint"),
            "token_expires_at": auth.get("token_expires_at"),
            "has_access_token": bool(auth.get("has_access_token")),
            "last_exchange_at": auth.get("last_exchange_at"),
            "last_exchange_error": auth.get("last_exchange_error"),
            "status_note": auth.get("status_note"),
            "last_report": (self.store.state.get("last_report") or {}).get(str(pool.get("id") or "")),
        }

    def _find_pool(self, pool_id: str | None = None, pool_url: str | None = None) -> dict[str, Any] | None:
        for pool in self.store.settings.get("pools") or []:
            if pool_id and str(pool.get("id")) == str(pool_id):
                return pool
            if pool_url and str(pool.get("url") or "").rstrip("/") == pool_url.rstrip("/"):
                return pool
        return None

    def exchange_pool(self, pool_id: str) -> dict[str, Any]:
        pool = self._find_pool(pool_id=pool_id)
        if pool is None:
            raise ValueError("unknown pool")
        try:
            self.pool_client(pool).exchange(force=True)
            outcome = "bound"
            error = None
        except PoolBindingPending as err:
            outcome = "binding_pending"
            error = str(err)
        except PoolError as err:
            outcome = "error"
            error = str(err)
        return {
            "pool": self.pool_public_status(pool),
            "exchange_status": outcome,
            "exchange_error": error,
        }

    def receive_pool_key(
        self,
        pool_url: str,
        pool_api_key: str,
        *,
        pool_name: str | None = None,
        pool_id: str | None = None,
    ) -> dict[str, Any]:
        """Store a pool key handed over by SoulSync and try the exchange."""
        clean_url = str(pool_url or "").strip().rstrip("/")
        clean_key = str(pool_api_key or "").strip()
        if not clean_url or not clean_key:
            raise ValueError("pool_url and pool_api_key are required")
        check_pool_url(clean_url, allow_insecure=self.allow_insecure_pools)

        pool = self._find_pool(pool_id=pool_id, pool_url=clean_url)
        if pool is None:
            existing_ids = {str(p.get("id")) for p in self.store.settings.get("pools") or []}
            new_id = pool_id or ("sin" if "sin" not in existing_ids else f"pool-{secrets.token_hex(3)}")
            pool = {
                "id": new_id,
                "name": pool_name or "SIN",
                "url": clean_url,
                "api_key": clean_key,
                "role": "node",
                "enabled": True,
            }
            self.store.settings.setdefault("pools", []).append(pool)
        else:
            pool["url"] = clean_url
            pool["api_key"] = clean_key
            pool["enabled"] = True
            if pool_name:
                pool["name"] = pool_name

        # A new key belongs to a fresh binding/token lifecycle.
        self.store.state.setdefault("pool_auth", {})[str(pool["id"])] = {}
        self.store.save_settings()
        self.store.save_state()

        try:
            self.pool_client(pool).exchange(force=True)
            outcome = "bound"
            error = None
        except PoolBindingPending as err:
            outcome = "binding_pending"
            error = str(err)
        except PoolError as err:
            outcome = "error"
            error = str(err)
        return {
            "ok": True,
            "pool": self.pool_public_status(pool),
            "exchange_status": outcome,
            "exchange_error": error,
        }

    def disconnect_pool(self, pool_id: str | None = None) -> dict[str, Any]:
        """Clear stored pool key(s) and their access token/binding state."""
        changed: list[str] = []
        for pool in self.store.settings.get("pools") or []:
            if pool_id and str(pool.get("id")) != str(pool_id):
                continue
            pool["api_key"] = ""
            self.store.state.setdefault("pool_auth", {})[str(pool.get("id"))] = {}
            changed.append(str(pool.get("id")))
        if pool_id and not changed:
            raise ValueError("unknown pool")
        self.store.save_settings()
        self.store.save_state()
        return {"ok": True, "disconnected_pools": changed}

    # -- MetaSync local API key (SoulSync -> MetaSync) -----------------------
    @staticmethod
    def _local_key_hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def local_api_key_status(self) -> dict[str, Any]:
        return {
            "configured": bool(self.store.settings.get("local_api_key_hash")),
            "prefix": self.store.settings.get("local_api_key_prefix"),
            "created_at": self.store.settings.get("local_api_key_created_at"),
        }

    def issue_local_api_key(self) -> str:
        """Create a local key. The RAW value is returned exactly once."""
        raw_key = "mn_local_" + secrets.token_urlsafe(32)
        self.store.settings["local_api_key_hash"] = self._local_key_hash(raw_key)
        self.store.settings["local_api_key_prefix"] = raw_key[:16]
        self.store.settings["local_api_key_created_at"] = _now()
        self.store.save_settings()
        return raw_key

    # Descriptive alias for UI code.
    def regenerate_local_api_key(self) -> str:
        return self.issue_local_api_key()

    def revoke_local_api_key(self) -> None:
        self.store.settings["local_api_key_hash"] = None
        self.store.settings["local_api_key_prefix"] = None
        self.store.settings["local_api_key_created_at"] = None
        self.store.save_settings()

    def verify_local_api_key(self, raw_key: str | None) -> bool:
        stored = self.store.settings.get("local_api_key_hash")
        if not raw_key or not stored:
            return False
        return hmac.compare_digest(self._local_key_hash(raw_key), str(stored))

    # -- one-click local pairing ---------------------------------------------
    def request_pairing(self, requested_from: str | None = None) -> dict[str, Any]:
        """Open a pairing session. ``requested_from`` (the caller's address)
        is shown beside the approval buttons: the request endpoint is
        unauthenticated by design, so the person approving must be able to
        see whether it came from their SoulSync or from somewhere else."""
        now_ts = time.time()
        attempts = [
            float(ts) for ts in (self.store.state.get("pairing_attempts") or [])
            if isinstance(ts, (int, float)) and float(ts) > now_ts - 600
        ]
        if len(attempts) >= 5:
            self.store.state["pairing_attempts"] = attempts
            self.store.save_state()
            raise PairingRateLimited("pairing requests are rate-limited; try again later")
        attempts.append(now_ts)
        self.store.state["pairing_attempts"] = attempts

        existing = self.store.state.get("pairing_session")
        if isinstance(existing, dict) and existing.get("status") in {"pending", "approved"}:
            expires = _parse_time(existing.get("expires_at"))
            if expires is not None and expires > datetime.now(timezone.utc):
                self.store.save_state()
                raise PairingError("a pairing request is already in progress")

        created = datetime.now(timezone.utc)
        expires = created + timedelta(minutes=10)
        session = {
            "session_id": secrets.token_urlsafe(18),
            "pairing_code": f"{secrets.randbelow(1_000_000):06d}",
            "status": "pending",
            "created_at": created.isoformat(),
            "expires_at": expires.isoformat(),
            "requested_from": str(requested_from or "unknown"),
        }
        self.store.state["pairing_session"] = session
        self.store.save_state()
        return {
            "session_id": session["session_id"],
            "pairing_code": session["pairing_code"],
            "node_fingerprint": self.identity.fingerprint,
        }

    def pending_pairing(self) -> dict[str, Any] | None:
        session = self.store.state.get("pairing_session")
        if not isinstance(session, dict) or session.get("status") not in {"pending", "approved"}:
            return None
        expires = _parse_time(session.get("expires_at"))
        if expires is not None and expires <= datetime.now(timezone.utc):
            session["status"] = "expired"
            self.store.save_state()
            return None
        return dict(session)

    def confirm_pairing(self, session_id: str, approved: bool) -> dict[str, Any]:
        session = self.pending_pairing()
        if session is None or not hmac.compare_digest(str(session.get("session_id") or ""), str(session_id or "")):
            raise PairingError("pairing request unavailable")
        stored = self.store.state["pairing_session"]
        stored["status"] = "approved" if approved else "rejected"
        stored["decided_at"] = _now()
        self.store.save_state()
        return {
            "session_id": stored["session_id"],
            "status": stored["status"],
            "node_fingerprint": self.identity.fingerprint,
            "expires_at": stored.get("expires_at"),
        }

    def claim_pairing(
        self,
        session_id: str,
        pairing_code: str,
        soulsync_name: str,
        soulsync_url: str,
        soulsync_api_key: str,
    ) -> dict[str, Any]:
        """Complete an approved pairing exactly once.

        All failures intentionally collapse to one generic error: the
        endpoint must not become an oracle for session/code state.
        """
        session = self.store.state.get("pairing_session")
        valid = isinstance(session, dict)
        if valid:
            expires = _parse_time(session.get("expires_at"))
            valid = (
                session.get("status") == "approved"
                and expires is not None
                and expires > datetime.now(timezone.utc)
                and hmac.compare_digest(str(session.get("session_id") or ""), str(session_id or ""))
                and hmac.compare_digest(str(session.get("pairing_code") or ""), str(pairing_code or ""))
                and bool(str(soulsync_url or "").strip())
                and bool(str(soulsync_api_key or "").strip())
            )
        if not valid:
            raise PairingError("pairing unavailable")

        clean_url = str(soulsync_url).strip().rstrip("/")
        self.store.settings["soulsync"] = {
            "url": clean_url,
            "api_key": str(soulsync_api_key).strip(),
        }
        session["status"] = "claimed"
        session["claimed_at"] = _now()
        self.store.state["last_pairing"] = {
            "at": _now(),
            "soulsync_name": str(soulsync_name or ""),
            "soulsync_url": clean_url,
        }
        local_key = self.issue_local_api_key()
        self.store.save_settings()
        self.store.save_state()
        return {
            "local_api_key": local_key,
            "node_id": self.identity.node_id,
            "node_fingerprint": self.identity.fingerprint,
        }

    def local_status(self) -> dict[str, Any]:
        status = self.status()
        pairing = self.pending_pairing()
        return {
            "ok": True,
            "service": "metasync-node",
            "version": "0.2.0",
            "node_id": self.identity.node_id,
            "node_fingerprint": self.identity.fingerprint,
            "node_public_key": self.identity.public_key_b64url,
            "soulsync_connected": status["soulsync_connected"],
            "soulsync_url": (self.store.settings.get("soulsync") or {}).get("url") or "",
            "local_api_key": self.local_api_key_status(),
            "pools": status["pools"],
            "last_report": status["last_report"],
            "last_refresh_at": status["last_refresh_at"],
            "charts_offered": status["charts_offered"],
            "pairing_status": pairing.get("status") if pairing else None,
            "last_pairing": self.store.state.get("last_pairing"),
        }

    # -- report leg ---------------------------------------------------------
    def compute_report(self, now: datetime | None = None, progress: Any = None) -> dict[str, Any]:
        """Compute the report for the ISO week that closed before ``now``
        (DESIGN-NODE.md §12): events inside [week_start, week_end), ages
        measured at week_end, library state as read now."""
        now = now or self.clock()
        week_start = closed_week_start(now)
        week_end = week_start + timedelta(days=7)
        report_week = iso_week_string(week_start)
        emit = progress or (lambda event: None)
        client = self.soulsync()

        def page_events(phase: str, label: str, count_key: str):
            def on_page(page: int, count: int, total_pages: Any) -> None:
                emit({
                    "phase": phase,
                    "detail": f"Reading {label} from SoulSync, page {page}"
                              + (f" of {total_pages}" if total_pages else ""),
                    "page": page,
                    "pages_total": total_pages if isinstance(total_pages, int) else None,
                    "counts": {count_key: count},
                })
            return on_page

        emit({"phase": "watchlist", "detail": "Reading your watchlist from SoulSync", "page": None, "pages_total": None})
        watchlist = client.list_watchlist()
        emit({"counts": {"watchlist": len(watchlist)}})
        emit({"phase": "wishlist", "detail": "Reading your wishlist from SoulSync", "page": None, "pages_total": None})
        wishlist = client.list_wishlist(self.config.library_max_pages, on_page=page_events("wishlist", "your wishlist", "wishlist"))
        emit({"phase": "albums", "detail": "Reading your library albums from SoulSync", "page": None, "pages_total": None})
        albums = client.list_albums(self.config.library_max_pages, on_page=page_events("albums", "your library albums", "albums"))
        emit({"phase": "downloads", "detail": "Reading your download history from SoulSync", "page": None, "pages_total": None})
        downloads: list[dict[str, Any]] | None = None
        downloads_error: str | None = None
        try:
            downloads = client.download_activity(week_start.isoformat(), week_end.isoformat())
        except Exception as err:  # noqa: BLE001 — an older SoulSync 404s this endpoint; download signals are an addition, never a reason to fail the whole report.
            downloads_error = str(err)
        # Listening is read ONLY when the user turned it on: with the switch
        # off the node does not even ask SoulSync (DESIGN-LISTEN-SIGNALS.md).
        share_listening = bool(self.store.settings.get("share_listening", False))
        listening: list[dict[str, Any]] | None = None
        listening_error: str | None = None
        if share_listening:
            emit({"phase": "listening", "detail": "Reading last week's listening counts from SoulSync", "page": None, "pages_total": None})
            try:
                listening = client.listening_activity(week_start.isoformat(), week_end.isoformat())
            except Exception as err:  # noqa: BLE001 — same rule as downloads: an older SoulSync without the endpoint just sends no listening signals.
                listening_error = str(err)
        emit({"phase": "resolving", "detail": "Matching albums to MusicBrainz release groups", "page": None, "pages_total": None})
        # This week's work first: download and listening rows, then the
        # albums whose signals depend on this week (landed in it, crossing
        # their 14-day mark in it, or waiting for a late match). The rest
        # of the library only feeds churn snapshots and coverage numbers,
        # so a huge library spends its lookup budget on what is reported.
        late_albums = set(((self.store.state.get("late_pending") or {}).get("albums") or {}).keys())
        window_start = week_start - KEEP_AFTER - LATE_WINDOW

        def this_weeks_album(album: dict[str, Any]) -> bool:
            created = _parse_time(album.get("created_at"))
            return (created is not None and window_start <= created < week_end) or str(album.get("id")) in late_albums

        priority_release_ids = [
            str(row.get("musicbrainz_release_id"))
            for row in [*(downloads or []), *(listening or []), *(a for a in albums if this_weeks_album(a))]
            if is_uuid_shape(row.get("musicbrainz_release_id"))
        ]
        release_to_group = self._resolve_album_groups(
            albums, progress=emit, priority_release_ids=priority_release_ids,
        )
        emit({"phase": "signals", "detail": "Building last week's signals", "page": None, "pages_total": None})
        # Download history the node keeps for itself: release group ->
        # first/last completed download. SoulSync only reports albums
        # still in the library, so this record (built while albums live)
        # is what lets a later deletion be recognized as churn at all.
        downloaded_groups: dict[str, dict[str, str]] = dict(self.store.state.get("downloaded_groups") or {})
        if downloads is not None:
            for row in downloads:
                release_id = row.get("musicbrainz_release_id")
                if not is_uuid_shape(release_id):
                    continue
                rg = release_to_group.get(release_id.lower())
                if not rg:
                    continue
                entry = downloaded_groups.setdefault(rg, {})
                first = row.get("first_downloaded_at")
                last = row.get("last_downloaded_at")
                if isinstance(first, str) and (not entry.get("first_downloaded_at") or first < entry["first_downloaded_at"]):
                    entry["first_downloaded_at"] = first
                if isinstance(last, str) and (not entry.get("last_downloaded_at") or last > entry["last_downloaded_at"]):
                    entry["last_downloaded_at"] = last
            self.store.state["downloaded_groups"] = downloaded_groups
        # Churn snapshots are per WEEK, not per run: "absent from two
        # consecutive reads" must mean two weeks, however often the report
        # is re-run. prev = the newest snapshot from an earlier week, prev2
        # the one before it; this week's own snapshot is replaced in place.
        snapshots = _weekly_snapshots(self.store.state.get("library_group_snapshots"))
        earlier = [snap for snap in snapshots if snap["week"] < report_week]
        prev_groups = set(earlier[0]["groups"]) if len(earlier) > 0 else set()
        prev2_groups = set(earlier[1]["groups"]) if len(earlier) > 1 else set()
        # A read that stopped at the page cap is a partial picture: say so in
        # the diagnostics every surface shows, never undercount quietly.
        truncated = getattr(client, "truncated_paths", set())
        albums_truncated = "/library/albums" in truncated
        result = compute_signals(
            watchlist=watchlist, wishlist=wishlist, albums=albums,
            release_to_group=release_to_group, week_start=week_start,
            downloads=downloads, downloaded_groups=downloaded_groups,
            library_groups_prev=prev_groups, library_groups_prev2=prev2_groups,
            share_churn=bool(self.store.settings.get("share_churn", False)),
            library_complete=not albums_truncated,
            listening=listening,
            first_seen=self.store.state.get("library_first_seen") or {},
            late_pending=self.store.state.get("late_pending") or {},
        )
        # The node's own memory (never sent): earliest date per album group,
        # and rows still waiting for a MusicBrainz match (signals.py GAP-1/3).
        self.store.state["library_first_seen"] = result.pop("first_seen")
        self.store.state["late_pending"] = result.pop("late_pending")
        result["diagnostics"]["albums_truncated"] = albums_truncated
        result["diagnostics"]["wishlist_truncated"] = "/wishlist" in truncated
        result["diagnostics"]["downloads_available"] = downloads is not None
        if downloads_error:
            result["diagnostics"]["downloads_error"] = downloads_error
        if listening_error:
            result["diagnostics"]["listening_error"] = listening_error
        # Record the snapshot only on a complete read: a partial picture
        # must never become the "proof" that an album is gone.
        if not albums_truncated:
            self.store.state["library_group_snapshots"] = [
                {"week": report_week, "groups": result["library_groups"]},
                *earlier,
            ][:_SNAPSHOT_WEEKS]
        emit({"counts": {"signals": len(result["signals"])}})
        self.store.save_state()
        return result

    def _resolve_album_groups(self, albums: list[dict[str, Any]], progress: Any = None,
                              priority_release_ids: list[str] | None = None) -> dict[str, str]:
        emit = progress or (lambda event: None)
        mapping: dict[str, str] = dict(self.store.state.get("release_to_group") or {})
        # SoulSync's own enrichment serves the group on each album: those
        # need no MusicBrainz call at all, and override an older cached
        # answer (SoulSync re-resolves when an album's release id changes).
        for album in albums:
            release_id = album.get("musicbrainz_release_id")
            group = album.get("musicbrainz_release_group_id")
            if is_uuid_shape(release_id) and is_uuid_shape(group):
                mapping[release_id.lower()] = group.lower()
        budget = self.config.release_resolution_cap
        checked = 0
        # Download rows resolve first: there are few of them and they carry
        # this week's grab/churn value. The library tail spends the budget
        # that remains (a 71k library catches up over several runs; the
        # downloads are never the ones left waiting).
        queue: list[str] = [rid.lower() for rid in (priority_release_ids or [])]
        for album in albums:
            release_id = album.get("musicbrainz_release_id")
            if is_uuid_shape(release_id):
                queue.append(release_id.lower())
        seen: set[str] = set()
        for key in queue:
            if key in seen or key in mapping or budget <= 0:
                continue
            seen.add(key)
            budget -= 1
            checked += 1
            try:
                group = self.resolver.release_to_group(key)
            except Exception:
                group = None  # MB failure skips this album this week; never fails the run
            if group:
                mapping[key] = group.lower()
                self.store.state.setdefault("release_to_group", {})[key] = group.lower()
            if checked % 25 == 0:
                emit({"phase": "resolving", "detail": f"Matching albums to MusicBrainz release groups ({checked} checked)"})
        return mapping

    def payload_for_pool(self, pool: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "week": report["week"],
            "token": token_for_week(self.identity, report["week"]),
            "signals": report["signals"],
        }
        # Only a node sharing drop signals says so: the pool counts how many
        # do, never which, to weigh drops against every reporter's keeps.
        if self.store.settings.get("share_churn", False):
            payload["churn_optin"] = True
        return payload

    def report_slot(self) -> timedelta:
        """This node's fixed offset into Monday (0-6h UTC) before the
        scheduler sends. Spreads load, and keeps every node's report from
        arriving in the same minute (arrival time is the one thing the
        pool sees that the payload does not carry). Derived from the
        node's secret, so it is stable and says nothing about the node."""
        digest = hashlib.sha256(f"{self.identity.key}:report-slot".encode()).digest()
        return timedelta(seconds=int.from_bytes(digest[:4], "big") % (6 * 3600))

    def report_due(self, now: datetime) -> bool:
        """Whether the scheduler should run the weekly report now.

        Due once per closed week per pool, after this node's slot, with a
        back-off between failed attempts — never every scheduler tick. A
        dry-run node previews each closed week once. A pool whose last
        report is for this week or LATER is done: that also covers the
        upgrade from the old start-of-week reporting, whose last_report
        names the following week, so a node never re-reports (and
        double-counts) the week it already sent."""
        pools = self.pools()
        if not pools:
            return False
        week = iso_week_string(closed_week_start(now))
        if now < closed_week_start(now) + timedelta(days=7) + self.report_slot():
            return False
        last = self.store.state.get("last_report") or {}
        if all(str((last.get(p["id"]) or {}).get("week") or "") >= week for p in pools):
            return False
        if self.dry_run:
            previews = self.store.state.get("previews") or {}
            if all((previews.get(p["id"]) or {}).get("week") == week for p in pools):
                return False
        attempt = self.store.state.get("last_report_attempt") or {}
        tried = _parse_time(attempt.get("at"))
        retry = timedelta(hours=self.config.report_retry_interval_h)
        if attempt.get("week") == week and tried is not None and now - tried < retry:
            return False
        return True

    def run_report(self, now: datetime | None = None, progress: Any = None) -> dict[str, Any]:
        """Pull + compute + (unless dry-run) push last week's report to every enabled pool."""
        emit = progress or (lambda event: None)
        now = now or self.clock()
        outcome: dict[str, Any] = {
            "week": iso_week_string(closed_week_start(now)), "pools": {}, "error": None, "dry_run": self.dry_run,
        }
        if not self._run_lock.acquire(blocking=False):
            outcome["error"] = "A report is already running."
            return outcome
        try:
            self.store.state["last_report_attempt"] = {"week": outcome["week"], "at": now.isoformat()}
            return self._run_report_locked(now, emit, outcome)
        finally:
            self._run_lock.release()

    def _run_report_locked(self, now: datetime, emit: Any, outcome: dict[str, Any]) -> dict[str, Any]:
        try:
            report = self.compute_report(now, progress=emit)
        except Exception as err:  # SoulSync leg failed: record, stay alive.
            self.store.record_error("report:pull", err)
            self.store.save_state()
            outcome["error"] = str(err)
            return outcome
        outcome["week"] = report["week"]
        outcome["signal_count"] = len(report["signals"])
        outcome["diagnostics"] = report["diagnostics"]
        # The preview page renders this snapshot: it never recomputes live.
        self.store.state["last_diagnostics"] = report["diagnostics"]
        for pool in self.pools():
            pool_name = str(pool.get("name") or pool["id"])
            payload = self.payload_for_pool(pool, report)
            self.store.state.setdefault("previews", {})[pool["id"]] = {
                **payload, "computed_at": now.isoformat(), "sent_at": None,
            }
            entry: dict[str, Any] = {"sent": False, "status": "dry_run" if self.dry_run else "pending", "response": None, "error": None, "payload": payload}
            if self.dry_run:
                emit({"phase": "sending", "detail": f"Dry run: {pool_name} would receive {len(report['signals'])} signals. Nothing is sent.", "page": None, "pages_total": None})
            elif not report["signals"]:
                # A quiet week: nothing to say, and a pool refuses an empty
                # report anyway. The week still counts as done.
                entry["status"] = "empty"
                self.store.state.setdefault("last_report", {})[pool["id"]] = {
                    "week": report["week"], "at": now.isoformat(), "signal_count": 0,
                }
                emit({"phase": "sending", "detail": f"Nothing happened in {report['week']} worth reporting; {pool_name} gets no report this week."})
            else:
                emit({"phase": "sending", "detail": f"Sending the report to {pool_name} (the first contact exchanges your key for a token)", "page": None, "pages_total": None})
                try:
                    response = self.pool_client(pool).post_report(payload)
                    entry["response"] = response
                    entry["sent"] = True
                    # A pool counts one report per node per week; a repeat
                    # is acknowledged, not counted again.
                    entry["status"] = "already_sent" if response.get("duplicate") else "sent"
                    self.store.state.setdefault("last_report", {})[pool["id"]] = {
                        "week": report["week"], "at": now.isoformat(), "signal_count": len(report["signals"]),
                    }
                    self.store.state["previews"][pool["id"]]["sent_at"] = now.isoformat()
                except PoolBindingPending as err:
                    # A first exchange can legitimately stop here: show the
                    # waiting state, do not count it as a failed push.
                    entry["status"] = "binding_pending"
                    entry["error"] = str(err)
                    emit({"phase": "sending", "detail": f"{pool_name} is waiting for approval in the portal. Approve this node there, then run the report again."})
                except Exception as err:
                    self.store.record_error(f"report:push:{pool['id']}", err)
                    entry["status"] = "error"
                    entry["error"] = str(err)
            outcome["pools"][pool["id"]] = entry
        self.store.save_state()
        return outcome

    # -- report job: the same run, in the background, with live status ------
    def report_job(self) -> dict[str, Any] | None:
        job = self.store.state.get("report_job")
        return dict(job) if isinstance(job, dict) else None

    def start_report_job(self) -> dict[str, Any]:
        """Start the weekly report on a background thread and return its job
        record immediately. The dashboard polls report_job() for live status;
        a second start while one is running returns the running job."""
        with self._report_lock:
            current = self.store.state.get("report_job") or {}
            if current.get("status") == "running":
                return dict(current)
            job: dict[str, Any] = {
                "id": secrets.token_hex(4),
                "kind": "report",
                "status": "running",
                "started_at": _now(),
                "finished_at": None,
                "phase": "starting",
                "detail": "Starting the weekly report",
                "page": None,
                "pages_total": None,
                "counts": {"watchlist": None, "wishlist": None, "albums": None, "signals": None},
                "week": None,
                "dry_run": self.dry_run,
                "error": None,
                "pools": {},
            }
            self.store.state["report_job"] = job
            self.store.save_state()
        thread = threading.Thread(
            target=self._run_report_job, args=(job["id"],),
            daemon=True, name="metasync-report-job",
        )
        thread.start()
        return dict(job)

    def _job_progress(self, job_id: str):
        # The dashboard polls the in-memory job record, so progress needs no
        # disk write to show up. state.json is still written now and then
        # (a phase change, or every couple of seconds) so a restart mid-run
        # finds a recent record — but never once per page: on a large
        # library that was hundreds of full state rewrites per run.
        last_saved = {"at": 0.0, "phase": None}

        def progress(event: dict[str, Any]) -> None:
            job = self.store.state.get("report_job") or {}
            if job.get("id") != job_id or job.get("status") != "running":
                return
            for key in ("phase", "detail", "page", "pages_total"):
                if key in event:
                    job[key] = event[key]
            counts = event.get("counts")
            if isinstance(counts, dict):
                job.setdefault("counts", {}).update(counts)
            self.store.state["report_job"] = job
            if job.get("phase") != last_saved["phase"] or time.monotonic() - last_saved["at"] >= 2.0:
                last_saved["at"] = time.monotonic()
                last_saved["phase"] = job.get("phase")
                self.store.save_state()
        return progress

    def _run_report_job(self, job_id: str) -> None:
        try:
            outcome = self.run_report(self.clock(), progress=self._job_progress(job_id))
        except Exception as err:  # noqa: BLE001 — backstop only: a background job must never die silently; run_report handles its own legs.
            outcome = {"error": str(err), "pools": {}, "week": None, "dry_run": self.dry_run}
        job = self.store.state.get("report_job") or {}
        if job.get("id") != job_id:
            return
        failed = bool(outcome.get("error"))
        job["status"] = "error" if failed else "done"
        job["phase"] = "failed" if failed else "done"
        job["detail"] = (
            str(outcome.get("error"))
            if failed
            else "Dry run complete. The payload preview is up to date; nothing was sent."
            if outcome.get("dry_run")
            else f"Report for {outcome.get('week')} sent to every enabled pool."
        )
        diag = outcome.get("diagnostics") or {}
        if not failed and (diag.get("albums_truncated") or diag.get("wishlist_truncated")):
            job["detail"] += " Warning: a SoulSync read hit its page cap, so this report covers part of the library."
        job["error"] = outcome.get("error")
        job["week"] = outcome.get("week")
        job["finished_at"] = _now()
        job["page"] = None
        job["pages_total"] = None
        if outcome.get("signal_count") is not None:
            job.setdefault("counts", {})["signals"] = outcome["signal_count"]
        job["pools"] = {
            pid: {"status": entry.get("status"), "error": entry.get("error")}
            for pid, entry in (outcome.get("pools") or {}).items()
        }
        self.store.state["report_job"] = job
        self.store.save_state()

    # -- refresh leg: pull the pool's charts, offer them to SoulSync ----------
    def run_refresh(self, now: datetime | None = None) -> dict[str, Any]:
        """For every enabled pool: get its canonical playlist data and keep it.

        Node-role pools PULL published playlist data. Admin-role pools (the
        official node) BUILD the definitions from the index via MusicBrainz
        and PUBLISH them. Either way the result is cached here and offered to
        SoulSync on GET /local/charts, which SoulSync's own daily automation
        reads. The node never writes a playlist into SoulSync, never
        wishlists and never downloads: SoulSync shows the charts as playlists
        a person can open, and only that person's Sync & download moves
        anything. A pool that does not implement playlist endpoints yet
        reports "playlist_data_unavailable"; nothing is invented.
        """
        now = now or datetime.now(timezone.utc)
        outcome: dict[str, Any] = {"pools": {}, "error": None, "dry_run": self.dry_run}
        for pool in self.pools():
            client = self.pool_client(pool)
            entry: dict[str, Any] = {"role": client.role, "status": "ok", "week": None, "playlists": [], "error": None}
            try:
                if client.role == "admin":
                    built = build_canonical_playlists(
                        client, self.resolver,
                        max_items=self.config.playlist_max_items,
                        tracks_per_item=self.config.playlist_tracks_per_item,
                    )
                    entry["week"] = built["week"]
                    entry["unresolved"] = built["unresolved"]
                    self.store.state.setdefault("built_definitions", {})[pool["id"]] = built
                    try:
                        client.publish_playlists(built["week"], built["playlists"])
                        entry["status"] = "published"
                    except PoolNotAvailable as err:
                        entry["status"] = "publish_unavailable"
                        entry["error"] = str(err)
                    week, playlists = built["week"], built["playlists"]
                else:
                    try:
                        pulled = client.get_playlists()
                    except PoolNotAvailable as err:
                        entry["status"] = "playlist_data_unavailable"
                        entry["error"] = str(err)
                        outcome["pools"][pool["id"]] = entry
                        continue
                    week, playlists = pulled["week"], pulled["playlists"]
                entry["week"] = week
                self.store.state.setdefault("playlists_cache", {})[pool["id"]] = {
                    "fetched_at": now.isoformat(), "week": week, "playlists": playlists,
                }
                entry["playlists"] = [
                    {"key": str(p.get("key")), "name": p.get("name"), "track_count": len(p.get("tracks") or [])}
                    for p in playlists
                ]
            except PoolBindingPending as err:
                entry["status"] = "binding_pending"
                entry["error"] = str(err)
            except Exception as err:
                self.store.record_error(f"refresh:{pool['id']}", err)
                entry["status"] = "error"
                entry["error"] = str(err)
            outcome["pools"][pool["id"]] = entry
        self.store.state["last_refresh_at"] = now.isoformat()
        self.store.save_state()
        return outcome

    def local_charts(self) -> dict[str, Any]:
        """What SoulSync's "Refresh MetaSync Charts" automation reads
        (GET /local/charts): every enabled pool's cached charts, as
        {week, playlists: [{key, name, tracks}]}. SIN's charts keep their
        keys; another pool's are prefixed with its id (and its name in the
        title) so two pools can never collide. Empty playlists are left out.
        """
        cache = self.store.state.get("playlists_cache") or {}
        week: str | None = None
        playlists: list[dict[str, Any]] = []
        for pool in self.pools():
            pool_id = str(pool.get("id") or "pool")
            cached = cache.get(pool_id) or {}
            week = week or cached.get("week")
            prefix = "" if pool_id == "sin" else re.sub(r"[^a-z0-9]+", "-", pool_id.lower()).strip("-") + "-"
            for chart in cached.get("playlists") or []:
                tracks = chart.get("tracks") or []
                if not tracks:
                    continue
                name = str(chart.get("name") or chart.get("key") or "")
                playlists.append({
                    "key": f"{prefix}{chart.get('key')}"[:64],
                    "name": name if not prefix else f"{pool.get('name') or pool_id} · {name}",
                    "tracks": tracks,
                })
        return {"week": week, "playlists": playlists}

    # -- metadata network (relay transport, DESIGN-METADATA.md section 10) --
    def _meta_relay_url(self) -> str:
        return str(self.store.settings.get("meta_relay_url") or "").strip()

    def _meta_node_mode(self) -> str:
        return normalize_mode(self.store.settings.get("meta_node_mode"))

    def _relay_token(self) -> str:
        """A current pool access token for the relay hello.

        The relay authenticates with the SIN exchange token (section
        10): the first enabled pool's client exchanges as usual, and
        the token must carry the meta:read scope the pools now grant.
        Anything else (no pool, legacy pool, missing scope) is a
        connect failure the relay client backs off on."""
        pools = self.pools()
        if not pools:
            raise RelayError("no pool joined: the relay hello needs a pool key exchange")
        client = self.pool_client(pools[0])
        client.exchange()
        token = client.auth_state.get("access_token")
        if not token:
            raise RelayError("pool exchange produced no access token (legacy pool?)")
        scopes = client.auth_state.get("scopes") or []
        if "meta:read" not in scopes:
            # Missing or empty scope lists mean the token cannot be
            # shown to carry meta:read (a legacy pool records none):
            # fail here with the clear diagnosis instead of letting
            # the relay refuse a hello it was never entitled to make.
            raise RelayError("pool access token does not carry the meta:read scope")
        return str(token)

    def _new_relay_client(self) -> RelayClient:
        return RelayClient(
            self.identity,
            url_provider=self._meta_relay_url,
            mode_provider=self._meta_node_mode,
            token_provider=self._relay_token,
        )

    def sync_relay_client(self) -> dict[str, Any]:
        """Start or stop the relay client to match the settings.

        The metadata network is its own switch: settings meta_network
        (default off) plus a relay URL. With the switch off, or no URL,
        the client stops and nothing connects anywhere. With it on, the
        client runs; a URL or mode change kicks the live session so it
        re-hellos with the new values. Idempotent; the scheduler calls
        it every tick so dashboard changes land without a restart."""
        want = bool(self.store.settings.get("meta_network", False)) and bool(self._meta_relay_url())
        stop_client: RelayClient | None = None
        with self._relay_lock:
            client = self._relay_client
            if want:
                if client is None:
                    client = self._relay_client = self._new_relay_client()
                    client.start()
                elif not client.is_alive():
                    client.start()
                elif client.session_params is not None and client.session_params != (
                    self._meta_relay_url(),
                    self._meta_node_mode(),
                ):
                    client.kick()
            elif client is not None:
                stop_client = client
        # The stop runs OUTSIDE the lock: stop() joins the connection
        # thread, and holding _relay_lock across that join would stall
        # every relay_status() for its duration. The reference is only
        # dropped once stop() confirms the thread is dead; a client
        # that is still winding down keeps its reference, so the next
        # tick retries the stop instead of spawning a replacement that
        # would duel the survivor at the relay.
        if stop_client is not None and stop_client.stop():
            with self._relay_lock:
                if self._relay_client is stop_client:
                    self._relay_client = None
        # Answering is its own opt-in, separate from the network
        # switch and from every SIN switch: the query handler is
        # registered only while BOTH meta_network and meta_answering
        # are on, and removed the moment either turns off. The
        # responder re-reads the node mode per query, so a mode
        # change needs no rewiring here.
        with self._relay_lock:
            client = self._relay_client
        if client is not None:
            want_handler = want and self.meta_answering()
            client.set_query_handler(
                self._meta_query_handler if want_handler else None)
        return self.relay_status()

    def stop_relay_client(self) -> None:
        with self._relay_lock:
            client = self._relay_client
        if client is not None and client.stop():
            with self._relay_lock:
                if self._relay_client is client:
                    self._relay_client = None

    def relay_status(self) -> dict[str, Any]:
        """Relay connection status for the dashboard and diagnostics."""
        enabled = bool(self.store.settings.get("meta_network", False))
        client = self._relay_client
        if client is None:
            return {
                "enabled": enabled,
                "answering": self.meta_answering(),
                "connected": False,
                "peers": None,
                "mode": self._meta_node_mode(),
                "relay_url": self._meta_relay_url(),
                "last_error": None,
            }
        status = client.status()
        status["enabled"] = enabled
        status["answering"] = self.meta_answering()
        return status

    # -- metadata responder (claims store, DESIGN-METADATA.md section 5) --
    def meta_answering(self) -> bool:
        """The answering opt-in: settings meta_answering, default off,
        separate from meta_network and from every SIN switch."""
        return bool(self.store.settings.get("meta_answering", False))

    def meta_store(self) -> MetaStore:
        if self._meta_store is None:
            with self._meta_construct_lock:
                if self._meta_store is None:
                    self._meta_store = MetaStore(self.store.dir)
        return self._meta_store

    def _meta_responder_obj(self) -> Responder:
        if self._meta_responder is None:
            with self._meta_construct_lock:
                if self._meta_responder is None:
                    self._meta_responder = Responder(
                        self.meta_store(), self.identity,
                        mode_provider=self._meta_node_mode)
        return self._meta_responder

    def _meta_store_created(self) -> bool:
        """The claims store exists (constructed in this process, or
        its file is already on disk). Status/due paths use this to
        stay store-free on nodes that never built one: opening the
        store would CREATE meta_claims.sqlite3 on a node with the
        whole metadata feature off."""
        return self._meta_store is not None or \
            (self.store.dir / META_DB_FILENAME).exists()

    def _meta_query_handler(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        return self._meta_responder_obj().handle(query)

    def _meta_builder(self, live_gate: bool = False) -> LibraryBuilder:
        # The live gate (stop at the next page if answering is
        # switched off mid-walk) applies to scheduler-started builds.
        # A manual rebuild is the user's explicit request and runs
        # regardless of the switch.
        return LibraryBuilder(
            self.soulsync(), self.identity, self.meta_store(),
            enabled_on=self.meta_answering if live_gate else None)

    def meta_status(self) -> dict[str, Any]:
        if not self._meta_store_created():
            # No store yet: report the empty shape WITHOUT opening
            # one. A status read must not create the file on a node
            # that has the feature off (sync_meta_store's not-due
            # path lands here on every scheduler tick).
            return {
                "answering": self.meta_answering(),
                "mode": self._meta_node_mode(),
                "store": {"own_claims": 0, "own_subjects": 0,
                          "cached_batches": 0, "cached_claims": 0},
                "build": {"last_status": None, "last_detail": None,
                          "last_run_at": None, "last_completed_at": None,
                          "last_full_completed_at": None,
                          "last_totals": {}, "walk_entity": "",
                          "full": True},
            }
        builder = LibraryBuilder(None, self.identity, self.meta_store())
        return {
            "answering": self.meta_answering(),
            "mode": self._meta_node_mode(),
            "store": self.meta_store().counts(),
            "build": builder.status(),
        }

    def preview_answers(self, query: Any) -> dict[str, Any]:
        """Exactly what the responder would answer for this query —
        the same code path the relay handler runs, for the node's
        own UI/API ("what your node would answer")."""
        batches = self._meta_responder_obj().handle(
            query if isinstance(query, dict) else {})
        return {
            "ok": True,
            "mode": self._meta_node_mode(),
            "answering": self.meta_answering(),
            "batches": batches,
        }

    def rebuild_meta_store(self, now: datetime | None = None,
                           force_full: bool = False,
                           live_gate: bool = False) -> dict[str, Any]:
        """One claims-store build over the SoulSync export. Outcomes
        are recorded in the store and returned; a stopped build
        (export missing, answering switched off mid-run) is a
        status, not an exception."""
        if not self._meta_lock.acquire(blocking=False):
            return {"status": "already_running"}
        try:
            now = now or self.clock()
            try:
                builder = self._meta_builder(live_gate=live_gate)
            except SoulSyncError as err:
                return {"status": "soulsync_unavailable",
                        "detail": str(err)}
            try:
                # A rebuild also satisfies a due full walk: the
                # cadence lives in the builder's state, and a manual
                # rebuild is as good a moment as a scheduled one.
                totals = builder.run(
                    now.isoformat(),
                    force_full=force_full or builder.full_walk_due(now))
                builder.record_outcome("completed")
                return {"status": "completed", **totals}
            except BuildStopped as stop:
                builder.record_outcome(stop.reason, stop.detail or None)
                return {"status": stop.reason, "detail": stop.detail}
        finally:
            self._meta_lock.release()

    def meta_rebuild_due(self, now: datetime) -> bool:
        """Due when answering is on and the last completed build is
        older than the rebuild interval (or never ran) — or a full
        walk is due on its own cadence, however fresh the last
        incremental completion is (incrementals never see
        deletions, so they cannot substitute for the sweep)."""
        if not self.meta_answering():
            return False
        if not self._meta_store_created():
            return True  # never built; no store read needed
        builder = LibraryBuilder(None, self.identity, self.meta_store())
        if builder.full_walk_due(now):
            return True
        last = _parse_time(builder.status().get("last_completed_at"))
        if last is None:
            return True
        return now - last >= timedelta(hours=_META_REBUILD_INTERVAL_H)

    def sync_meta_store(self) -> dict[str, Any]:
        """Scheduler leg: start a claims-store build on a background
        thread when one is due. The build must never block the
        scheduler (a full walk of a large library takes minutes)."""
        if not self.meta_rebuild_due(self.clock()):
            return self.meta_status()
        thread = self._meta_build_thread
        if thread is not None and thread.is_alive():
            return self.meta_status()
        thread = threading.Thread(
            target=self._run_meta_build, daemon=True,
            name="metasync-meta-build")
        self._meta_build_thread = thread
        thread.start()
        return self.meta_status()

    def _run_meta_build(self) -> None:
        try:
            self.rebuild_meta_store(live_gate=True)
        except Exception as err:  # noqa: BLE001 — a background build must never die silently.
            self.store.record_error("meta_build", err)
            self.store.save_state()

    # -- status ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        state = self.store.state
        settings = self.store.settings
        return {
            "ok": True,
            "service": "metasync-node",
            "version": "0.2.0",
            "node_id": self.identity.node_id,
            "node_fingerprint": self.identity.fingerprint,
            "node_public_key": self.identity.public_key_b64url,
            "dry_run": self.dry_run,
            "share_churn": bool(settings.get("share_churn", False)),
            "share_listening": bool(settings.get("share_listening", False)),
            "current_week": iso_week_string(datetime.now(timezone.utc)),
            "report_week": iso_week_string(closed_week_start(datetime.now(timezone.utc))),
            "soulsync_connected": bool((settings.get("soulsync") or {}).get("url")),
            "soulsync_url": (settings.get("soulsync") or {}).get("url") or "",
            "local_api_key": self.local_api_key_status(),
            "pending_pairing": self.pending_pairing(),
            "pools": [self.pool_public_status(p) for p in settings.get("pools") or []],
            "last_report": state.get("last_report") or {},
            "last_refresh_at": state.get("last_refresh_at"),
            "charts_offered": len(self.local_charts()["playlists"]),
            "last_errors": (state.get("last_errors") or [])[:5],
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _weekly_snapshots(raw: Any) -> list[dict[str, Any]]:
    """Library snapshots as [{week, groups}], newest week first. The
    pre-weekly format (a bare list of group lists, rotated per RUN) cannot
    be dated, so it is dropped: churn restarts its two-week window rather
    than trust snapshots that may be minutes apart."""
    if not isinstance(raw, list):
        return []
    out = [
        {"week": str(snap["week"]), "groups": list(snap.get("groups") or [])}
        for snap in raw
        if isinstance(snap, dict) and isinstance(snap.get("week"), str)
    ]
    return sorted(out, key=lambda snap: snap["week"], reverse=True)
