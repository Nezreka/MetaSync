"""MetaSync web UI — Flask, server-rendered, no build step.

Pages: dashboard (/), SoulSync connection (/settings/soulsync), pools
(/pools), payload preview (/preview). Local setup protection: the first
visit creates an admin password (stored hashed in the settings store);
every page except /health then requires login. /health stays public so
Docker healthchecks work.
"""
from __future__ import annotations

import json
import secrets
from functools import wraps
from typing import Any

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .service import NodeService, PairingError, PairingRateLimited


def create_app(service: NodeService) -> Flask:
    app = Flask(__name__)
    app.secret_key = service.store.ui_secret()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    store = service.store

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

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not password_set():
            return redirect(url_for("setup"))
        if request.method == "POST":
            password = request.form.get("password", "")
            if check_password_hash(store.settings["ui_password_hash"], password):
                session["ui_admin"] = True
                return redirect(url_for("dashboard"))
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
            local_key_once=session.pop("local_api_key_once", None),
        )

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
            return service.request_pairing()
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
                str(body.get("pool_api_key") or body.get("api_key") or ""),
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
        outcome = service.run_report()
        if outcome.get("error"):
            flash(f"Report failed: {outcome['error']}", "error")
        elif service.dry_run:
            flash(f"Preview computed for {outcome['week']} (dry run: nothing was sent).", "ok")
        else:
            flash(f"Report for {outcome['week']} sent to every enabled pool.", "ok")
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

    @app.route("/settings/soulsync", methods=["GET", "POST"])
    @login_required
    def soulsync_settings():
        current = store.settings.get("soulsync") or {}
        test_result: dict[str, Any] | None = None
        if request.method == "POST":
            url = request.form.get("url", "").strip().rstrip("/")
            api_key = request.form.get("api_key", "").strip()
            store.settings["soulsync"] = {
                "url": url,
                "api_key": api_key or current.get("api_key", ""),
            }
            store.save_settings()
            if request.form.get("action") == "test":
                try:
                    test_result = service.test_soulsync()
                    flash(f"Connected. SoulSync answered with {test_result['watchlist_artists']} watchlist artists.", "ok")
                except Exception as err:
                    flash(f"Connection test failed: {err}", "error")
            else:
                flash("SoulSync connection saved.", "ok")
            current = store.settings["soulsync"]
        return render_template(
            "soulsync.html",
            current=current,
            has_key=bool(current.get("api_key")),
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
        previews: dict[str, Any] = {}
        diagnostics = None
        error = None
        enabled = service.pools()
        if enabled and (store.settings.get("soulsync") or {}).get("url"):
            try:
                report = service.compute_report()
                diagnostics = report["diagnostics"]
                for pool in enabled:
                    previews[pool["name"]] = service.payload_for_pool(pool, report)
            except Exception as err:
                error = str(err)
                previews = {
                    (p.get("name") or p["id"]): payload
                    for p in enabled
                    for payload in [service.store.state.get("previews", {}).get(p["id"])]
                    if payload
                }
        else:
            saved = service.store.state.get("previews") or {}
            previews = {pid: payload for pid, payload in saved.items()}
        pretty = {name: json.dumps(payload, indent=2) for name, payload in previews.items()}
        return render_template(
            "preview.html", previews=pretty, diagnostics=diagnostics,
            error=error, dry_run=service.dry_run,
        )

    return app
