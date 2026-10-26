"""Query, mark, delete, and incremental timeline sync."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from xarchive.db import Database, Media, Tweet
from xarchive.xclient import XApiError, XClientProtocol
from xarchive.ytd import (
    api_v2_to_tweet_dict,
    classify_tweet,
    engagement_from_obj,
    parse_created_at,
    tweet_id_str,
)

LOOKUP_BATCH = 100


class RealClock:
    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


@dataclass
class FakeClock:
    """Injectable clock for 429 tests — never sleeps the real thread."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += float(seconds)


@dataclass
class DeleteReport:
    deleted: list[str] = field(default_factory=list)
    already_gone: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def deleted_ids(self) -> list[str]:
        return list(self.deleted)


def parse_date_bound(value: str | None, *, end: bool = False) -> int | None:
    if not value:
        return None
    raw = value.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        y, m, d = (int(p) for p in raw.split("-"))
        if end:
            dt = datetime(y, m, d, 23, 59, 59, tzinfo=timezone.utc)
        else:
            dt = datetime(y, m, d, 0, 0, 0, tzinfo=timezone.utc)
        return int(dt.timestamp())
    from xarchive.ytd import parse_created_at as pca

    _iso, ts = pca(raw)
    return ts


def query_tweets(
    db: Database,
    *,
    text: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    media: bool = False,
    status: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    sort: str = "date",
) -> list[Tweet]:
    return db.query(
        text=text,
        from_ts=parse_date_bound(from_date, end=False),
        to_ts=parse_date_bound(to_date, end=True),
        media=media,
        status=status,
        limit=limit,
        offset=offset,
        sort=sort,
    )


def count_tweets(
    db: Database,
    *,
    text: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    media: bool = False,
    status: str | None = None,
) -> int:
    return db.count_matching(
        text=text,
        from_ts=parse_date_bound(from_date, end=False),
        to_ts=parse_date_bound(to_date, end=True),
        media=media,
        status=status,
    )


def mark_tweets(db: Database, action: str, ids: list[str]) -> int:
    status = {"keep": "keep", "delete": "delete_marked", "cached": "cached"}[action]
    return db.mark_many(ids, status)


def _dispatch_delete(client: XClientProtocol, tweet: Tweet) -> tuple[str, str]:
    """Originals: DELETE /2/tweets/:id. Retweets: DELETE /2/users/:id/retweets/:source."""
    if tweet.tweet_type == "retweet":
        if not tweet.retweeted_id:
            raise XApiError(
                400,
                "missing source tweet id for unretweet (archive had RT @… without retweeted_status)",
            )
        client.unretweet(tweet.retweeted_id)
        uid = getattr(client, "user_id", None) or "{id}"
        return ("DELETE", f"/2/users/{uid}/retweets/{tweet.retweeted_id}")
    client.delete_tweet(tweet.id)
    return ("DELETE", f"/2/tweets/{tweet.id}")


def _wait_seconds(err: XApiError, clock: RealClock | FakeClock) -> float:
    if err.retry_after is not None:
        return max(0.0, float(err.retry_after))
    if err.rate_limit_reset:
        return max(0.0, float(err.rate_limit_reset) - clock.time())
    return 1.0


def apply_deletes(
    db: Database,
    client: XClientProtocol,
    *,
    clock: RealClock | FakeClock | None = None,
    max_retries: int = 8,
    on_wait: Callable[[float], None] | None = None,
) -> DeleteReport:
    """Delete tweets marked delete_marked via the official API.

    404 already-gone is success. 429 is retried (honors Retry-After / x-rate-limit-*).
    Local media files are kept. Status is persisted so re-runs skip deleted ids.
    """
    clock = clock or RealClock()
    report = DeleteReport()
    marked = db.query(status="delete_marked")
    for tweet in marked:
        last_err: XApiError | None = None
        handled = False
        for _attempt in range(max_retries):
            rem = getattr(client, "rate_limit_remaining", None)
            reset = getattr(client, "rate_limit_reset", None)
            if rem == 0 and reset:
                wait = max(0.0, float(reset) - clock.time())
                if wait:
                    if on_wait:
                        on_wait(wait)
                    clock.sleep(wait)
            try:
                call = _dispatch_delete(client, tweet)
                report.calls.append(call)
                db.set_status(tweet.id, "deleted")
                report.deleted.append(tweet.id)
                handled = True
                break
            except XApiError as err:
                last_err = err
                if err.status_code == 404:
                    report.calls.append(("DELETE", err.path or tweet.id))
                    db.set_status(tweet.id, "deleted")
                    report.deleted.append(tweet.id)
                    report.already_gone.append(tweet.id)
                    handled = True
                    break
                if err.status_code == 429:
                    wait = _wait_seconds(err, clock)
                    if on_wait:
                        on_wait(wait)
                    clock.sleep(wait)
                    continue
                db.set_status(tweet.id, "delete_failed", err.message)
                report.failed.append(tweet.id)
                handled = True
                break
        if not handled:
            msg = f"rate limited after {max_retries} retries"
            if last_err:
                msg = f"{msg}: {last_err.message}"
            db.set_status(tweet.id, "delete_failed", msg)
            report.failed.append(tweet.id)
    return report


@dataclass
class SyncResult:
    inserted: int = 0
    media_downloaded: int = 0
    metrics_updated: int = 0
    metrics_lookups: int = 0


def media_download_spec(media: dict) -> tuple[str, str] | None:
    """Return (url, filename) for a v2 includes.media object, or None."""
    key = re.sub(r"[^A-Za-z0-9._-]", "_", str(media.get("media_key") or "media"))
    kind = (media.get("type") or "photo").lower()
    if kind == "photo":
        url = media.get("url") or media.get("preview_image_url")
        if not url:
            return None
        return str(url), f"{key}.jpg"
    if kind in ("video", "animated_gif"):
        variants = media.get("variants") or []
        mp4s = [
            v
            for v in variants
            if isinstance(v, dict) and "mp4" in str(v.get("content_type") or "")
        ]
        if mp4s:
            best = max(mp4s, key=lambda v: int(v.get("bit_rate") or 0))
            url = best.get("url")
            if url:
                return str(url), f"{key}.mp4"
        preview = media.get("preview_image_url")
        if preview:
            return str(preview), f"{key}.jpg"
    return None


def _fetch_url(url: str) -> bytes:
    resp = httpx.get(url, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def sync_timeline(
    db: Database,
    client: XClientProtocol,
    *,
    now: str | None = None,
    media_dir: Path | None = None,
    fetch_bytes: Callable[[str], bytes] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> SyncResult:
    """Insert only tweets newer than the cache max id. Download media when media_dir is set."""
    from datetime import datetime, timezone as tz

    def say(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    stamp = now or datetime.now(tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    max_id = db.max_id()
    payloads = client.user_timeline(since_id=max_id, on_progress=on_progress)
    result = SyncResult()
    max_int = int(max_id) if max_id and str(max_id).isdigit() else 0
    fetch = fetch_bytes if fetch_bytes is not None else (_fetch_url if media_dir else None)
    pending: list[tuple[dict, dict, str]] = []
    skipped = 0
    for payload in payloads:
        tweet = api_v2_to_tweet_dict(payload)
        tid = tweet_id_str(tweet)
        if not tid or (tid.isdigit() and int(tid) <= max_int) or db.get(tid):
            skipped += 1
            continue
        pending.append((payload, tweet, tid))
    say(
        f"{len(pending)} tweet(s) to save; "
        f"{skipped} skipped (already stored or older than the cache)."
    )
    total = len(pending)
    for index, (payload, tweet, tid) in enumerate(pending, start=1):
        say(f"Saving {index}/{total} {tid}")
        text = tweet.get("full_text") or ""
        created_at, ts = parse_created_at(tweet.get("created_at"))
        ttype, reply_id, rt_id = classify_tweet(tweet, text)
        media_objs = payload.get("_media") or []
        specs: list[tuple[dict, str, str]] = []
        if isinstance(media_objs, list):
            for m in media_objs:
                if not isinstance(m, dict):
                    continue
                spec = media_download_spec(m)
                if not spec:
                    continue
                url, suffix = spec
                specs.append((m, url, f"{tid}-{suffix}"))
        media_rows: list[Media] = []
        downloading = media_dir is not None and fetch is not None
        n_media = len(specs)
        for m_i, (m, url, filename) in enumerate(specs, start=1):
            local_path = None
            if downloading:
                say(f"Downloading media {m_i}/{n_media} for {tid}: {filename}")
                dest = media_dir / tid / filename
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(fetch(url))
                    local_path = str(Path("media") / tid / filename)
                    result.media_downloaded += 1
                except (httpx.HTTPError, OSError, ValueError):
                    say(f"Media download failed for {tid}: {filename}")
                    local_path = None
            media_rows.append(
                Media(
                    tweet_id=tid,
                    media_type=m.get("type"),
                    original_url=url,
                    local_path=local_path,
                    filename=filename if local_path else None,
                )
            )
        db.upsert_content(
            tweet_id=tid,
            created_at=created_at,
            created_at_ts=ts,
            text=text,
            tweet_type=ttype,
            in_reply_to_id=reply_id,
            retweeted_id=rt_id,
            lang=tweet.get("lang"),
            source=tweet.get("source"),
            raw_json=json.dumps(payload, ensure_ascii=False),
            has_media=bool(media_rows),
            now=stamp,
            engagement=engagement_from_obj(payload),
            metrics_at=stamp,
        )
        if media_rows:
            db.replace_media(tid, media_rows)
        result.inserted += 1
    updated, lookups = refresh_missing_metrics(db, client, now=stamp)
    result.metrics_updated = updated
    result.metrics_lookups = lookups
    return result


def _lookup_chunk(
    client: XClientProtocol,
    ids: list[str],
    clock: RealClock | FakeClock,
    *,
    max_retries: int = 8,
) -> list[dict]:
    last_err: XApiError | None = None
    for _attempt in range(max_retries):
        rem = getattr(client, "rate_limit_remaining", None)
        reset = getattr(client, "rate_limit_reset", None)
        if rem == 0 and reset:
            wait = max(0.0, float(reset) - clock.time())
            if wait:
                clock.sleep(wait)
        try:
            return client.lookup_tweets(ids)
        except XApiError as err:
            last_err = err
            if err.status_code == 429:
                clock.sleep(_wait_seconds(err, clock))
                continue
            raise
    if last_err:
        raise last_err
    return []


def refresh_missing_metrics(
    db: Database,
    client: XClientProtocol,
    *,
    now: str | None = None,
    clock: RealClock | FakeClock | None = None,
) -> tuple[int, int]:
    """GET /2/tweets?ids= (100/request) for posts that have never been metrics-synced.

    One pass per cache: later syncs skip ids that already have metrics_at.
    Missing/deleted posts are omitted by the API; we still stamp metrics_at so
    we do not retry them every week.
    """
    from datetime import datetime, timezone as tz

    stamp = now or datetime.now(tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clock = clock or RealClock()
    ids = db.ids_missing_metrics()
    updated = 0
    lookups = 0
    for i in range(0, len(ids), LOOKUP_BATCH):
        chunk = ids[i : i + LOOKUP_BATCH]
        rows = _lookup_chunk(client, chunk, clock)
        lookups += 1
        found: set[str] = set()
        with db.transaction():
            for payload in rows:
                tid = tweet_id_str(payload) or str(payload.get("id") or "")
                if not tid:
                    continue
                found.add(tid)
                eng = engagement_from_obj(payload)
                db.set_engagement(tid, metrics_at=stamp, **eng)
                updated += 1
            for tid in chunk:
                if tid not in found:
                    db.set_engagement(tid, metrics_at=stamp)
                    updated += 1
    return updated, lookups
