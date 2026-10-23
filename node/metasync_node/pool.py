"""Pool client — a pool is any SIN-compatible backend. SIN is one pool.

PERMISSION MODEL (Boulder's rule), enforced HERE, on the node's side:

- A pool joined with role "node" (every normal user) may ONLY:
    POST /v1/report        (push this node's weekly signals)
    GET  /v1/index/*       (pull the published index)
    GET  /v1/playlists     (pull the pool's CANONICAL playlist data)
    GET  /v1/whoami        (check the exchange-issued credential)
- The ADMIN operations exist only on a pool joined with role "admin"
  (Boulder's official MetaSync):
    POST /v1/aggregate     (trigger aggregation)
    POST /v1/playlists     (publish canonical playlist definitions)
  Calling them on a node-role pool raises PermissionError BEFORE any
  HTTP request is made.

Pool authentication follows PORTAL-DESIGN §3A.4: the long-lived pool key
is presented only to ``POST /v1/auth/exchange``, together with this
node's Ed25519 request signature. The pool returns a short-lived access
token; routine calls then use ``Authorization: Bearer <token>`` plus a
fresh node signature and never carry the raw pool key again. A pool on
loopback that answers the exchange with 404 is used in a clearly
labelled legacy direct-key development fallback; any other pool that
404s the exchange is an error, and the raw key is never sent to it.
"""
from __future__ import annotations

import ipaddress
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .identity import NodeIdentity

INDEX_KINDS = ("trending", "most-wanted", "breakouts")
NODE_OPERATIONS = ("post_report", "get_index", "get_playlists", "get_whoami")
ADMIN_OPERATIONS = ("post_aggregate", "publish_playlists")


class PoolError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class PoolNotAvailable(PoolError):
    """The pool does not implement an endpoint yet (HTTP 404)."""


class PoolBindingPending(PoolError):
    """The pool has the node/key pair, but a person has not approved it."""

    def __init__(self, message: str = "waiting for approval in the portal", status: int = 202):
        super().__init__(message, status=status)


class PoolAuthError(PoolError):
    """The pool rejected the key, binding, signature, or access token."""


class _ExchangeUnsupported(Exception):
    pass


def is_loopback_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _parse_expiry(body: dict[str, Any], now: float) -> float:
    raw = body.get("expires_at", body.get("expiresAt", body.get("expiry")))
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw:
        try:
            return float(raw)
        except ValueError:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    expires_in = body.get("expires_in", body.get("expiresIn"))
    if isinstance(expires_in, (int, float)):
        return now + float(expires_in)
    return now + 3600.0


class PoolClient:
    def __init__(
        self,
        pool: dict[str, Any],
        timeout_s: int = 15,
        session: requests.Session | None = None,
        *,
        identity: NodeIdentity | None = None,
        auth_state: dict[str, Any] | None = None,
        persist: Callable[[], None] | None = None,
        allow_legacy: bool = False,
    ):
        self.pool_id = str(pool.get("id") or "pool")
        self.name = str(pool.get("name") or self.pool_id)
        self.base = str(pool.get("url") or "").rstrip("/")
        self.api_key = str(pool.get("api_key") or "")
        self.role = str(pool.get("role") or "node")
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        self.identity = identity
        self.auth_state = auth_state if auth_state is not None else {}
        self._persist_callback = persist
        # The direct-key fallback is for a development pool on this
        # machine only (or an operator who explicitly allowed it).
        self.legacy_allowed = allow_legacy or is_loopback_url(self.base)

    # -- role gate ---------------------------------------------------------
    def _require(self, operation: str) -> None:
        if operation in NODE_OPERATIONS:
            return
        if operation in ADMIN_OPERATIONS and self.role == "admin":
            return
        raise PermissionError(
            f"pool '{self.name}' is joined with role '{self.role}': operation "
            f"'{operation}' requires an admin (official) pool connection"
        )

    # -- auth-state helpers --------------------------------------------------
    def _persist(self) -> None:
        if self._persist_callback is not None:
            try:
                self._persist_callback()
            except Exception:
                # Persistence is attempted after every auth transition; a
                # state-write failure must not turn a completed pool call
                # into a misleading network failure.
                pass

    def _update_auth(self, **updates: Any) -> None:
        self.auth_state.update(updates)
        self._persist()

    def _clear_token(self) -> None:
        for field in ("access_token", "token_expires_at", "token_expires_epoch", "scopes"):
            self.auth_state.pop(field, None)
        self._persist()

    def auth_status(self) -> dict[str, Any]:
        """Non-secret except for the access token, which callers must not render."""
        return dict(self.auth_state)

    def _token_fresh(self, skew_s: float = 30.0) -> bool:
        if not self.auth_state.get("access_token"):
            return False
        try:
            expires = float(self.auth_state.get("token_expires_epoch") or 0)
        except (TypeError, ValueError):
            return False
        return expires > time.time() + skew_s

    # -- HTTP primitives -----------------------------------------------------
    @staticmethod
    def _encode_body(method: str, body: Any) -> bytes:
        if method.upper() == "GET":
            return b""
        if body is None:
            return b"{}"
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _send(self, method: str, path: str, raw_body: bytes, headers: dict[str, str]) -> tuple[int, Any]:
        try:
            res = self.session.request(
                method,
                f"{self.base}{path}",
                data=raw_body if method.upper() != "GET" else None,
                headers=headers,
                timeout=self.timeout_s,
            )
        except requests.RequestException as err:
            raise PoolError(f"pool {self.name} unreachable at {path}: {err}") from err
        try:
            parsed = res.json()
        except ValueError:
            parsed = {}
        return res.status_code, parsed

    def _legacy_request(self, method: str, path: str, body: Any, *, include_key: bool) -> tuple[int, Any]:
        raw_body = self._encode_body(method, body)
        headers = {"Accept": "application/json"}
        if raw_body:
            headers["Content-Type"] = "application/json"
        if include_key:
            headers["X-SIN-Key"] = self.api_key
        if self.identity is not None:
            # Even the labelled legacy fallback is a pool request, so it
            # carries the node's signature; only the credential differs.
            headers.update(self.identity.sign_request(method, path, raw_body))
        return self._send(method, path, raw_body, headers)

    def _signed_request(self, method: str, path: str, body: Any, token: str) -> tuple[int, Any]:
        if self.identity is None:  # defensive; token mode is only entered with an identity
            raise PoolError("pool token mode requires a node signing identity")
        raw_body = self._encode_body(method, body)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **self.identity.sign_request(method, path, raw_body),
        }
        if raw_body:
            headers["Content-Type"] = "application/json"
        return self._send(method, path, raw_body, headers)

    # -- exchange --------------------------------------------------------------
    def exchange(self, *, force: bool = False) -> dict[str, Any]:
        """Exchange the stored pool key for an access token.

        Returns the pool's token response (or a small summary when a fresh
        token is already held). ``PoolBindingPending`` and ``PoolAuthError``
        are meaningful outcomes, not generic transport failures.
        """
        if self.identity is None:
            self._update_auth(mode="legacy", binding_status="not_applicable")
            return {"mode": "legacy"}
        if self.auth_state.get("exchange_supported") is False:
            return {"mode": "legacy"}
        if not force and self._token_fresh():
            return {
                "access_token": self.auth_state.get("access_token"),
                "expires_at": self.auth_state.get("token_expires_at"),
                "mode": "exchange",
                "cached": True,
            }

        raw_body = b"{}"
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-SIN-Key": self.api_key,
            **self.identity.sign_request("POST", "/v1/auth/exchange", raw_body),
        }
        if self.auth_state.get("binding_status") != "bound":
            headers["X-Node-Public-Key"] = self.identity.public_key_b64url

        now = time.time()
        now_iso = _iso_from_epoch(now)
        status, body = self._send("POST", "/v1/auth/exchange", raw_body, headers)
        if not isinstance(body, dict):
            body = {}

        if status == 404 and not self.legacy_allowed:
            # A production pool always has the exchange. A 404 here is a
            # wrong URL, a proxy, or someone in the path — never a reason to
            # start sending the raw pool key on every request.
            self._update_auth(
                last_exchange_at=now_iso,
                last_exchange_error="exchange HTTP 404",
                status_note="This pool has no key exchange (404). Check the pool URL; the key is not sent without it.",
            )
            raise PoolError(
                f"pool {self.name} has no key exchange at {self.base} (HTTP 404); refusing to send the pool key directly",
                status=404,
            )

        if status == 404:
            self._update_auth(
                exchange_supported=False,
                mode="legacy",
                binding_status=self.auth_state.get("binding_status") or "not_checked",
                last_exchange_at=now_iso,
                last_exchange_error=None,
                status_note="Legacy direct-key dev fallback: this pool does not support the key exchange (404).",
            )
            raise _ExchangeUnsupported("pool does not implement POST /v1/auth/exchange")

        if status == 202:
            fingerprint = str(body.get("node_fingerprint") or body.get("fingerprint") or self.identity.fingerprint)
            self._clear_token()
            self._update_auth(
                exchange_supported=True,
                mode="exchange",
                binding_status="pending_approval",
                pending_node_fingerprint=fingerprint,
                last_exchange_at=now_iso,
                last_exchange_error=None,
                status_note="Waiting for approval in the portal — compare this node's fingerprint there.",
            )
            raise PoolBindingPending(
                "waiting for approval in the portal — no access token has been issued",
                status=202,
            )

        if status == 401:
            self._clear_token()
            self._update_auth(
                exchange_supported=True,
                mode="exchange",
                binding_status="auth_failed",
                last_exchange_at=now_iso,
                last_exchange_error="pool rejected the key, node binding, or node signature",
                status_note="Pool authentication failed. Check the pool key and portal binding.",
            )
            raise PoolAuthError(f"pool {self.name} exchange failed: authentication rejected", status=401)

        if status >= 400:
            self._update_auth(
                last_exchange_at=now_iso,
                last_exchange_error=f"exchange HTTP {status}",
            )
            raise PoolError(f"pool {self.name} exchange failed: HTTP {status} {str(body)[:200]}", status=status)

        token = body.get("access_token") or body.get("token")
        if not isinstance(token, str) or not token:
            raise PoolError(f"pool {self.name} exchange returned no access token", status=status)
        returned_node_id = body.get("node_id")
        if returned_node_id and str(returned_node_id) != self.identity.node_id:
            self._clear_token()
            self._update_auth(
                binding_status="auth_failed",
                last_exchange_at=now_iso,
                last_exchange_error="pool issued a token for a different node",
            )
            raise PoolAuthError(f"pool {self.name} exchange returned a token for a different node", status=401)

        expires_epoch = _parse_expiry(body, now)
        self._update_auth(
            exchange_supported=True,
            mode="exchange",
            access_token=token,
            token_expires_epoch=expires_epoch,
            token_expires_at=_iso_from_epoch(expires_epoch),
            scopes=body.get("scopes") or [],
            key_prefix=body.get("key_prefix"),
            node_id=self.identity.node_id,
            binding_status=str(body.get("binding_status") or "bound"),
            bound=True,
            pending_node_fingerprint=None,
            last_exchange_at=now_iso,
            last_exchange_error=None,
            status_note="Exchange complete; routine calls use the short-lived access token.",
        )
        return body

    def _ensure_auth(self) -> str:
        """Return "token" or "legacy", exchanging first when needed."""
        if self.identity is None:
            return "legacy"
        if self.auth_state.get("exchange_supported") is False:
            self.auth_state.setdefault("mode", "legacy")
            self.auth_state.setdefault(
                "status_note",
                "Legacy direct-key dev fallback: this pool does not support the key exchange (404).",
            )
            return "legacy"
        if self._token_fresh():
            return "token"
        try:
            self.exchange(force=True)
        except _ExchangeUnsupported:
            return "legacy"
        return "token"

    def _request(self, method: str, path: str, body: Any = None, auth: bool = True, _retry: bool = False) -> tuple[int, Any]:
        if self.identity is None:
            return self._legacy_request(method, path, body, include_key=auth)

        # With a signing identity, every pool operation uses the exchange
        # path. The historical ``auth=False`` on index/playlist GETs meant
        # "public" in the pre-exchange MVP; §3A.7 gates those reads too.
        mode = self._ensure_auth()
        if mode == "legacy":
            return self._legacy_request(method, path, body, include_key=True)

        token = str(self.auth_state.get("access_token") or "")
        status, parsed = self._signed_request(method, path, body, token)
        if status == 401 and not _retry:
            # A 401 on a token call gets exactly one fresh exchange + retry.
            # Treat the binding as unbound for that exchange so the public
            # key is presented again if the pool dropped the binding.
            self._clear_token()
            self._update_auth(binding_status="unbound", bound=False)
            try:
                self.exchange(force=True)
            except _ExchangeUnsupported:
                return self._legacy_request(method, path, body, include_key=True)
            return self._request(method, path, body, auth=True, _retry=True)
        return status, parsed

    # -- node operations ---------------------------------------------------
    def post_report(self, report: dict[str, Any]) -> dict[str, Any]:
        self._require("post_report")
        status, body = self._request("POST", "/v1/report", report)
        if status >= 400:
            raise PoolError(f"pool {self.name} report failed: HTTP {status} {str(body)[:200]}", status=status)
        return body if isinstance(body, dict) else {}

    def get_index(self, kind: str) -> dict[str, Any]:
        self._require("get_index")
        if kind not in INDEX_KINDS:
            raise ValueError(f"unknown index kind: {kind}")
        status, body = self._request("GET", f"/v1/index/{kind}", auth=False)
        if status == 404:
            raise PoolNotAvailable(f"pool {self.name} has no index '{kind}'", status=404)
        if status >= 400:
            raise PoolError(f"pool {self.name} index {kind} failed: HTTP {status}", status=status)
        items = []
        for item in (body or {}).get("items") or []:
            count = item.get("nodeCount")
            if not isinstance(count, (int, float)):
                count = item.get("addsThisWeek") or 0
            items.append({
                "mbid": str(item.get("mbid")),
                "count": count,
                "reason": item.get("reason") or "",
                "raw": item,
            })
        return {"week": (body or {}).get("week"), "items": items}

    def get_playlists(self) -> dict[str, Any]:
        """Pull the pool's canonical playlist data (published by the official node)."""
        self._require("get_playlists")
        status, body = self._request("GET", "/v1/playlists", auth=False)
        if status == 404:
            raise PoolNotAvailable(
                f"pool {self.name} does not publish playlist data yet (GET /v1/playlists missing)",
                status=404,
            )
        if status >= 400:
            raise PoolError(f"pool {self.name} playlists pull failed: HTTP {status}", status=status)
        return {
            "week": (body or {}).get("week"),
            "playlists": (body or {}).get("playlists") or [],
        }

    def get_whoami(self) -> dict[str, Any]:
        self._require("get_whoami")
        status, body = self._request("GET", "/v1/whoami", auth=True)
        if status == 404:
            raise PoolNotAvailable(f"pool {self.name} has no whoami endpoint", status=404)
        if status >= 400:
            raise PoolError(f"pool {self.name} whoami failed: HTTP {status}", status=status)
        return body if isinstance(body, dict) else {}

    # Backwards-friendly alias.
    def whoami(self) -> dict[str, Any]:
        return self.get_whoami()

    # -- admin operations (official node only) ------------------------------
    def post_aggregate(self, week: str | None = None) -> dict[str, Any]:
        self._require("post_aggregate")
        status, body = self._request("POST", "/v1/aggregate", {"week": week} if week else {})
        if status >= 400:
            raise PoolError(f"pool {self.name} aggregate failed: HTTP {status} {str(body)[:200]}", status=status)
        return body if isinstance(body, dict) else {}

    def publish_playlists(self, week: str | None, playlists: list[dict[str, Any]]) -> dict[str, Any]:
        self._require("publish_playlists")
        status, body = self._request("POST", "/v1/playlists", {"week": week, "playlists": playlists})
        if status == 404:
            raise PoolNotAvailable(
                f"pool {self.name} does not accept playlist publication yet (POST /v1/playlists missing)",
                status=404,
            )
        if status >= 400:
            raise PoolError(f"pool {self.name} playlist publish failed: HTTP {status} {str(body)[:200]}", status=status)
        return body if isinstance(body, dict) else {}
