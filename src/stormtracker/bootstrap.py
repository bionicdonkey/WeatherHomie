"""Bootstrap config from environment / .env. Everything else lives in the DB."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import __version__


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


@dataclass(frozen=True)
class Bootstrap:
    database_url: str
    data_dir: Path
    master_key: str
    admin_password: str | None = None      # optional: creates the admin on first start
    admin_username: str = "admin"
    trusted_auth_header: str | None = None  # e.g. X-authentik-username (behind a proxy)
    trusted_proxies: tuple[str, ...] = ()
    cookie_secure: bool = True
    user_agent: str = f"StormTracker/{__version__} (personal, non-commercial; self-hosted)"

    @classmethod
    def from_env(cls) -> "Bootstrap":
        load_dotenv()
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        key = os.environ.get("MASTER_KEY", "")
        if len(key) < 32:
            raise SystemExit(
                "MASTER_KEY must be set (>=32 chars). Generate one with: "
                "python -m stormtracker.cli generate-key")
        return cls(
            database_url=os.environ.get("DATABASE_URL", f"sqlite+aiosqlite:///{data_dir}/stormtracker.db"),
            data_dir=data_dir,
            master_key=key,
            admin_password=os.environ.get("ADMIN_PASSWORD") or None,
            admin_username=os.environ.get("ADMIN_USERNAME", "admin"),
            trusted_auth_header=os.environ.get("TRUSTED_AUTH_HEADER") or None,
            trusted_proxies=tuple(x for x in os.environ.get("TRUSTED_PROXIES", "").split(",") if x),
            cookie_secure=os.environ.get("COOKIE_SECURE", "true").lower() != "false",
        )
