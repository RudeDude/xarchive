"""Official-archive-shaped fixtures. No live X."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from xarchive.config import load_config
from xarchive.db import Database

# 1x1 JPEG (SOI/APP0/SOF/SOS/EOI) — good enough to copy as {id}-photo.jpg
MINI_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00" + bytes([8] * 64)
    + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x7f\x3f"
    b"\xff\xd9"
)

# Snowflake-ish string ids spanning 2018–2025
T2018 = "970000000000000001"
T2019_XYLO = "1140000000000000002"
T2020_REPLY = "1210000000000000003"
T2021_RT = "1450000000000000004"
T2022_MEDIA = "1560000000000000005"
T2023 = "1620000000000000006"
T2024_REPLY = "1780000000000000007"
T2025_NOTE = "1870000000000000008"
T2025_PART1 = "1990000000000000009"
RT_SOURCE = "9998887770000000000"
NOTE_ID = "1870000000000000999"
ACCOUNT_ID = "5166878"

XYLOPHONE = "xylophone"
NOTE_PHRASE = "longform_banjo_solo"


def _tweet(
    id_str: str,
    created: str,
    full_text: str,
    *,
    reply_to: str | None = None,
    retweeted_status_id: str | None = None,
    media: bool = False,
    note_id: str | None = None,
    favorite_count: str = "0",
    retweet_count: str = "0",
) -> dict:
    tw: dict = {
        "edit_info": {
            "initial": {
                "editTweetIds": [id_str],
                "editableUntil": "2099-01-01T00:00:00.000Z",
                "editsRemaining": "5",
                "isEditEligible": True,
            }
        },
        "retweeted": False,
        "source": '<a href="https://mobile.twitter.com" rel="nofollow">Twitter Web App</a>',
        "entities": {"hashtags": [], "symbols": [], "user_mentions": [], "urls": []},
        "display_text_range": ["0", str(len(full_text))],
        "favorite_count": favorite_count,
        "id_str": id_str,
        "truncated": False,
        "retweet_count": retweet_count,
        "id": id_str,
        "created_at": created,
        "favorited": False,
        "full_text": full_text,
        "lang": "en",
    }
    if reply_to:
        tw["in_reply_to_status_id_str"] = reply_to
        tw["in_reply_to_user_id_str"] = ACCOUNT_ID
        tw["in_reply_to_screen_name"] = "someone"
    if retweeted_status_id:
        tw["retweeted_status"] = {
            "id_str": retweeted_status_id,
            "id": retweeted_status_id,
            "full_text": "great article",
        }
        tw["entities"]["user_mentions"] = [
            {
                "screen_name": "friend",
                "name": "Friend",
                "id_str": "42",
                "id": "42",
                "indices": ["3", "10"],
            }
        ]
    if media:
        m = {
            "expanded_url": f"https://twitter.com/user/status/{id_str}/photo/1",
            "indices": ["0", "23"],
            "url": "https://t.co/media",
            "media_url": "http://pbs.twimg.com/media/photo.jpg",
            "id_str": id_str + "9",
            "id": id_str + "9",
            "media_url_https": "https://pbs.twimg.com/media/photo.jpg",
            "display_url": "pic.twitter.com/media",
            "type": "photo",
        }
        tw["entities"]["media"] = [m]
        tw["extended_entities"] = {"media": [m]}
    if note_id:
        tw["note_tweet"] = {"id": note_id, "text": full_text}
    return {"tweet": tw}


def _ytd(ns: str, part: int, payload: list) -> str:
    body = json.dumps(payload, indent=2)
    return f"window.YTD.{ns}.part{part} = {body};\n"


def part0_tweets() -> list[dict]:
    return [
        _tweet(T2018, "Thu Mar 01 12:00:00 +0000 2018", "hello from the beginning of the archive"),
        _tweet(
            T2019_XYLO,
            "Sat Jun 15 18:30:00 +0000 2019",
            f"I played the {XYLOPHONE} at the park",
            favorite_count="12",
        ),
        _tweet(
            T2020_REPLY,
            "Fri Jan 10 09:00:00 +0000 2020",
            "this is a reply to the first post",
            reply_to=T2018,
        ),
        _tweet(
            T2021_RT,
            "Thu Nov 11 11:11:00 +0000 2021",
            "RT @friend: great article",
            retweeted_status_id=RT_SOURCE,
        ),
        _tweet(
            T2022_MEDIA,
            "Sat Aug 20 16:00:00 +0000 2022",
            "sunset photo from the ridge",
            media=True,
            retweet_count="7",
        ),
        _tweet(T2023, "Thu Feb 02 02:02:00 +0000 2023", "ordinary mid years post"),
        _tweet(
            T2024_REPLY,
            "Sun May 05 05:05:00 +0000 2024",
            "replying in 2024",
            reply_to=T2023,
        ),
        _tweet(
            T2025_NOTE,
            "Wed Jan 01 00:00:00 +0000 2025",
            "A longer thought https://t.co/abcd",
            note_id=NOTE_ID,
        ),
    ]


def part1_tweets() -> list[dict]:
    return [
        _tweet(T2025_PART1, "Wed Dec 31 23:59:00 +0000 2025", "new years eve wrap up"),
    ]


def note_tweet_payload() -> list[dict]:
    long_text = (
        "A longer thought that would not fit in 280 characters. "
        f"It includes a {NOTE_PHRASE} in the middle of the essay. " * 4
    )
    return [
        {
            "noteTweet": {
                "noteTweetId": NOTE_ID,
                "tweetId": T2025_NOTE,
                "noteTweetResults": {
                    "text": long_text.strip(),
                    "entitySet": {
                        "hashtags": [],
                        "symbols": [],
                        "userMentions": [],
                        "urls": [],
                    },
                },
            }
        }
    ]


def account_payload() -> list[dict]:
    return [
        {
            "account": {
                "username": "fixture_user",
                "accountId": ACCOUNT_ID,
                "createdAt": "2009-01-01T00:00:00.000Z",
                "accountDisplayName": "Fixture",
            }
        }
    ]


def write_archive_zip(path: Path, *, extra_members: dict[str, bytes] | None = None) -> Path:
    """Build a ZIP shaped like a 2026 official archive. No community folder."""
    root = "twitter-2026-01-01-deadbeef"
    buf = io.BytesIO() if path.suffix == "" else None
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{root}/data/tweets.js", _ytd("tweets", 0, part0_tweets()))
        zf.writestr(f"{root}/data/tweets1.js", _ytd("tweets", 1, part1_tweets()))
        zf.writestr(f"{root}/data/note-tweet.js", _ytd("note_tweet", 0, note_tweet_payload()))
        zf.writestr(f"{root}/data/account.js", _ytd("account", 0, account_payload()))
        zf.writestr(
            f"{root}/data/tweet-headers.js",
            _ytd(
                "tweet_headers",
                0,
                [{"tweetHeaders": {"tweetId": T2018, "createdAt": "2018-03-01T12:00:00.000Z"}}],
            ),
        )
        zf.writestr(
            f"{root}/data/tweets_media/{T2022_MEDIA}-photo.jpg",
            MINI_JPEG,
        )
        # likes.js present but must be ignored
        zf.writestr(
            f"{root}/data/like.js",
            _ytd("like", 0, [{"like": {"tweetId": "1", "fullText": "nope"}}]),
        )
        if extra_members:
            for name, data in extra_members.items():
                zf.writestr(name, data)
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def cfg(data_dir: Path):
    return load_config(data_dir)


@pytest.fixture
def db(cfg) -> Database:
    database = Database(cfg.db_path)
    yield database
    database.close()


@pytest.fixture
def archive_zip(tmp_path: Path) -> Path:
    z = tmp_path / "archive.zip"
    write_archive_zip(z)
    return z
