"""Parse official X archive YTD JavaScript wrappers (not JSON)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

YTD_PREFIX = re.compile(
    r"^window\.YTD\.[A-Za-z0-9_]+\.part\d+\s*=\s*",
    re.DOTALL,
)

TWEETS_JS = re.compile(
    r"(^|/)data/tweets?(?:-part\d+|\d+)?\.js$",
    re.IGNORECASE,
)
NOTE_TWEET_JS = re.compile(
    r"(^|/)data/note[-_]tweet(?:-part\d+|\d+)?\.js$",
    re.IGNORECASE,
)
COMMUNITY_JS = re.compile(
    r"(^|/)data/community[-_]tweet(?:-part\d+|\d+)?\.js$",
    re.IGNORECASE,
)
DELETED_JS = re.compile(
    r"(^|/)data/deleted[-_]tweets(?:-part\d+|\d+)?\.js$",
    re.IGNORECASE,
)
ACCOUNT_JS = re.compile(r"(^|/)data/account\.js$", re.IGNORECASE)
HEADERS_JS = re.compile(
    r"(^|/)data/tweet[-_]headers(?:-part\d+|\d+)?\.js$",
    re.IGNORECASE,
)
MEDIA_MEMBER = re.compile(
    r"(^|/)data/tweets_media/(.+)$",
    re.IGNORECASE,
)

SKIP_PREFIXES = (
    "direct-messages",
    "direct_messages",
    "ad-impressions",
    "ad_impressions",
    "ads",
    "grok",
    "like.js",
    "likes.js",
    "browser-extension",
)


class YtdParseError(ValueError):
    """The file is not a valid YTD tweets.js-style payload."""


def parse_ytd_js(text: str) -> list[Any]:
    """Strip ``window.YTD.<name>.partN =`` and optional trailing ``;``, then JSON."""
    raw = text.lstrip("\ufeff").strip()
    raw = YTD_PREFIX.sub("", raw, count=1)
    raw = raw.rstrip()
    if raw.endswith(";"):
        raw = raw[:-1].rstrip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise YtdParseError(f"invalid YTD JSON: {exc}") from exc
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise YtdParseError(f"unexpected YTD payload type: {type(data).__name__}")


def posix_name(name: str) -> str:
    return name.replace("\\", "/")


def is_skipped_archive_file(name: str) -> bool:
    base = posix_name(name).rsplit("/", 1)[-1].lower()
    lower = posix_name(name).lower()
    if any(p in lower for p in ("/direct-messages", "/direct_messages", "/ad-impressions", "/ads/", "/grok")):
        return True
    return base in {"like.js", "likes.js"} or base.startswith("like-")


def parse_created_at(value: str | None) -> tuple[str, int]:
    if not value:
        return "1970-01-01T00:00:00Z", 0
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        iso = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return value, 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), int(dt.timestamp())


def normalize_header_tweet(obj: dict[str, Any]) -> dict[str, Any]:
    """Map a tweet-headers.js record onto the tweets.js field names.

    Official archives list every non-deleted post here (``tweetId`` / ``createdAt``,
    or ``tweet.tweet_id``). ``tweets.js`` often has full text only for a newer slice.
    """
    if not isinstance(obj, dict):
        return {}
    tid = tweet_id_str(obj)
    created = obj.get("created_at") or obj.get("createdAt") or ""
    text = obj.get("full_text") or obj.get("text") or ""
    out = dict(obj)
    if tid:
        out["id"] = str(tid)
        out["id_str"] = str(tid)
    if created:
        out["created_at"] = str(created)
    if text:
        out["full_text"] = text
    elif "full_text" not in out and "text" not in out:
        out["full_text"] = ""
    return out


def tweet_id_str(obj: dict[str, Any]) -> str | None:
    for key in ("id_str", "id", "tweetId", "tweet_id"):
        val = obj.get(key)
        if val is not None and str(val).strip():
            return str(val)
    return None


def unwrap_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    for key in (
        "tweet",
        "communityTweet",
        "community_tweet",
        "deletedTweet",
        "deleted_tweet",
        "tweetHeaders",
        "noteTweet",
    ):
        inner = item.get(key)
        if isinstance(inner, dict):
            nested = inner.get("tweet")
            if key != "tweet" and isinstance(nested, dict):
                return nested
            return inner
    return item


def classify_tweet(tweet: dict[str, Any], text: str) -> tuple[str, str | None, str | None]:
    """Return (type, in_reply_to_id, retweeted_source_id)."""
    rt_id = None
    rt_obj = tweet.get("retweeted_status")
    if isinstance(rt_obj, dict):
        rt_id = tweet_id_str(rt_obj)
    if not rt_id:
        for key in ("retweeted_status_id_str", "retweeted_status_id"):
            if tweet.get(key):
                rt_id = str(tweet[key])
                break
    if rt_id:
        return "retweet", None, rt_id
    reply = tweet.get("in_reply_to_status_id_str") or tweet.get("in_reply_to_status_id")
    if reply:
        return "reply", str(reply), None
    refs = tweet.get("referenced_tweets")
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            rtype = ref.get("type")
            rid = ref.get("id")
            if rtype == "retweeted" and rid:
                return "retweet", None, str(rid)
            if rtype == "replied_to" and rid:
                return "reply", str(rid), None
    return "original", None, None


_STATUS_URL = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/([^/?#]+)/status/(\d+)",
    re.IGNORECASE,
)
_RT_AT = re.compile(r"^RT @([A-Za-z0-9_]+):")
_PLACEHOLDER_HANDLES = frozenset({"i", "web", "intent"})


@dataclass(frozen=True)
class Ref:
    """Display-only pointer to another post. Not used for delete routing."""

    kind: str  # reply | quote | retweet
    status_id: str | None
    screen_name: str | None


def _as_raw_dict(raw: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _handle(name: str | None) -> str | None:
    if not name:
        return None
    s = str(name).strip().lstrip("@")
    if not s or s.lower() in _PLACEHOLDER_HANDLES:
        return None
    return s


def _user_screen_name(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    user = obj.get("user")
    if isinstance(user, dict):
        return _handle(user.get("screen_name") or user.get("username"))
    return _handle(obj.get("screen_name") or obj.get("username"))


def reference_targets(
    raw: dict[str, Any] | str | None,
    *,
    text: str = "",
    in_reply_to_id: str | None = None,
    retweeted_id: str | None = None,
    tweet_id: str | None = None,
) -> list[Ref]:
    """Extract reply/quote/retweet targets from archive or v2 JSON for the UI.

    Does not change ``classify_tweet`` / delete routing. ``RT @user:`` text
    without a source id yields a handle-only retweet ref (no embed).
    """
    data = _as_raw_dict(raw)
    out: list[Ref] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: str, status_id: str | None, screen_name: str | None) -> None:
        sid = str(status_id).strip() if status_id else None
        if sid == "":
            sid = None
        handle = _handle(screen_name)
        if not sid and not handle:
            return
        if tweet_id and sid == str(tweet_id):
            return
        key = (kind, sid or "", handle or "")
        if key in seen:
            return
        seen.add(key)
        out.append(Ref(kind=kind, status_id=sid, screen_name=handle))

    reply_id = in_reply_to_id or data.get("in_reply_to_status_id_str") or data.get(
        "in_reply_to_status_id"
    )
    reply_name = data.get("in_reply_to_screen_name")
    rt_id = retweeted_id
    refs = data.get("referenced_tweets")
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            rtype = ref.get("type")
            rid = ref.get("id")
            if not rid:
                continue
            if rtype == "replied_to" and not reply_id:
                reply_id = str(rid)
            elif rtype == "retweeted" and not rt_id:
                rt_id = str(rid)
            elif rtype == "quoted":
                add("quote", str(rid), None)

    if reply_id or reply_name:
        add("reply", str(reply_id) if reply_id else None, reply_name)

    rt_name = None
    rt_obj = data.get("retweeted_status")
    if isinstance(rt_obj, dict):
        rt_id = rt_id or tweet_id_str(rt_obj)
        rt_name = _user_screen_name(rt_obj)
    if not rt_id:
        for key in ("retweeted_status_id_str", "retweeted_status_id"):
            if data.get(key):
                rt_id = str(data[key])
                break
    m_rt = _RT_AT.match(text or "")
    if m_rt and not rt_name:
        rt_name = m_rt.group(1)
    if rt_id or rt_name:
        add("retweet", rt_id, rt_name)

    qid = data.get("quoted_status_id_str") or data.get("quoted_status_id")
    qobj = data.get("quoted_status")
    qname = _user_screen_name(qobj) if isinstance(qobj, dict) else None
    if isinstance(qobj, dict) and not qid:
        qid = tweet_id_str(qobj)
    if qid:
        add("quote", str(qid), qname)

    entities = data.get("entities")
    url_items: list[Any] = []
    if isinstance(entities, dict):
        url_items = entities.get("urls") or []
    if isinstance(url_items, list):
        for u in url_items:
            if not isinstance(u, dict):
                continue
            exp = str(u.get("expanded_url") or u.get("url") or "")
            m = _STATUS_URL.search(exp)
            if not m:
                continue
            handle, sid = m.group(1), m.group(2)
            if reply_id and str(sid) == str(reply_id):
                continue
            if rt_id and str(sid) == str(rt_id):
                continue
            add("quote", sid, handle)

    return out


def _note_text_from_obj(nt: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (note_id, tweet_id, text) from a noteTweet object."""
    note_id = tweet_id_str(nt) or (str(nt["noteTweetId"]) if nt.get("noteTweetId") else None)
    tweet_id = str(nt["tweetId"]) if nt.get("tweetId") else None
    text = None
    results = nt.get("noteTweetResults")
    if isinstance(results, dict):
        text = results.get("text") or text
        inner = results.get("result")
        if isinstance(inner, dict):
            text = inner.get("text") or text
            core = inner.get("core")
            if isinstance(core, dict):
                text = core.get("text") or text
            note_id = note_id or tweet_id_str(inner)
    core = nt.get("core")
    if isinstance(core, dict):
        text = text or core.get("text")
    text = text or nt.get("text")
    return note_id, tweet_id, text if isinstance(text, str) else None


def index_note_tweets(items: list[Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Maps tweet_id -> long text and note_id -> long text."""
    by_tweet: dict[str, str] = {}
    by_note: dict[str, str] = {}
    for item in items:
        nt = unwrap_item(item) if "noteTweet" in item or "note_tweet" in item else (
            item.get("noteTweet") if isinstance(item, dict) else None
        )
        if not isinstance(nt, dict):
            if isinstance(item, dict) and ("noteTweetId" in item or "core" in item or "noteTweetResults" in item):
                nt = item
            elif isinstance(item, dict) and isinstance(item.get("noteTweet"), dict):
                nt = item["noteTweet"]
            else:
                continue
        note_id, tweet_id, text = _note_text_from_obj(nt)
        if not text:
            continue
        if tweet_id:
            by_tweet[tweet_id] = text
        if note_id:
            by_note[note_id] = text
    return by_tweet, by_note


def extract_inline_note(tweet: dict[str, Any]) -> tuple[str | None, str | None]:
    nt = tweet.get("note_tweet") or tweet.get("noteTweet")
    if not isinstance(nt, dict):
        return None, None
    note_id, _tweet_id, text = _note_text_from_obj(nt)
    return note_id, text


def joined_text(tweet: dict[str, Any], notes_by_tweet: dict[str, str], notes_by_note: dict[str, str]) -> str:
    base = tweet.get("full_text") or tweet.get("text") or ""
    tid = tweet_id_str(tweet)
    note_id, inline = extract_inline_note(tweet)
    candidates = [base]
    if inline:
        candidates.append(inline)
    if tid and tid in notes_by_tweet:
        candidates.append(notes_by_tweet[tid])
    if note_id and note_id in notes_by_note:
        candidates.append(notes_by_note[note_id])
    # Prefer the longest body (note tweets expand past 280 chars).
    return max(candidates, key=len)


def media_refs(tweet: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in (
        (tweet.get("extended_entities") or {}).get("media") if isinstance(tweet.get("extended_entities"), dict) else None,
        (tweet.get("entities") or {}).get("media") if isinstance(tweet.get("entities"), dict) else None,
    ):
        if not isinstance(bucket, list):
            continue
        for m in bucket:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id_str") or m.get("id") or m.get("media_url_https") or "")
            if mid in seen:
                continue
            seen.add(mid)
            out.append(m)
    return out


def parse_account_id(items: list[Any]) -> str | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        acc = item.get("account") if isinstance(item.get("account"), dict) else item
        for key in ("accountId", "account_id", "userId", "user_id"):
            if acc.get(key):
                return str(acc[key])
    return None


def api_v2_to_tweet_dict(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a GET /2/users/:id/tweets object into archive-like fields."""
    created = payload.get("created_at") or ""
    text = payload.get("text") or payload.get("note_tweet", {}).get("text") or ""
    if isinstance(payload.get("note_tweet"), dict) and payload["note_tweet"].get("text"):
        text = payload["note_tweet"]["text"]
    return {
        "id_str": str(payload.get("id") or ""),
        "id": str(payload.get("id") or ""),
        "created_at": created,
        "full_text": text,
        "text": text,
        "lang": payload.get("lang"),
        "source": payload.get("source"),
        "referenced_tweets": payload.get("referenced_tweets") or [],
        "entities": payload.get("entities") or {},
        "public_metrics": payload.get("public_metrics") or {},
        "in_reply_to_status_id_str": next(
            (
                str(r["id"])
                for r in (payload.get("referenced_tweets") or [])
                if isinstance(r, dict) and r.get("type") == "replied_to" and r.get("id")
            ),
            None,
        ),
    }


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def engagement_from_obj(obj: dict[str, Any] | None) -> dict[str, int | None]:
    """Likes/RTs/replies/quotes/bookmarks/impressions from archive or v2 JSON."""
    data = obj if isinstance(obj, dict) else {}
    pm = data.get("public_metrics")
    if not isinstance(pm, dict):
        pm = {}
    likes = _as_int(pm.get("like_count"))
    if likes is None:
        likes = _as_int(data.get("favorite_count") if data.get("favorite_count") is not None else data.get("like_count"))
    rts = _as_int(pm.get("retweet_count"))
    if rts is None:
        rts = _as_int(data.get("retweet_count"))
    return {
        "like_count": likes,
        "retweet_count": rts,
        "reply_count": _as_int(pm.get("reply_count") if pm.get("reply_count") is not None else data.get("reply_count")),
        "quote_count": _as_int(pm.get("quote_count") if pm.get("quote_count") is not None else data.get("quote_count")),
        "bookmark_count": _as_int(pm.get("bookmark_count")),
        "impression_count": _as_int(
            pm.get("impression_count") if pm.get("impression_count") is not None else data.get("impression_count")
        ),
    }
