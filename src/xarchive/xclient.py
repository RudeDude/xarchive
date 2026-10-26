"""Official X API v2 client (user-context). No GraphQL, no cookies."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import httpx

from xarchive.config import Config

DELETE_TWEET_PATH = "/2/tweets/{id}"
UNRETWEET_PATH = "/2/users/{user_id}/retweets/{source_tweet_id}"
ME_PATH = "/2/users/me"
TIMELINE_PATH = "/2/users/{id}/tweets"
# Owned-account timeline is capped around 3,200 posts. Never use search/all as history.
TIMELINE_CAP = 3200
TIMELINE_PAGE_SIZE = 100


class XApiError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        retry_after: float | None = None,
        rate_limit_remaining: int | None = None,
        rate_limit_reset: int | None = None,
        path: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        self.rate_limit_remaining = rate_limit_remaining
        self.rate_limit_reset = rate_limit_reset
        self.path = path


class XClientProtocol(Protocol):
    user_id: str | None

    def delete_tweet(self, tweet_id: str) -> None: ...
    def unretweet(self, source_tweet_id: str) -> None: ...
    def get_me(self) -> dict[str, Any]: ...
    def user_timeline(
        self,
        since_id: str | None = None,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]: ...
    def lookup_tweets(self, ids: list[str]) -> list[dict[str, Any]]: ...


def _oauth1_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
) -> str:
    parsed = urllib.parse.urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    sig_params = {**query, **oauth}
    encoded = "&".join(
        f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(sig_params.items())
    )
    base = "&".join(
        [
            method.upper(),
            urllib.parse.quote(base_url, safe=""),
            urllib.parse.quote(encoded, safe=""),
        ]
    )
    key = f"{urllib.parse.quote(consumer_secret, safe='')}&{urllib.parse.quote(token_secret, safe='')}"
    digest = hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    import base64

    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    parts = ", ".join(
        f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
        for k, v in sorted(oauth.items())
    )
    return f"OAuth {parts}"


def _parse_retry_after(resp: httpx.Response) -> float | None:
    ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
    if not ra:
        return None
    try:
        return float(ra)
    except ValueError:
        return None


def _rate_headers(resp: httpx.Response) -> tuple[int | None, int | None]:
    rem = resp.headers.get("x-rate-limit-remaining")
    reset = resp.headers.get("x-rate-limit-reset")
    try:
        rem_i = int(rem) if rem is not None else None
    except ValueError:
        rem_i = None
    try:
        reset_i = int(reset) if reset is not None else None
    except ValueError:
        reset_i = None
    return rem_i, reset_i


@dataclass
class LiveXClient:
    """Official REST only. Bind this to tests via a fake instead — never hit live X in pytest."""

    cfg: Config
    user_id: str | None = None
    timeout: float = 30.0
    rate_limit_remaining: int | None = None
    rate_limit_reset: int | None = None
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    _http: httpx.Client | None = field(default=None, repr=False)
    _refresh_attempted: bool = field(default=False, repr=False)

    def _client(self) -> httpx.Client:
        if self._http is None:
            kwargs: dict[str, Any] = {"base_url": self.cfg.api_base, "timeout": self.timeout}
            if self.transport is not None:
                kwargs["transport"] = self.transport
            self._http = httpx.Client(**kwargs)
        return self._http

    def _token_expired(self) -> bool:
        exp = self.cfg.oauth2_expires_at
        if exp is None or not self.cfg.oauth2_refresh_token:
            return False
        return time.time() >= (exp - 60)

    def _try_refresh(self) -> bool:
        if self._refresh_attempted:
            return False
        if not self.cfg.oauth2_refresh_token or not self.cfg.oauth2_client_id:
            return False
        self._refresh_attempted = True
        from xarchive.oauth import refresh_tokens

        try:
            refresh_tokens(self.cfg, client=self._client())
        except (RuntimeError, httpx.HTTPError):
            return False
        return bool(self.cfg.oauth2_access_token)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _auth_headers(self, method: str, url: str) -> dict[str, str]:
        if self.cfg.oauth2_access_token:
            return {"Authorization": f"Bearer {self.cfg.oauth2_access_token}"}
        if (
            self.cfg.oauth1_api_key
            and self.cfg.oauth1_api_secret
            and self.cfg.oauth1_access_token
            and self.cfg.oauth1_access_token_secret
        ):
            return {
                "Authorization": _oauth1_header(
                    method,
                    url,
                    self.cfg.oauth1_api_key,
                    self.cfg.oauth1_api_secret,
                    self.cfg.oauth1_access_token,
                    self.cfg.oauth1_access_token_secret,
                )
            }
        raise XApiError(401, "no X API user-context credentials configured")

    def request(self, method: str, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        if self.cfg.oauth2_access_token and self._token_expired():
            self._try_refresh()
        url_path = path if path.startswith("http") else f"{self.cfg.api_base}{path}"
        headers = self._auth_headers(method, url_path if not params else url_path)
        # Recompute OAuth1 signature with query params included.
        if params and not self.cfg.oauth2_access_token:
            q = urllib.parse.urlencode(params)
            full = f"{self.cfg.api_base}{path}?{q}" if path.startswith("/") else f"{path}?{q}"
            headers = self._auth_headers(method, full)
        resp = self._client().request(method, path, params=params, headers=headers)
        rem, reset = _rate_headers(resp)
        if rem is not None:
            self.rate_limit_remaining = rem
        if reset is not None:
            self.rate_limit_reset = reset
        if resp.status_code == 401 and self.cfg.oauth2_refresh_token and self._try_refresh():
            return self.request(method, path, params)
        if resp.status_code >= 400:
            retry = _parse_retry_after(resp)
            body = resp.text[:500]
            raise XApiError(
                resp.status_code,
                body or resp.reason_phrase,
                retry_after=retry,
                rate_limit_remaining=rem,
                rate_limit_reset=reset,
                path=path,
            )
        self._refresh_attempted = False
        return resp

    def delete_tweet(self, tweet_id: str) -> None:
        self.request("DELETE", f"/2/tweets/{tweet_id}")

    def unretweet(self, source_tweet_id: str) -> None:
        uid = self.ensure_user_id()
        self.request("DELETE", f"/2/users/{uid}/retweets/{source_tweet_id}")

    def get_me(self) -> dict[str, Any]:
        resp = self.request("GET", ME_PATH, params={"user.fields": "id,username"})
        data = resp.json().get("data") or {}
        if data.get("id"):
            self.user_id = str(data["id"])
        return data

    def ensure_user_id(self) -> str:
        if self.user_id:
            return self.user_id
        me = self.get_me()
        if not me.get("id"):
            raise XApiError(401, "could not resolve authenticated user id")
        return str(me["id"])

    def user_timeline(
        self,
        since_id: str | None = None,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """GET /2/users/:id/tweets — optional incremental fill, ~3200 cap.

        Never uses search/all. Owned-read only.
        The user-tweets endpoint does not return a total, so the pull count
        announced up front is the maximum (cap / page size). Each page then
        reports whether another pull follows.
        """

        def say(msg: str) -> None:
            if on_progress is not None:
                on_progress(msg)

        uid = self.ensure_user_id()
        tweets: list[dict[str, Any]] = []
        pagination_token = None
        fields = (
            "created_at,referenced_tweets,entities,attachments,lang,source,"
            "note_tweet,public_metrics"
        )
        max_pages = max(1, (TIMELINE_CAP + TIMELINE_PAGE_SIZE - 1) // TIMELINE_PAGE_SIZE)
        since_label = since_id if since_id else "none"
        say(
            "Timeline pulls to make: at most "
            f"{max_pages} ({TIMELINE_PAGE_SIZE} tweets each, cap {TIMELINE_CAP}, "
            f"since_id={since_label})."
        )
        page = 0
        while len(tweets) < TIMELINE_CAP:
            page += 1
            say(f"Pull {page} of up to {max_pages}…")
            params: dict[str, Any] = {
                "max_results": TIMELINE_PAGE_SIZE,
                "tweet.fields": fields,
                "expansions": "attachments.media_keys",
                "media.fields": "url,preview_image_url,type,alt_text,duration_ms,variants",
            }
            if since_id:
                params["since_id"] = since_id
            if pagination_token:
                params["pagination_token"] = pagination_token
            resp = self.request("GET", f"/2/users/{uid}/tweets", params=params)
            payload = resp.json()
            batch = attach_media_includes(payload)
            tweets.extend(batch)
            pagination_token = (payload.get("meta") or {}).get("next_token")
            has_next = bool(pagination_token) and bool(payload.get("data"))
            at_cap = len(tweets) >= TIMELINE_CAP
            if at_cap and has_next:
                tail = f"stopped at cap {TIMELINE_CAP}"
            elif has_next:
                left = max(0, max_pages - page)
                tail = f"another pull follows (at most {left} left)"
            else:
                tail = "last page"
            rate = ""
            if self.rate_limit_remaining is not None:
                rate = f"; rate limit {self.rate_limit_remaining} remaining"
            say(f"Pull {page}: {len(batch)} tweet(s), {len(tweets)} so far; {tail}{rate}.")
            if not has_next or at_cap:
                break
        fetched = min(len(tweets), TIMELINE_CAP)
        say(f"Fetched {fetched} tweet(s) in {page} pull(s).")
        return tweets[:TIMELINE_CAP]

    def lookup_tweets(self, ids: list[str]) -> list[dict[str, Any]]:
        """GET /2/tweets?ids= — up to 100 ids. Missing/deleted posts are omitted."""
        clean = [i for i in ids if i][:100]
        if not clean:
            return []
        resp = self.request(
            "GET",
            "/2/tweets",
            params={
                "ids": ",".join(clean),
                "tweet.fields": "public_metrics",
            },
        )
        data = resp.json().get("data") or []
        return [t for t in data if isinstance(t, dict)]


def attach_media_includes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Join includes.media onto each tweet as ``_media`` (by attachments.media_keys)."""
    index: dict[str, dict[str, Any]] = {}
    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    for m in (includes or {}).get("media") or []:
        if isinstance(m, dict) and m.get("media_key"):
            index[str(m["media_key"])] = m
    out: list[dict[str, Any]] = []
    for t in payload.get("data") or []:
        if not isinstance(t, dict):
            continue
        row = dict(t)
        keys = (row.get("attachments") or {}).get("media_keys") or []
        row["_media"] = [index[str(k)] for k in keys if str(k) in index]
        out.append(row)
    return out


def build_client(cfg: Config, user_id: str | None = None) -> LiveXClient:
    return LiveXClient(cfg=cfg, user_id=user_id)
