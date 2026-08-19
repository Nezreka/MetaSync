"""Configuration, read from the environment or a local ``.env``.

Deliberately tiny and dependency-free. The API key never appears in code, in
logs, or in any file MetaSync writes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

DEFAULT_ENV_FILE = ".env"


def load_env_file(path: Path) -> Dict[str, str]:
    """Parse a minimal KEY=VALUE file. Blank lines and ``#`` comments ignored."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Config:
    soulsync_url: str = "http://localhost:8008"
    soulsync_api_key: str = ""
    data_dir: Path = Path("data")
    page_size: int = 500

    @property
    def store_path(self) -> Path:
        return self.data_dir / "metasync.db"

    @property
    def node_key_path(self) -> Path:
        return self.data_dir / "node.key"

    @classmethod
    def load(cls, env_file: Path | str = DEFAULT_ENV_FILE) -> "Config":
        """Environment wins over the .env file, so a container can override."""
        values = load_env_file(Path(env_file))
        values.update({k: v for k, v in os.environ.items() if k.startswith("METASYNC_") or k.startswith("SOULSYNC_")})

        def get(name: str, default: str) -> str:
            return values.get(name) or default

        return cls(
            soulsync_url=get("SOULSYNC_URL", "http://localhost:8008").rstrip("/"),
            soulsync_api_key=get("SOULSYNC_API_KEY", ""),
            data_dir=Path(get("METASYNC_DATA_DIR", "data")),
            page_size=int(get("METASYNC_PAGE_SIZE", "500")),
        )

    def require_api_key(self) -> str:
        if not self.soulsync_api_key:
            raise RuntimeError(
                "SOULSYNC_API_KEY is not set. Put it in .env (which is gitignored):\n"
                "  SOULSYNC_URL=http://localhost:8008\n"
                "  SOULSYNC_API_KEY=sk_..."
            )
        return self.soulsync_api_key
