"""In-process fake of the official X API. Never opens a socket to api.x.com."""

from __future__ import annotations

from typing import Any

from xarchive.xclient import XApiError


class FakeXClient:
    def __init__(self, user_id: str = "5166878"):
        self.user_id = user_id
        self.calls: list[tuple[str, str]] = []
        self._429_left: dict[str, int] = {}
        self._404: set[str] = set()
        self.timeline: list[dict[str, Any]] = []
        self.rate_limit_remaining: int | None = 50
        self.rate_limit_reset: int | None = None
        self.deleted: list[str] = []
        self.unretweeted: list[str] = []
        self.lookup: dict[str, dict[str, Any]] = {}
        self.lookup_ids: list[list[str]] = []

    def fail_429_then_ok(self, path: str, times: int = 1) -> None:
        self._429_left[path] = times

    def gone_404(self, path: str) -> None:
        self._404.add(path)

    def _hit(self, method: str, path: str) -> None:
        self.calls.append((method, path))
        if path in self._404:
            raise XApiError(404, "Not Found", path=path)
        left = self._429_left.get(path, 0)
        if left > 0:
            self._429_left[path] = left - 1
            raise XApiError(429, "Too Many Requests", retry_after=7, path=path)

    def delete_tweet(self, tweet_id: str) -> None:
        path = f"/2/tweets/{tweet_id}"
        self._hit("DELETE", path)
        self.deleted.append(tweet_id)

    def unretweet(self, source_tweet_id: str) -> None:
        path = f"/2/users/{self.user_id}/retweets/{source_tweet_id}"
        self._hit("DELETE", path)
        self.unretweeted.append(source_tweet_id)

    def get_me(self) -> dict[str, Any]:
        self.calls.append(("GET", "/2/users/me"))
        return {"id": self.user_id, "username": "fixture_user"}

    def user_timeline(
        self,
        since_id: str | None = None,
        *,
        on_progress: Any = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(("GET", f"/2/users/{self.user_id}/tweets"))
        out = list(self.timeline)
        if since_id and str(since_id).isdigit():
            cut = int(since_id)
            out = [t for t in out if int(t["id"]) > cut]
        return out

    def lookup_tweets(self, ids: list[str]) -> list[dict[str, Any]]:
        path = "/2/tweets"
        self._hit("GET", path)
        chunk = [i for i in ids if i][:100]
        self.lookup_ids.append(chunk)
        out: list[dict[str, Any]] = []
        for i in chunk:
            if i in self.lookup:
                row = dict(self.lookup[i])
                row.setdefault("id", i)
                out.append(row)
        return out
