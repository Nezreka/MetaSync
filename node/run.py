#!/usr/bin/env python3
"""MetaSync node entry point — bare metal, in the spirit of SoulSync's dev.py.

    python3 run.py              serve the UI + scheduler (default)
    python3 run.py serve        same as above
    python3 run.py report       compute + send this week's report (dry-run aware), then exit
    python3 run.py refresh      pull playlist data + update local playlists, then exit
    python3 run.py preview      print the exact report payload, send nothing

Config comes from env (see .env.example); connections are managed in the
UI and stored under METASYNC_STATE_DIR (default /data — on bare metal,
point it somewhere writable, e.g. METASYNC_STATE_DIR=./data).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from metasync_node.app import create_app  # noqa: E402
from metasync_node.config import load_config  # noqa: E402
from metasync_node.service import NodeService  # noqa: E402
from metasync_node.store import default_pool_from_env  # noqa: F401  (re-exported for tests)


def build_service() -> NodeService:
    config = load_config()
    return NodeService(config, env=dict(os.environ))


def scheduler_loop(service: NodeService, stop: threading.Event) -> None:
    """Weekly report + periodic playlist refresh, each leg isolated."""
    while not stop.is_set():
        try:
            now = datetime.now(timezone.utc)
            # Once per closed week, after this node's slot, with a back-off
            # after a failure (service.report_due) — never every tick, and
            # never alongside a report the UI already started.
            job = service.report_job() or {}
            if job.get("status") != "running" and service.report_due(now):
                service.run_report(now)
            last_refresh = service.store.state.get("last_refresh_at")
            refresh_due = True
            if last_refresh:
                try:
                    refresh_due = (now - datetime.fromisoformat(last_refresh)).total_seconds() >= service.config.refresh_interval_h * 3600
                except ValueError:
                    refresh_due = True
            if refresh_due and service.pools():
                service.run_refresh(now)
        except Exception as err:  # the scheduler itself must never die
            service.store.record_error("scheduler", err)
            service.store.save_state()
        stop.wait(service.config.scheduler_tick_s)


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "serve"
    service = build_service()
    if command == "preview":
        report = service.compute_report()
        for pool in service.pools():
            print(json.dumps(service.payload_for_pool(pool, report), indent=2))
        print("diagnostics:", json.dumps(report["diagnostics"]), file=sys.stderr)
        return 0
    if command == "report":
        outcome = service.run_report()
        print(json.dumps(outcome, indent=2, default=str))
        return 1 if outcome.get("error") else 0
    if command == "refresh":
        outcome = service.run_refresh()
        print(json.dumps(outcome, indent=2, default=str))
        return 0
    if command == "serve":
        stop = threading.Event()
        thread = threading.Thread(target=scheduler_loop, args=(service, stop), daemon=True)
        thread.start()
        app = create_app(service)
        print(f"MetaSync node UI on http://0.0.0.0:{service.config.port} (dry_run={service.dry_run})")
        try:
            app.run(host="0.0.0.0", port=service.config.port, threaded=True)
        finally:
            stop.set()
        return 0
    print(f"unknown command: {command} (expected serve|report|refresh|preview)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
