"""OAuth 2.0 token refresh. No live X."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from xarchive.config import load_config, save_oauth2_tokens
from xarchive.oauth import refresh_tokens
from xarchive.xclient import LiveXClient, XApiError


def test_save_oauth2_tokens_stores_expires_at_and_updates_cfg(cfg):
    before = time.time()
    save_oauth2_tokens(cfg, "access-1", "refresh-1", expires_in=7200)
    payload = json.loads(cfg.credentials_path.read_text(encoding="utf-8"))
    assert payload["access_token"] == "access-1"
    assert payload["refresh_token"] == "refresh-1"
    assert payload["expires_at"] == pytest.approx(before + 7200, abs=2)
    assert cfg.oauth2_access_token == "access-1"
    assert cfg.oauth2_refresh_token == "refresh-1"
    assert cfg.oauth2_expires_at == pytest.approx(before + 7200, abs=2)


def test_refresh_tokens_persists_rotated_pair(cfg):
    cfg.oauth2_client_id = "client"
    cfg.oauth2_refresh_token = "refresh-old"
    cfg.oauth2_access_token = "access-old"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/2/oauth2/token")
        body = request.content.decode()
        assert "grant_type=refresh_token" in body
        assert "refresh_token=refresh-old" in body
        return httpx.Response(
            200,
            json={
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "expires_in": 7200,
                "token_type": "bearer",
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    tokens = refresh_tokens(cfg, client=client)
    assert tokens["access_token"] == "access-new"
    assert cfg.oauth2_access_token == "access-new"
    assert cfg.oauth2_refresh_token == "refresh-new"
    saved = json.loads(cfg.credentials_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "access-new"
    assert saved["refresh_token"] == "refresh-new"


def test_live_client_401_refreshes_and_retries(cfg):
    cfg.oauth2_client_id = "client"
    cfg.oauth2_access_token = "old-token"
    cfg.oauth2_refresh_token = "refresh-1"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/2/oauth2/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "new-token",
                    "refresh_token": "refresh-2",
                    "expires_in": 7200,
                },
            )
        auth = request.headers.get("Authorization")
        if auth == "Bearer old-token":
            return httpx.Response(401, text="expired")
        if auth == "Bearer new-token":
            return httpx.Response(200, json={"data": {"id": "5166878", "username": "u"}})
        return httpx.Response(400, text=f"unexpected auth {auth}")

    client = LiveXClient(cfg=cfg, transport=httpx.MockTransport(handler))
    me = client.get_me()
    assert me["id"] == "5166878"
    assert cfg.oauth2_access_token == "new-token"
    assert "POST /2/oauth2/token" in calls
    client.close()


def test_live_client_401_without_refresh_token_raises(cfg):
    cfg.oauth2_access_token = "old-token"
    cfg.oauth2_refresh_token = None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="expired")

    client = LiveXClient(cfg=cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(XApiError) as ei:
        client.get_me()
    assert ei.value.status_code == 401
    client.close()


def test_default_data_dir_is_checkout_data(monkeypatch):
    monkeypatch.delenv("XARCHIVE_DATA_DIR", raising=False)
    from xarchive.config import default_data_dir, project_root

    root = project_root()
    assert (root / "pyproject.toml").is_file()
    assert default_data_dir() == (root / "data").resolve()


def test_load_config_reads_expires_at(data_dir):
    path = data_dir / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_at": 1_700_000_000,
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(data_dir)
    assert cfg.oauth2_access_token == "a"
    assert cfg.oauth2_refresh_token == "r"
    assert cfg.oauth2_expires_at == 1_700_000_000.0
