"""Data-dir and credentials. Tokens are never printed."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

DEFAULT_API_BASE = "https://api.x.com"
OAUTH2_SCOPES = "tweet.read tweet.write users.read offline.access"


def _redact(value: str | None) -> str:
    if not value:
        return ""
    return "***"


@dataclass
class Config:
    data_dir: Path
    api_base: str = DEFAULT_API_BASE
    oauth2_client_id: str | None = None
    oauth2_client_secret: str | None = None
    oauth2_access_token: str | None = None
    oauth2_refresh_token: str | None = None
    oauth2_expires_at: float | None = None
    oauth1_api_key: str | None = None
    oauth1_api_secret: str | None = None
    oauth1_access_token: str | None = None
    oauth1_access_token_secret: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "xarchive.sqlite"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "credentials.json"

    @property
    def serve_log_path(self) -> Path:
        return self.data_dir / "serve.log"

    def has_user_auth(self) -> bool:
        if self.oauth2_access_token:
            return True
        return bool(
            self.oauth1_api_key
            and self.oauth1_api_secret
            and self.oauth1_access_token
            and self.oauth1_access_token_secret
        )

    def __repr__(self) -> str:  # noqa: D105 — keep secrets out of logs
        return (
            f"Config(data_dir={self.data_dir!r}, api_base={self.api_base!r}, "
            f"oauth2_client_id={_redact(self.oauth2_client_id)!r}, "
            f"oauth2_access_token={_redact(self.oauth2_access_token)!r}, "
            f"oauth1_api_key={_redact(self.oauth1_api_key)!r})"
        )


def project_root() -> Path:
    """Checkout that holds pyproject.toml, so ``data/`` and ``.venv`` move with the code.

    Editable installs point ``__file__`` at ``src/xarchive``. A venv created inside
    the checkout (``.venv/lib/.../site-packages``) still has ``pyproject.toml`` above it.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return here.parents[2]


def default_data_dir() -> Path:
    env = os.environ.get("XARCHIVE_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return project_root() / "data"


def load_config(data_dir: str | Path | None = None) -> Config:
    path = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()
    path.mkdir(parents=True, exist_ok=True)

    root = project_root()
    load_dotenv()
    load_dotenv(root / ".env")
    load_dotenv(path / ".env")
    load_dotenv(path / "config.env")

    creds: dict[str, Any] = {}
    cred_file = path / "credentials.json"
    if cred_file.is_file():
        try:
            creds = json.loads(cred_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            creds = {}

    def _env(*names: str, cred: str | None = None) -> str | None:
        for name in names:
            val = os.environ.get(name)
            if val:
                return val
        if cred and creds.get(cred):
            return str(creds[cred])
        return None

    return Config(
        data_dir=path,
        api_base=(os.environ.get("X_API_BASE") or DEFAULT_API_BASE).rstrip("/"),
        oauth2_client_id=_env("X_OAUTH2_CLIENT_ID"),
        oauth2_client_secret=_env("X_OAUTH2_CLIENT_SECRET"),
        oauth2_access_token=_env("X_OAUTH2_ACCESS_TOKEN", cred="access_token"),
        oauth2_refresh_token=_env("X_OAUTH2_REFRESH_TOKEN", cred="refresh_token"),
        oauth2_expires_at=_parse_expires_at(creds.get("expires_at")),
        oauth1_api_key=_env("X_API_KEY", "TWITTER_API_KEY", "CONSUMER_KEY"),
        oauth1_api_secret=_env("X_API_SECRET", "TWITTER_API_SECRET", "CONSUMER_SECRET"),
        oauth1_access_token=_env("X_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN"),
        oauth1_access_token_secret=_env(
            "X_ACCESS_TOKEN_SECRET", "TWITTER_ACCESS_TOKEN_SECRET"
        ),
    )


def _parse_expires_at(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def save_oauth2_tokens(
    cfg: Config,
    access_token: str,
    refresh_token: str | None,
    *,
    expires_in: int | float | None = None,
) -> None:
    expires_at = None
    if expires_in is not None:
        try:
            expires_at = time.time() + float(expires_in)
        except (TypeError, ValueError):
            expires_at = None
    payload = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "scope": OAUTH2_SCOPES,
        "expires_at": expires_at,
    }
    path = cfg.credentials_path
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    cfg.oauth2_access_token = access_token
    cfg.oauth2_refresh_token = refresh_token
    cfg.oauth2_expires_at = expires_at
