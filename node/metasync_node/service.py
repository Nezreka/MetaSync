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
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .identity import NodeIdentity, load_or_create_identity, token_for_week
from .musicbrainz import MusicBrainzResolver
from .playlists import build_canonical_playlists, content_hash, local_playlist_id
from .pool import PoolBindingPending, PoolClient, PoolError, PoolNotAvailable
from .signals import compute_signals, is_uuid_shape
from .soulsync import SoulSyncClient, SoulSyncError
from .store import Store, default_pool_from_env
from .weekutil import iso_week_string


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
        if changed:
            self.store.save_settings()
        self.resolver = resolver or MusicBrainzResolver(
            config.musicbrainz_base_url,
            config.musicbrainz_user_agent,
            min_interval_s=config.musicbrainz_min_interval_s,
            cache=self.store.state.setdefault("mb_cache", {}),
        )
        self._report_lock = threading.Lock()
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
    def request_pairing(self) -> dict[str, Any]:
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
            "managed_playlists": status["managed_playlists"],
            "pairing_status": pairing.get("status") if pairing else None,
            "last_pairing": self.store.state.get("last_pairing"),
        }

    # -- report leg ---------------------------------------------------------
    def compute_report(self, now: datetime | None = None, progress: Any = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
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
        wishlist = client.list_wishlist(on_page=page_events("wishlist", "your wishlist", "wishlist"))
        emit({"phase": "albums", "detail": "Reading your library albums from SoulSync", "page": None, "pages_total": None})
        albums = client.list_albums(self.config.library_max_pages, on_page=page_events("albums", "your library albums", "albums"))
        emit({"phase": "resolving", "detail": "Matching albums to MusicBrainz release groups", "page": None, "pages_total": None})
        release_to_group = self._resolve_album_groups(albums, progress=emit)
        emit({"phase": "signals", "detail": "Building this week's signals", "page": None, "pages_total": None})
        result = compute_signals(
            watchlist=watchlist, wishlist=wishlist, albums=albums,
            release_to_group=release_to_group, now=now,
        )
        emit({"counts": {"signals": len(result["signals"])}})
        self.store.save_state()
        return result

    def _resolve_album_groups(self, albums: list[dict[str, Any]], progress: Any = None) -> dict[str, str]:
        emit = progress or (lambda event: None)
        mapping: dict[str, str] = dict(self.store.state.get("release_to_group") or {})
        budget = self.config.release_resolution_cap
        checked = 0
        for album in albums:
            release_id = album.get("musicbrainz_release_id")
            if not is_uuid_shape(release_id):
                continue
            key = release_id.lower()
            if key in mapping or budget <= 0:
                continue
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
        return {
            "week": report["week"],
            "token": token_for_week(self.identity, report["week"]),
            "signals": report["signals"],
        }

    def run_report(self, now: datetime | None = None, progress: Any = None) -> dict[str, Any]:
        """Pull + compute + (unless dry-run) push the weekly report to every enabled pool."""
        emit = progress or (lambda event: None)
        now = now or datetime.now(timezone.utc)
        outcome: dict[str, Any] = {"week": iso_week_string(now), "pools": {}, "error": None, "dry_run": self.dry_run}
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
        for pool in self.pools():
            pool_name = str(pool.get("name") or pool["id"])
            if self.dry_run:
                emit({"phase": "sending", "detail": f"Dry run: {pool_name} would receive {len(report['signals'])} signals. Nothing is sent.", "page": None, "pages_total": None})
            else:
                emit({"phase": "sending", "detail": f"Sending the report to {pool_name} (the first contact exchanges your key for a token)", "page": None, "pages_total": None})
            payload = self.payload_for_pool(pool, report)
            self.store.state.setdefault("previews", {})[pool["id"]] = {
                **payload, "computed_at": now.isoformat(), "sent_at": None,
            }
            entry: dict[str, Any] = {"sent": False, "status": "dry_run" if self.dry_run else "pending", "response": None, "error": None, "payload": payload}
            if not self.dry_run:
                try:
                    entry["response"] = self.pool_client(pool).post_report(payload)
                    entry["sent"] = True
                    entry["status"] = "sent"
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
            self.store.save_state()
        return progress

    def _run_report_job(self, job_id: str) -> None:
        try:
            outcome = self.run_report(progress=self._job_progress(job_id))
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
            else "Report sent to every enabled pool."
        )
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

    # -- refresh leg: provide + update --------------------------------------
    def _materialize(self, pool: dict[str, Any], canonical: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
        """Sync canonical playlists into the local SoulSync (stable ids, replace)."""
        results: list[dict[str, Any]] = []
        managed = self.store.state.setdefault("managed_playlists", {}).setdefault(pool["id"], {})
        client = self.soulsync()
        for playlist in canonical:
            key = str(playlist.get("key"))
            name = str(playlist.get("name") or key)
            tracks = playlist.get("tracks") or []
            playlist_id = local_playlist_id(pool["id"], key)
            entry: dict[str, Any] = {
                "key": key, "playlist_id": playlist_id, "track_count": len(tracks),
                "synced": False, "skipped_unchanged": False, "error": None,
            }
            previous = managed.get(key) or {}
            digest = content_hash(tracks)
            if previous.get("content_hash") == digest and previous.get("last_sync_at"):
                entry["skipped_unchanged"] = True
                entry["synced"] = True
                results.append(entry)
                continue
            if self.dry_run or not tracks:
                # Dry-run by design; empty by design too — a "replace" sync of
                # zero tracks would wipe the user's existing playlist.
                results.append(entry)
                continue
            try:
                client.sync_playlist(playlist_id, name, tracks, "replace")
                entry["synced"] = True
                managed[key] = {
                    "playlist_id": playlist_id, "name": name, "content_hash": digest,
                    "track_count": len(tracks), "last_sync_at": now.isoformat(), "last_error": None,
                }
            except Exception as err:
                self.store.record_error(f"refresh:sync:{pool['id']}:{key}", err)
                managed[key] = {
                    "playlist_id": playlist_id, "name": name,
                    "content_hash": previous.get("content_hash", ""), "track_count": previous.get("track_count", 0),
                    "last_sync_at": previous.get("last_sync_at"), "last_error": str(err),
                }
                entry["error"] = str(err)
            results.append(entry)
        return results

    def run_refresh(self, now: datetime | None = None) -> dict[str, Any]:
        """For every enabled pool: get canonical playlist data, materialize locally.

        Node-role pools PULL published playlist data. Admin-role pools (the
        official node) BUILD the definitions from the index via MusicBrainz
        and PUBLISH them to the pool, then materialize the same definitions
        locally. A pool that does not implement playlist endpoints yet
        reports status "playlist_data_unavailable" — nothing is invented
        and nothing is synced.
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
                    canonical = built["playlists"]
                else:
                    try:
                        pulled = client.get_playlists()
                    except PoolNotAvailable as err:
                        entry["status"] = "playlist_data_unavailable"
                        entry["error"] = str(err)
                        outcome["pools"][pool["id"]] = entry
                        continue
                    entry["week"] = pulled["week"]
                    self.store.state.setdefault("playlists_cache", {})[pool["id"]] = {
                        "fetched_at": now.isoformat(), "week": pulled["week"], "playlists": pulled["playlists"],
                    }
                    canonical = pulled["playlists"]
                entry["playlists"] = self._materialize(pool, canonical, now)
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
            "current_week": iso_week_string(datetime.now(timezone.utc)),
            "soulsync_connected": bool((settings.get("soulsync") or {}).get("url")),
            "soulsync_url": (settings.get("soulsync") or {}).get("url") or "",
            "local_api_key": self.local_api_key_status(),
            "pending_pairing": self.pending_pairing(),
            "pools": [self.pool_public_status(p) for p in settings.get("pools") or []],
            "last_report": state.get("last_report") or {},
            "last_refresh_at": state.get("last_refresh_at"),
            "managed_playlists": state.get("managed_playlists") or {},
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
