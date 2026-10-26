import argparse

import httpx

from xarchive.archive import import_archive
from xarchive.cli import cmd_sync
from xarchive.ops import media_download_spec, sync_timeline
from xarchive.xclient import LiveXClient, XApiError, attach_media_includes
from tests.conftest import MINI_JPEG, T2018, T2025_PART1
from tests.fake_client import FakeXClient


def test_sync_inserts_only_newer_than_max_id(cfg, db, archive_zip):
    import_archive(archive_zip, db, cfg.media_dir)
    max_id = db.max_id()
    assert max_id == T2025_PART1
    n_before = db.count()
    client = FakeXClient()
    older = str(int(T2025_PART1) - 10)
    newer = str(int(T2025_PART1) + 10)
    newer2 = str(int(T2025_PART1) + 20)
    client.timeline = [
        {
            "id": older,
            "text": "should be ignored, older than cache",
            "created_at": "2024-01-01T00:00:00.000Z",
        },
        {
            "id": newer,
            "text": "brand new post after the archive",
            "created_at": "2026-02-01T00:00:00.000Z",
        },
        {
            "id": newer2,
            "text": "even newer",
            "created_at": "2026-03-01T00:00:00.000Z",
        },
    ]
    inserted = sync_timeline(db, client)
    assert inserted.inserted == 2
    assert db.count() == n_before + 2
    assert db.get(newer) is not None
    assert db.get(older) is None
    assert ("GET", f"/2/users/{client.user_id}/tweets") in client.calls


def test_sync_downloads_photo_media(cfg, db, archive_zip):
    import_archive(archive_zip, db, cfg.media_dir)
    newer = str(int(T2025_PART1) + 10)
    client = FakeXClient()
    client.timeline = [
        {
            "id": newer,
            "text": "new photo",
            "created_at": "2026-02-01T00:00:00.000Z",
            "attachments": {"media_keys": ["3_abc"]},
            "_media": [
                {
                    "media_key": "3_abc",
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/abc.jpg",
                }
            ],
        }
    ]
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return MINI_JPEG

    result = sync_timeline(db, client, media_dir=cfg.media_dir, fetch_bytes=fetch)
    assert result.inserted == 1
    assert result.media_downloaded == 1
    assert fetched == ["https://pbs.twimg.com/media/abc.jpg"]
    dest = cfg.media_dir / newer / f"{newer}-3_abc.jpg"
    assert dest.is_file()
    row = db.get(newer)
    assert row is not None and row.has_media
    linked = db.media_for(newer)
    assert linked and linked[0].local_path.endswith(f"{newer}-3_abc.jpg")


def test_attach_media_includes_joins_by_key():
    tweets = attach_media_includes(
        {
            "data": [
                {"id": "1", "text": "a", "attachments": {"media_keys": ["3_x"]}},
                {"id": "2", "text": "b"},
            ],
            "includes": {
                "media": [
                    {"media_key": "3_x", "type": "photo", "url": "https://pbs.twimg.com/media/x.jpg"}
                ]
            },
        }
    )
    assert len(tweets[0]["_media"]) == 1
    assert tweets[0]["_media"][0]["url"].endswith("x.jpg")
    assert tweets[1]["_media"] == []


def test_media_download_spec_picks_highest_bitrate_mp4():
    spec = media_download_spec(
        {
            "media_key": "7_vid",
            "type": "video",
            "variants": [
                {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/a.m3u8"},
                {"content_type": "video/mp4", "bit_rate": 256000, "url": "https://video.twimg.com/lo.mp4"},
                {"content_type": "video/mp4", "bit_rate": 832000, "url": "https://video.twimg.com/hi.mp4"},
            ],
        }
    )
    assert spec == ("https://video.twimg.com/hi.mp4", "7_vid.mp4")


def test_sync_backfills_metrics_once(cfg, db, archive_zip):
    import_archive(archive_zip, db, cfg.media_dir)
    assert db.get(T2018).impression_count is None
    assert db.get(T2018).metrics_at is None
    client = FakeXClient()
    client.lookup[T2018] = {
        "id": T2018,
        "public_metrics": {
            "like_count": 9,
            "retweet_count": 1,
            "reply_count": 2,
            "quote_count": 0,
            "bookmark_count": 0,
            "impression_count": 400,
        },
    }
    result = sync_timeline(db, client)
    assert result.metrics_lookups == 1
    row = db.get(T2018)
    assert row.impression_count == 400
    assert row.like_count == 9
    assert row.metrics_at
    client2 = FakeXClient()
    result2 = sync_timeline(db, client2)
    assert result2.metrics_lookups == 0
    assert client2.lookup_ids == []


def test_sync_stores_public_metrics_on_new_tweets(cfg, db, archive_zip):
    import_archive(archive_zip, db, cfg.media_dir)
    newer = str(int(T2025_PART1) + 10)
    client = FakeXClient()
    client.timeline = [
        {
            "id": newer,
            "text": "fresh with metrics",
            "created_at": "2026-02-01T00:00:00.000Z",
            "public_metrics": {"like_count": 2, "impression_count": 50, "retweet_count": 0},
        }
    ]
    # Pre-stamp existing rows so this test only covers the new tweet path.
    for t in db.iter_all():
        db.set_engagement(t.id, metrics_at="2020-01-01T00:00:00Z")
    result = sync_timeline(db, client)
    assert result.inserted == 1
    assert result.metrics_lookups == 0
    row = db.get(newer)
    assert row.like_count == 2
    assert row.impression_count == 50


def test_sync_cli_noop_without_creds(cfg, capsys):
    from xarchive.cli import main

    rc = main(["--data-dir", str(cfg.data_dir), "sync"])
    assert rc == 0
    assert "skipping" in capsys.readouterr().out.lower()


def test_sync_cli_prints_connection_and_cache(cfg, capsys):
    client = FakeXClient()
    rc = cmd_sync(cfg, argparse.Namespace(), client=client)
    assert rc == 0
    out = capsys.readouterr().out
    assert "API connected as @fixture_user (5166878)." in out
    assert "Local cache: 0 tweet(s); newest id (none)." in out
    assert "0 tweet(s) to save; 0 skipped" in out
    assert "Sync inserted 0 newer tweet(s)." in out
    assert ("GET", "/2/users/me") in client.calls


def test_sync_cli_reports_api_connection_failure(cfg, capsys):
    class Boom:
        user_id = "1"

        def get_me(self) -> dict:
            raise XApiError(401, "Unauthorized")

    rc = cmd_sync(cfg, argparse.Namespace(), client=Boom())  # type: ignore[arg-type]
    assert rc == 2
    err = capsys.readouterr().err
    assert "API connection failed" in err
    assert "Unauthorized" in err


def test_sync_timeline_reports_saves_and_media(cfg, db, archive_zip):
    import_archive(archive_zip, db, cfg.media_dir)
    newer = str(int(T2025_PART1) + 10)
    older = str(int(T2025_PART1) - 10)
    class Unfiltered(FakeXClient):
        def user_timeline(self, since_id=None, *, on_progress=None):
            self.calls.append(("GET", f"/2/users/{self.user_id}/tweets"))
            return list(self.timeline)

    client = Unfiltered()
    client.timeline = [
        {"id": older, "text": "old", "created_at": "2024-01-01T00:00:00.000Z"},
        {
            "id": newer,
            "text": "new photo",
            "created_at": "2026-02-01T00:00:00.000Z",
            "attachments": {"media_keys": ["3_abc"]},
            "_media": [
                {
                    "media_key": "3_abc",
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/abc.jpg",
                }
            ],
        },
    ]
    notes: list[str] = []
    result = sync_timeline(
        db,
        client,
        media_dir=cfg.media_dir,
        fetch_bytes=lambda _url: MINI_JPEG,
        on_progress=notes.append,
    )
    assert result.inserted == 1
    assert result.media_downloaded == 1
    text = "\n".join(notes)
    assert "1 tweet(s) to save; 1 skipped" in text
    assert f"Saving 1/1 {newer}" in text
    assert f"Downloading media 1/1 for {newer}: {newer}-3_abc.jpg" in text


def test_user_timeline_reports_each_pull(cfg):
    cfg.oauth2_access_token = "tok"
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/2/users/9/tweets"
        assert request.url.params["since_id"] == "1"
        assert "public_metrics" in request.url.params["tweet.fields"]
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "2", "text": "new"}],
                    "meta": {"result_count": 1, "next_token": "abc"},
                },
                headers={"x-rate-limit-remaining": "1499"},
            )
        assert request.url.params["pagination_token"] == "abc"
        return httpx.Response(
            200,
            json={
                "data": [{"id": "3", "text": "newer"}],
                "meta": {"result_count": 1},
            },
            headers={"x-rate-limit-remaining": "1498"},
        )

    client = LiveXClient(cfg=cfg, user_id="9", transport=httpx.MockTransport(handler))
    notes: list[str] = []
    tweets = client.user_timeline(since_id="1", on_progress=notes.append)
    client.close()
    assert [t["id"] for t in tweets] == ["2", "3"]
    text = "\n".join(notes)
    assert "Timeline pulls to make: at most 32 (100 tweets each, cap 3200, since_id=1)." in text
    assert "Pull 1 of up to 32…" in text
    assert "Pull 1: 1 tweet(s), 1 so far; another pull follows (at most 31 left); rate limit 1499 remaining." in text
    assert "Pull 2: 1 tweet(s), 2 so far; last page; rate limit 1498 remaining." in text
    assert "Fetched 2 tweet(s) in 2 pull(s)." in text
