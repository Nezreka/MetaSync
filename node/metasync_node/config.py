"""Runtime configuration from env. Secrets come from env or the settings
store (edited through the UI) — never from code."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    state_dir: str = "/data"
    port: int = 8790  # SoulSync uses 8000; MetaSync's UI lives here instead.
    musicbrainz_base_url: str = "https://musicbrainz.org/ws/2"
    musicbrainz_user_agent: str = "MetaSync-Node/0.1 (https://github.com/Nezreka/MetaSync)"
    musicbrainz_min_interval_s: float = 1.1  # MusicBrainz asks for <= 1 req/sec
    playlist_max_items: int = 20
    playlist_tracks_per_item: int = 3
    library_max_pages: int = 5000
    release_resolution_cap: int = 1000
    index_stale_days: int = 7
    scheduler_tick_s: int = 60
    refresh_interval_h: int = 24
    http_timeout_s: int = 15


def _int(env: dict[str, str], name: str, default: int) -> int:
    try:
        value = int(env.get(name, "") or default)
        return value if value > 0 else default
    except ValueError:
        return default


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    return Config(
        state_dir=env.get("METASYNC_STATE_DIR", "/data"),
        port=_int(env, "METASYNC_PORT", 8790),
        musicbrainz_base_url=env.get("MUSICBRAINZ_BASE_URL", "https://musicbrainz.org/ws/2").rstrip("/"),
        musicbrainz_user_agent=env.get(
            "MUSICBRAINZ_USER_AGENT", "MetaSync-Node/0.1 (https://github.com/Nezreka/MetaSync)"
        ),
        playlist_max_items=_int(env, "PLAYLIST_MAX_ITEMS", 20),
        playlist_tracks_per_item=_int(env, "PLAYLIST_TRACKS_PER_ITEM", 3),
        library_max_pages=_int(env, "LIBRARY_MAX_PAGES", 5000),
        release_resolution_cap=_int(env, "RELEASE_RESOLUTION_CAP", 1000),
        index_stale_days=_int(env, "INDEX_STALE_DAYS", 7),
        http_timeout_s=_int(env, "HTTP_TIMEOUT_MS", 15_000) // 1000 or 15,
    )
