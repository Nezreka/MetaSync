"""MetaSync web UI — Flask, server-rendered, no build step.

Pages: dashboard (/), SoulSync connection (/settings/soulsync), pools
(/pools), payload preview (/preview). Local setup protection: the first
visit creates an admin password (stored hashed in the settings store);
every page except /health then requires login. /health stays public so
Docker healthchecks work.
"""
from __future__ import annotations

import hmac
import json
import secrets
import time
from functools import wraps
from typing import Any

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .service import NodeService, PairingError, PairingRateLimited, check_pool_url

# POST routes SoulSync calls (no browser session, no form): CSRF-exempt.
MACHINE_POST_PATHS = frozenset({
    "/local/pair/request", "/local/pair/claim",
    "/local/handoff/pool-key", "/local/disconnect",
})

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 300


def create_app(service: NodeService) -> Flask:
    app = Flask(__name__)
    app.secret_key = service.store.ui_secret()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    store = service.store

    # CSRF: every browser form POST carries the session's token. The
    # session cookie alone is not enough — SameSite=Lax still sends it on
    # requests from any page on the same site, and SoulSync (port 8000) is
    # the same site as this UI (port 8790): a page there, or any other app
    # on this host, could otherwise post to /pools or /actions/dry-run as
    # the logged-in admin. The machine-to-machine routes SoulSync calls are
    # exempt: they authenticate with the local API key (or a pairing
    # code), never with the cookie. /local/pair/confirm is NOT one of them:
    # it is the dashboard's approve button.
    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def check_csrf():
        if request.method != "POST" or request.path in MACHINE_POST_PATHS:
            return None
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("csrf_token") or ""
        if not expected or not hmac.compare_digest(str(sent), str(expected)):
            abort(400, description="This form expired or did not come from this page. Reload and try again.")
        return None

    def password_set() -> bool:
        return bool(store.settings.get("ui_password_hash"))

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not password_set():
                return redirect(url_for("setup"))
            if not session.get("ui_admin"):
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    def local_key_required(view):
        """Bearer auth for the machine-to-machine /local/* surface."""

        @wraps(view)
        def wrapped(*args, **kwargs):
            auth = request.headers.get("Authorization") or ""
            token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
            if not service.verify_local_api_key(token):
                return {"ok": False, "error": "unauthorized"}, 401
            return view(*args, **kwargs)

        return wrapped

    def _json_body() -> dict[str, Any]:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    @app.get("/health")
    def health():
        return {"ok": True, "service": "metasync-node", "version": "0.2.0"}

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if password_set():
            return redirect(url_for("login"))
        if request.method == "POST":
            password = request.form.get("password", "")
            confirm = request.form.get("confirm", "")
            if len(password) < 8:
                flash("Use at least 8 characters.", "error")
            elif password != confirm:
                flash("Passwords do not match.", "error")
            else:
                store.settings["ui_password_hash"] = generate_password_hash(password)
                store.save_settings()
                session["ui_admin"] = True
                flash("Admin password set. This browser is now signed in.", "ok")
                return redirect(url_for("dashboard"))
        return render_template("setup.html")

    # Failed logins per client address, for a small brute-force brake: the
    # UI is often reachable from the whole LAN, and the password guards the
    # pool key and the SoulSync key.
    failed_logins: dict[str, list[float]] = {}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not password_set():
            return redirect(url_for("setup"))
        if request.method == "POST":
            client_addr = request.remote_addr or "?"
            now = time.monotonic()
            recent = [t for t in failed_logins.get(client_addr, []) if now - t < LOGIN_WINDOW_S]
            if len(recent) >= LOGIN_MAX_FAILURES:
                failed_logins[client_addr] = recent
                flash("Too many wrong passwords. Wait a few minutes and try again.", "error")
                return render_template("login.html"), 429
            password = request.form.get("password", "")
            if check_password_hash(store.settings["ui_password_hash"], password):
                failed_logins.pop(client_addr, None)
                session.clear()
                session["ui_admin"] = True
                return redirect(url_for("dashboard"))
            failed_logins[client_addr] = [*recent, now]
            flash("Wrong password.", "error")
        return render_template("login.html")

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        return render_template(
            "dashboard.html",
            status=service.status(),
            report_job=service.report_job(),
            local_key_once=session.pop("local_api_key_once", None),
        )

    @app.get("/api/report-job")
    @login_required
    def report_job_status():
        job = service.report_job()
        if job is None:
            return {"exists": False, "status": None}
        return {"exists": True, **job}

    @app.get("/api/pairing-status")
    @login_required
    def pairing_status():
        pairing = service.pending_pairing()
        if pairing is None:
            return {
                "exists": False,
                "session_id": None,
                "pairing_code": None,
                "status": None,
                "expires_at": None,
            }
        return {
            "exists": True,
            "session_id": pairing.get("session_id"),
            "pairing_code": pairing.get("pairing_code"),
            "status": pairing.get("status"),
            "expires_at": pairing.get("expires_at"),
        }

    @app.post("/local-keys/regenerate")
    @login_required
    def local_key_regenerate():
        # The raw key is carried to exactly one dashboard render, where
        # it is shown once with a copy button. Only its hash persists.
        session["local_api_key_once"] = service.issue_local_api_key()
        flash("New MetaSync local API key created. Copy it now — it will not be shown again.", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/local-keys/revoke")
    @login_required
    def local_key_revoke():
        service.revoke_local_api_key()
        session.pop("local_api_key_once", None)
        flash("MetaSync local API key revoked.", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/local/pair/confirm")
    @login_required
    def pair_confirm():
        session_id = request.form.get("session_id", "")
        approved = request.form.get("decision") == "approve"
        try:
            result = service.confirm_pairing(session_id, approved)
            flash(
                "Pairing request approved. SoulSync can now complete the connection."
                if result["status"] == "approved"
                else "Pairing request rejected.",
                "ok",
            )
        except PairingError:
            flash("That pairing request is no longer available.", "error")
        return redirect(url_for("dashboard"))

    # -- machine-to-machine local API (SoulSync -> MetaSync) -----------------
    @app.post("/local/pair/request")
    def pair_request():
        try:
            return service.request_pairing(requested_from=request.remote_addr)
        except PairingRateLimited:
            return {"ok": False, "error": "rate_limited"}, 429
        except PairingError:
            return {"ok": False, "error": "pairing_unavailable"}, 409

    @app.post("/local/pair/claim")
    def pair_claim():
        body = _json_body()
        try:
            return service.claim_pairing(
                str(body.get("session_id") or ""),
                str(body.get("pairing_code") or ""),
                str(body.get("soulsync_name") or ""),
                str(body.get("soulsync_url") or ""),
                str(body.get("soulsync_api_key") or ""),
            )
        except PairingError:
            return {"ok": False, "error": "unauthorized"}, 401

    @app.post("/local/handoff/pool-key")
    @local_key_required
    def local_handoff_pool_key():
        body = _json_body()
        try:
            return service.receive_pool_key(
                str(body.get("pool_url") or body.get("url") or ""),
                str(body.get("pool_api_key") or body.get("pool_key") or body.get("api_key") or ""),
                pool_name=str(body.get("pool_name") or body.get("name") or ""),
                pool_id=str(body["pool_id"]) if body.get("pool_id") else None,
            )
        except ValueError as err:
            return {"ok": False, "error": str(err)}, 400

    @app.get("/local/status")
    @local_key_required
    def local_status():
        return service.local_status()

    @app.post("/local/disconnect")
    @local_key_required
    def local_disconnect():
        body = _json_body()
        try:
            return service.disconnect_pool(str(body["pool_id"]) if body.get("pool_id") else None)
        except ValueError as err:
            return {"ok": False, "error": str(err)}, 404

    @app.post("/actions/report")
    @login_required
    def action_report():
        # The run happens on a background thread; the dashboard's live
        # progress card is where its status, steps, and errors show.
        before = service.report_job()
        job = service.start_report_job()
        if before and before.get("status") == "running" and before.get("id") == job.get("id"):
            flash("A report is already running. Its progress is on the dashboard.", "ok")
        else:
            flash("Report started. Its live progress is on the dashboard.", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/actions/refresh")
    @login_required
    def action_refresh():
        outcome = service.run_refresh()
        flash("Playlist refresh finished. See pool status on the dashboard.", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/actions/dry-run")
    @login_required
    def action_dry_run():
        store.settings["dry_run"] = request.form.get("dry_run") == "on"
        store.save_settings()
        flash(f"Dry run is now {'ON' if store.settings['dry_run'] else 'OFF'}.", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/actions/share-listening")
    @login_required
    def action_share_listening():
        store.settings["share_listening"] = request.form.get("share_listening") == "on"
        store.save_settings()
        flash(f"Listening signals are now {'ON' if store.settings['share_listening'] else 'OFF'}.", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/actions/share-churn")
    @login_required
    def action_share_churn():
        store.settings["share_churn"] = request.form.get("share_churn") == "on"
        store.save_settings()
        flash(f"Drop signals are now {'ON' if store.settings['share_churn'] else 'OFF'}.", "ok")
        return redirect(url_for("dashboard"))

    @app.route("/settings/soulsync", methods=["GET", "POST"])
    @login_required
    def soulsync_settings():
        current = store.settings.get("soulsync") or {}
        if request.method == "POST":
            action = request.form.get("action", "save")
            if action == "disconnect":
                # Local half of the unlink: drop the stored SoulSync address
                # and key. SoulSync's own Disconnect revokes the key on its
                # side; the flash says so rather than implying a full unlink.
                store.settings["soulsync"] = {"url": "", "api_key": ""}
                store.save_settings()
                flash(
                    "Disconnected here — MetaSync no longer holds a SoulSync address or key. "
                    "To unlink fully, also disconnect in SoulSync's MetaSync settings, which revokes the key.",
                    "ok",
                )
            elif action == "test_saved":
                try:
                    test_result = service.test_soulsync()
                    flash(f"Connected. SoulSync answered with {test_result['watchlist_artists']} watchlist artists.", "ok")
                except Exception as err:
                    flash(f"Connection test failed: {err}", "error")
            else:
                url = request.form.get("url", "").strip().rstrip("/")
                api_key = request.form.get("api_key", "").strip()
                store.settings["soulsync"] = {
                    "url": url,
                    "api_key": api_key or current.get("api_key", ""),
                }
                store.save_settings()
                if action == "test":
                    try:
                        test_result = service.test_soulsync()
                        flash(f"Connected. SoulSync answered with {test_result['watchlist_artists']} watchlist artists.", "ok")
                    except Exception as err:
                        flash(f"Connection test failed: {err}", "error")
                else:
                    flash("SoulSync connection saved.", "ok")
            current = store.settings.get("soulsync") or {}
        return render_template(
            "soulsync.html",
            current=current,
            has_key=bool(current.get("api_key")),
            connected=bool(current.get("url") and current.get("api_key")),
            status=service.status(),
            local_key_once=session.pop("local_api_key_once", None),
        )

    @app.route("/pools", methods=["GET", "POST"])
    @login_required
    def pools():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            url = request.form.get("url", "").strip().rstrip("/")
            api_key = request.form.get("api_key", "").strip()
            role = request.form.get("role", "node")
            pool_id = request.form.get("pool_id", "").strip()
            if role not in ("node", "admin"):
                role = "node"
            if pool_id:
                # Labelled manual fallback: replace an existing pool's key
                # (and URL if supplied), reset its token lifecycle, and
                # immediately attempt the §3A.4 exchange.
                existing = next(
                    (p for p in store.settings.get("pools") or [] if p.get("id") == pool_id),
                    None,
                )
                if existing is None:
                    flash("That pool no longer exists.", "error")
                else:
                    try:
                        result = service.receive_pool_key(
                            url or str(existing.get("url") or ""),
                            api_key or str(existing.get("api_key") or ""),
                            pool_name=name or str(existing.get("name") or "Pool"),
                            pool_id=pool_id,
                        )
                        existing["role"] = role
                        store.save_settings()
                        flash(
                            f"Pool key saved for '{existing.get('name')}'. Exchange: {result['exchange_status']}.",
                            "ok",
                        )
                    except ValueError as err:
                        flash(str(err), "error")
            elif not name or not url:
                flash("A pool needs at least a name and a URL.", "error")
            elif (url_error := _pool_url_error(url)) is not None:
                flash(url_error, "error")
            else:
                pool_id = secrets.token_hex(4)
                store.settings.setdefault("pools", []).append({
                    "id": pool_id, "name": name, "url": url,
                    "api_key": api_key, "role": role, "enabled": True,
                })
                store.save_settings()
                flash(f"Pool '{name}' added.", "ok")
            return redirect(url_for("pools"))
        return render_template("pools.html", pools=service.status()["pools"])

    def _pool_url_error(url: str) -> str | None:
        try:
            check_pool_url(url, allow_insecure=service.allow_insecure_pools)
        except ValueError as err:
            return str(err)
        return None

    @app.post("/pools/<pool_id>/toggle")
    @login_required
    def pool_toggle(pool_id: str):
        for pool in store.settings.get("pools") or []:
            if pool.get("id") == pool_id:
                pool["enabled"] = not pool.get("enabled", True)
        store.save_settings()
        return redirect(url_for("pools"))

    @app.post("/pools/<pool_id>/delete")
    @login_required
    def pool_delete(pool_id: str):
        store.settings["pools"] = [p for p in store.settings.get("pools") or [] if p.get("id") != pool_id]
        (store.state.get("pool_auth") or {}).pop(pool_id, None)
        store.save_settings()
        store.save_state()
        return redirect(url_for("pools"))

    @app.get("/preview")
    @login_required
    def preview():
        # A snapshot of the LAST completed run, never a fresh computation:
        # computing here blocked the page on a full library read plus
        # MusicBrainz lookups and raced any report job already running.
        saved = service.store.state.get("previews") or {}
        by_id = {p["id"]: p for p in service.pools()}
        previews: dict[str, Any] = {}
        for pid, payload in saved.items():
            pool = by_id.get(pid) or {}
            # Bookkeeping keys the run stored beside the payload are not part
            # of what gets POSTed: the page promises the exact payload.
            previews[str(pool.get("name") or pid)] = {
                k: v for k, v in payload.items() if k not in ("computed_at", "sent_at")
            }
        pretty = {name: json.dumps(payload, indent=2) for name, payload in previews.items()}
        return render_template(
            "preview.html", previews=pretty,
            diagnostics=service.store.state.get("last_diagnostics"),
            error=None, dry_run=service.dry_run,
            report_job=service.report_job(),
        )

    return app
