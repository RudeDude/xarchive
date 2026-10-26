import json
import sqlite3
from pathlib import Path

from xarchive.archive import import_archive
from xarchive.db import Database
from xarchive.ops import query_tweets

from tests.conftest import (
    NOTE_PHRASE,
    T2018,
    T2019_XYLO,
    T2021_RT,
    T2022_MEDIA,
    T2025_NOTE,
    T2025_PART1,
    XYLOPHONE,
    write_archive_zip,
)


def test_import_query_fts_media_and_note_join(cfg, db, archive_zip):
    result = import_archive(archive_zip, db, cfg.media_dir)
    assert result.total >= 8
    assert db.count() == 9
    rows = query_tweets(db)
    ids = {t.id for t in rows}
    assert T2018 in ids
    assert T2025_PART1 in ids  # part1
    # FTS distinctive word
    hits = query_tweets(db, text=XYLOPHONE)
    assert [t.id for t in hits] == [T2019_XYLO]
    # media copied and linked
    media_file = cfg.media_dir / T2022_MEDIA / f"{T2022_MEDIA}-photo.jpg"
    assert media_file.is_file()
    linked = db.media_for(T2022_MEDIA)
    assert linked
    assert linked[0].local_path.endswith(f"{T2022_MEDIA}-photo.jpg")
    media_rows = query_tweets(db, media=True)
    assert any(t.id == T2022_MEDIA for t in media_rows)
    # note-tweet join: distinctive long-form word is not in tweets.js full_text
    note = db.get(T2025_NOTE)
    assert note is not None
    assert NOTE_PHRASE in note.text
    fts_note = query_tweets(db, text=NOTE_PHRASE)
    assert any(t.id == T2025_NOTE for t in fts_note)
    # RT classified from retweeted_status
    rt = db.get(T2021_RT)
    assert rt is not None
    assert rt.tweet_type == "retweet"
    assert rt.retweeted_id == "9998887770000000000"
    assert db.get(T2019_XYLO).like_count == 12
    assert db.get(T2018).like_count == 0
    by_likes = query_tweets(db, sort="likes")
    assert by_likes[0].id == T2019_XYLO
    assert db.get(T2022_MEDIA).retweet_count == 7
    by_rts = query_tweets(db, sort="retweets")
    assert by_rts[0].id == T2022_MEDIA
    # types
    types = {t.id: t.tweet_type for t in db.iter_all()}
    assert types[T2018] == "original"
    # missing community folder did not crash (fixture has none)
    assert result.account_id == "5166878"


def test_legacy_sqlite_gains_like_count_on_open(tmp_path):
    """Pre-metrics caches used to fail import with 'no such column: like_count'."""
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE tweets (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            created_at_ts INTEGER NOT NULL,
            text TEXT NOT NULL,
            tweet_type TEXT NOT NULL,
            in_reply_to_id TEXT,
            retweeted_id TEXT,
            lang TEXT,
            source TEXT,
            raw_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'cached',
            delete_error TEXT,
            has_media INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO tweets (
            id, created_at, created_at_ts, text, tweet_type, raw_json,
            has_media, imported_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("1", "2018-01-01T00:00:00Z", 1514764800, "hi", "original", "{}", 0, "t", "t"),
    )
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(tweets)")}
        assert "like_count" in cols
        assert "impression_count" in cols
        row = db.get("1")
        assert row is not None
        assert row.like_count is None
    finally:
        db.close()


def test_reimport_idempotent_and_preserves_marks(cfg, db, archive_zip):
    import_archive(archive_zip, db, cfg.media_dir)
    n = db.count()
    db.set_status(T2018, "keep")
    db.set_status(T2019_XYLO, "delete_marked")
    result = import_archive(archive_zip, db, cfg.media_dir)
    assert db.count() == n
    assert result.updated == n
    assert result.inserted == 0
    assert db.get(T2018).status == "keep"
    assert db.get(T2019_XYLO).status == "delete_marked"
    # content still patched
    assert XYLOPHONE in db.get(T2019_XYLO).text


def test_import_from_directory(cfg, db, archive_zip, tmp_path):
    import zipfile

    dest = tmp_path / "extracted"
    dest.mkdir()
    with zipfile.ZipFile(archive_zip) as zf:
        zf.extractall(dest)
    import_archive(dest, db, cfg.media_dir)
    assert db.count() == 9


def test_reimport_without_media_keeps_local_paths(cfg, db, archive_zip, tmp_path):
    import zipfile

    import_archive(archive_zip, db, cfg.media_dir)
    linked = db.media_for(T2022_MEDIA)
    assert linked and linked[0].local_path
    dest = tmp_path / "text_only"
    dest.mkdir()
    with zipfile.ZipFile(archive_zip) as zf:
        for name in zf.namelist():
            if name.endswith("/") or "tweets_media" in name.replace("\\", "/"):
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
    import_archive(dest, db, cfg.media_dir)
    linked2 = db.media_for(T2022_MEDIA)
    assert linked2 and linked2[0].local_path
    assert (cfg.media_dir / T2022_MEDIA / f"{T2022_MEDIA}-photo.jpg").is_file()


def test_reimport_skips_unchanged_media(cfg, db, archive_zip):
    first = import_archive(archive_zip, db, cfg.media_dir)
    assert first.media_copied == 1
    from tests.conftest import T2022_MEDIA

    media_file = cfg.media_dir / T2022_MEDIA / f"{T2022_MEDIA}-photo.jpg"
    mtime = media_file.stat().st_mtime
    second = import_archive(archive_zip, db, cfg.media_dir)
    assert second.media_copied == 0
    assert second.media_skipped == 1
    assert media_file.stat().st_mtime == mtime


def test_deleted_tweets_js_marks_id_already_in_tweets_js(cfg, db, tmp_path):
    z = tmp_path / "with-deleted.zip"
    payload = [
        {
            "tweet": {
                "id_str": T2018,
                "id": T2018,
                "full_text": "hello from the beginning of the archive",
                "created_at": "Thu Mar 01 12:00:00 +0000 2018",
            }
        }
    ]
    body = f"window.YTD.deleted_tweets.part0 = {json.dumps(payload)};\n"
    write_archive_zip(
        z,
        extra_members={
            "twitter-2026-01-01-deadbeef/data/deleted-tweets.js": body.encode(),
        },
    )
    import_archive(z, db, cfg.media_dir)
    assert db.get(T2018).status == "deleted"
    assert db.get(T2019_XYLO).status == "cached"


def test_missing_community_folder_ok(cfg, db, tmp_path):
    z = tmp_path / "nocomm.zip"
    write_archive_zip(z)  # no community_tweet.js
    import_archive(z, db, cfg.media_dir)
    assert db.count() == 9
