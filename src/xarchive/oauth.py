"""OAuth 2.0 PKCE user-context login on 127.0.0.1 (no tracking, no public bind)."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from xarchive.config import OAUTH2_SCOPES, Config, save_oauth2_tokens

AUTHORIZE_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": OAUTH2_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q)


def exchange_code(
    cfg: Config,
    *,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "client_id": cfg.oauth2_client_id,
    }
    auth = None
    if cfg.oauth2_client_secret:
        auth = (cfg.oauth2_client_id or "", cfg.oauth2_client_secret)
    token_url = cfg.api_base.rstrip("/") + "/2/oauth2/token"
    # Prefer api.x.com; fall back documented twitter host via cfg.api_base.
    resp = httpx.post(
        token_url if cfg.api_base else TOKEN_URL,
        data=data,
        auth=auth,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"token exchange failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


def refresh_tokens(cfg: Config, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """Exchange a refresh token for a new access token. Updates cfg and credentials.json."""
    if not cfg.oauth2_client_id:
        raise RuntimeError("set X_OAUTH2_CLIENT_ID (see .env.example)")
    if not cfg.oauth2_refresh_token:
        raise RuntimeError("no OAuth2 refresh token stored")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": cfg.oauth2_refresh_token,
        "client_id": cfg.oauth2_client_id,
    }
    auth = None
    if cfg.oauth2_client_secret:
        auth = (cfg.oauth2_client_id or "", cfg.oauth2_client_secret)
    token_url = cfg.api_base.rstrip("/") + "/2/oauth2/token"
    close = False
    http = client
    if http is None:
        http = httpx.Client(timeout=30.0)
        close = True
    try:
        resp = http.post(
            token_url,
            data=data,
            auth=auth,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30.0,
        )
    finally:
        if close:
            http.close()
    if resp.status_code >= 400:
        raise RuntimeError(f"token refresh failed ({resp.status_code}): {resp.text[:500]}")
    tokens = resp.json()
    access = tokens.get("access_token")
    if not access:
        raise RuntimeError("token refresh missing access_token")
    new_refresh = tokens.get("refresh_token") or cfg.oauth2_refresh_token
    save_oauth2_tokens(
        cfg,
        access,
        new_refresh,
        expires_in=tokens.get("expires_in"),
    )
    return tokens


class _Callback:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.query: dict[str, list[str]] = {}


def run_pkce_login(cfg: Config, *, host: str = "127.0.0.1", port: int = 8787) -> str:
    if host not in LOCAL_HOSTS:
        raise ValueError("OAuth callback must bind 127.0.0.1")
    if not cfg.oauth2_client_id:
        raise RuntimeError("set X_OAUTH2_CLIENT_ID (see .env.example)")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://{host}:{port}/callback"
    url = authorize_url(cfg.oauth2_client_id, redirect_uri, challenge, state)
    box = _Callback()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            box.query = urllib.parse.parse_qs(parsed.query)
            body = b"<html><body>xarchive: you can close this tab.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            box.event.set()

        def log_message(self, fmt: str, *args: object) -> None:
            return

    httpd = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print("Open this URL in a browser to authorize xarchive:")
    print(url)
    print("Waiting for 127.0.0.1 callback…")
    ok = box.event.wait(timeout=300)
    httpd.shutdown()
    if not ok:
        raise RuntimeError("timed out waiting for OAuth callback")
    if box.query.get("state", [None])[0] != state:
        raise RuntimeError("OAuth state mismatch")
    if box.query.get("error"):
        raise RuntimeError("authorization denied")
    code = (box.query.get("code") or [None])[0]
    if not code:
        raise RuntimeError("no authorization code returned")
    tokens = exchange_code(cfg, code=code, redirect_uri=redirect_uri, verifier=verifier)
    access = tokens.get("access_token")
    if not access:
        raise RuntimeError("token response missing access_token")
    save_oauth2_tokens(
        cfg,
        access,
        tokens.get("refresh_token"),
        expires_in=tokens.get("expires_in"),
    )
    return "saved"
