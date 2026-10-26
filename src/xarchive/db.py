"""SQLite + FTS5 cache. Status marks are never overwritten on import merge."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from xarchive import STATUSES

SCHEMA = """
CREATE TABLE IF NOT EXISTS tweets (
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
    like_count INTEGER,
    retweet_count INTEGER,
    reply_count INTEGER,
    quote_count INTEGER,
    bookmark_count INTEGER,
    impression_count INTEGER,
    metrics_at TEXT,
    imported_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS tweets_fts USING fts5(
    text,
    content='tweets',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS tweets_ai AFTER INSERT ON tweets BEGIN
    INSERT INTO tweets_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS tweets_ad AFTER DELETE ON tweets BEGIN
    INSERT INTO tweets_fts(tweets_fts, rowid, text)
        VALUES('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS tweets_au AFTER UPDATE ON tweets BEGIN
    INSERT INTO tweets_fts(tweets_fts, rowid, text)
        VALUES('delete', old.rowid, old.text);
    INSERT INTO tweets_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id TEXT NOT NULL,
    media_type TEXT,
    original_url TEXT,
    local_path TEXT,
    filename TEXT,
    FOREIGN KEY (tweet_id) REFERENCES tweets(id)
);

CREATE INDEX IF NOT EXISTS idx_media_tweet ON media(tweet_id);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at_ts);
CREATE INDEX IF NOT EXISTS idx_tweets_status ON tweets(status);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Tweet:
    id: str
    created_at: str
    created_at_ts: int
    text: str
    tweet_type: str
    in_reply_to_id: str | None
    retweeted_id: str | None
    lang: str | None
    source: str | None
    raw_json: str
    status: str
    delete_error: str | None
    has_media: bool
    imported_at: str = ""
    updated_at: str = ""
    like_count: int | None = None
    retweet_count: int | None = None
    reply_count: int | None = None
    quote_count: int | None = None
    bookmark_count: int | None = None
    impression_count: int | None = None
    metrics_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Tweet":
        keys = row.keys()
        return cls(
            id=row["id"],
            created_at=row["created_at"],
            created_at_ts=int(row["created_at_ts"]),
            text=row["text"],
            tweet_type=row["tweet_type"],
            in_reply_to_id=row["in_reply_to_id"],
            retweeted_id=row["retweeted_id"],
            lang=row["lang"],
            source=row["source"],
            raw_json=row["raw_json"],
            status=row["status"],
            delete_error=row["delete_error"],
            has_media=bool(row["has_media"]),
            imported_at=row["imported_at"] if "imported_at" in keys else "",
            updated_at=row["updated_at"] if "updated_at" in keys else "",
            like_count=_row_int(row, "like_count"),
            retweet_count=_row_int(row, "retweet_count"),
            reply_count=_row_int(row, "reply_count"),
            quote_count=_row_int(row, "quote_count"),
            bookmark_count=_row_int(row, "bookmark_count"),
            impression_count=_row_int(row, "impression_count"),
            metrics_at=row["metrics_at"] if "metrics_at" in keys else None,
        )


@dataclass
class Media:
    tweet_id: str
    media_type: str | None
    original_url: str | None
    local_path: str | None
    filename: str | None


def _row_int(row: sqlite3.Row, key: str) -> int | None:
    if key not in row.keys():
        return None
    val = row[key]
    if val is None:
        return None
    return int(val)


def _row_to_tweet(row: sqlite3.Row) -> Tweet:
    return Tweet.from_row(row)


SORTS = {
    "date": "tweets.created_at_ts DESC, CAST(tweets.id AS INTEGER) DESC",
    "likes": "tweets.like_count IS NULL, tweets.like_count DESC, tweets.created_at_ts DESC",
    "retweets": "tweets.retweet_count IS NULL, tweets.retweet_count DESC, tweets.created_at_ts DESC",
    "views": "tweets.impression_count IS NULL, tweets.impression_count DESC, tweets.created_at_ts DESC",
}

METRIC_COLUMNS = (
    ("like_count", "INTEGER"),
    ("retweet_count", "INTEGER"),
    ("reply_count", "INTEGER"),
    ("quote_count", "INTEGER"),
    ("bookmark_count", "INTEGER"),
    ("impression_count", "INTEGER"),
    ("metrics_at", "TEXT"),
)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._tx_depth = 0
        self._init()

    def _init(self) -> None:
        self._conn.executescript(SCHEMA)
        # ALTER first: SCHEMA's CREATE INDEX IF NOT EXISTS on new columns would
        # fail on a pre-metrics cache ("no such column: like_count") and skip migrate.
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tweets)")}
        if not cols:
            return
        for name, decl in METRIC_COLUMNS:
            if name not in cols:
                self._conn.execute(f"ALTER TABLE tweets ADD COLUMN {name} {decl}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tweets_likes ON tweets(like_count)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tweets_retweets ON tweets(retweet_count)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tweets_impressions ON tweets(impression_count)"
        )

    def _commit(self) -> None:
        if self._tx_depth == 0:
            self._conn.commit()

    @contextmanager
    def transaction(self):
        self._tx_depth += 1
        try:
            yield
            if self._tx_depth == 1:
                self._conn.commit()
        except Exception:
            if self._tx_depth == 1:
                self._conn.rollback()
            raise
        finally:
            self._tx_depth -= 1

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._commit()

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM tweets").fetchone()["n"])

    def status_counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for row in self._conn.execute("SELECT status, COUNT(*) AS n FROM tweets GROUP BY status"):
            out[row["status"]] = int(row["n"])
        return out

    def get(self, tweet_id: str) -> Tweet | None:
        row = self._conn.execute("SELECT * FROM tweets WHERE id = ?", (tweet_id,)).fetchone()
        return _row_to_tweet(row) if row else None

    def get_many(self, ids: Iterable[str]) -> dict[str, Tweet]:
        uniq = [i for i in dict.fromkeys(ids) if i]
        if not uniq:
            return {}
        out: dict[str, Tweet] = {}
        # SQLite caps bound variables (~999); a UI page is far smaller.
        chunk = 400
        for i in range(0, len(uniq), chunk):
            part = uniq[i : i + chunk]
            placeholders = ",".join("?" * len(part))
            rows = self._conn.execute(
                f"SELECT * FROM tweets WHERE id IN ({placeholders})",
                part,
            ).fetchall()
            for row in rows:
                out[row["id"]] = _row_to_tweet(row)
        return out

    def max_id(self) -> str | None:
        row = self._conn.execute(
            "SELECT id FROM tweets ORDER BY CAST(id AS INTEGER) DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    def upsert_content(
        self,
        *,
        tweet_id: str,
        created_at: str,
        created_at_ts: int,
        text: str,
        tweet_type: str,
        in_reply_to_id: str | None,
        retweeted_id: str | None,
        lang: str | None,
        source: str | None,
        raw_json: str,
        has_media: bool,
        now: str,
        initial_status: str = "cached",
        engagement: dict[str, int | None] | None = None,
        metrics_at: str | None = None,
    ) -> str:
        """INSERT OR IGNORE + patch content. Never overwrites keep/delete marks.

        Returns 'inserted' or 'updated'. Metric columns use COALESCE so a missing
        ZIP/API field does not wipe a previously synced value.
        """
        existing = self.get(tweet_id)
        eng = engagement or {}
        likes = eng.get("like_count")
        rts = eng.get("retweet_count")
        replies = eng.get("reply_count")
        quotes = eng.get("quote_count")
        bookmarks = eng.get("bookmark_count")
        impressions = eng.get("impression_count")
        self._conn.execute(
            """
            INSERT OR IGNORE INTO tweets (
                id, created_at, created_at_ts, text, tweet_type,
                in_reply_to_id, retweeted_id, lang, source, raw_json,
                status, has_media, like_count, retweet_count, reply_count,
                quote_count, bookmark_count, impression_count, metrics_at,
                imported_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tweet_id,
                created_at,
                created_at_ts,
                text,
                tweet_type,
                in_reply_to_id,
                retweeted_id,
                lang,
                source,
                raw_json,
                initial_status,
                1 if has_media else 0,
                likes,
                rts,
                replies,
                quotes,
                bookmarks,
                impressions,
                metrics_at,
                now,
                now,
            ),
        )
        self._conn.execute(
            """
            UPDATE tweets SET
                created_at = ?,
                created_at_ts = ?,
                text = ?,
                tweet_type = ?,
                in_reply_to_id = COALESCE(?, in_reply_to_id),
                retweeted_id = COALESCE(?, retweeted_id),
                lang = COALESCE(?, lang),
                source = COALESCE(?, source),
                raw_json = ?,
                has_media = ?,
                like_count = COALESCE(?, like_count),
                retweet_count = COALESCE(?, retweet_count),
                reply_count = COALESCE(?, reply_count),
                quote_count = COALESCE(?, quote_count),
                bookmark_count = COALESCE(?, bookmark_count),
                impression_count = COALESCE(?, impression_count),
                metrics_at = COALESCE(?, metrics_at),
                updated_at = ?
            WHERE id = ?
            """,
            (
                created_at,
                created_at_ts,
                text,
                tweet_type,
                in_reply_to_id,
                retweeted_id,
                lang,
                source,
                raw_json,
                1 if has_media else 0,
                likes,
                rts,
                replies,
                quotes,
                bookmarks,
                impressions,
                metrics_at,
                now,
                tweet_id,
            ),
        )
        self._commit()
        return "updated" if existing else "inserted"

    def set_engagement(
        self,
        tweet_id: str,
        *,
        like_count: int | None = None,
        retweet_count: int | None = None,
        reply_count: int | None = None,
        quote_count: int | None = None,
        bookmark_count: int | None = None,
        impression_count: int | None = None,
        metrics_at: str | None = None,
    ) -> None:
        """Patch engagement columns. NULL arguments leave the stored value."""
        self._conn.execute(
            """
            UPDATE tweets SET
                like_count = COALESCE(?, like_count),
                retweet_count = COALESCE(?, retweet_count),
                reply_count = COALESCE(?, reply_count),
                quote_count = COALESCE(?, quote_count),
                bookmark_count = COALESCE(?, bookmark_count),
                impression_count = COALESCE(?, impression_count),
                metrics_at = COALESCE(?, metrics_at)
            WHERE id = ?
            """,
            (
                like_count,
                retweet_count,
                reply_count,
                quote_count,
                bookmark_count,
                impression_count,
                metrics_at,
                tweet_id,
            ),
        )
        self._commit()

    def ids_missing_metrics(self) -> list[str]:
        """Non-deleted posts that have never been metrics-synced from the API."""
        rows = self._conn.execute(
            """
            SELECT id FROM tweets
            WHERE metrics_at IS NULL AND status != 'deleted'
            ORDER BY CAST(id AS INTEGER) DESC
            """
        ).fetchall()
        return [row["id"] for row in rows]

    def replace_media(self, tweet_id: str, items: Iterable[Media]) -> None:
        self._conn.execute("DELETE FROM media WHERE tweet_id = ?", (tweet_id,))
        for m in items:
            self._conn.execute(
                """
                INSERT INTO media (tweet_id, media_type, original_url, local_path, filename)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tweet_id, m.media_type, m.original_url, m.local_path, m.filename),
            )
        self._commit()

    def media_for(self, tweet_id: str) -> list[Media]:
        rows = self._conn.execute(
            "SELECT tweet_id, media_type, original_url, local_path, filename FROM media WHERE tweet_id = ?",
            (tweet_id,),
        ).fetchall()
        return [
            Media(
                tweet_id=r["tweet_id"],
                media_type=r["media_type"],
                original_url=r["original_url"],
                local_path=r["local_path"],
                filename=r["filename"],
            )
            for r in rows
        ]

    def set_status(self, tweet_id: str, status: str, error: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"invalid status: {status}")
        self._conn.execute(
            "UPDATE tweets SET status = ?, delete_error = ? WHERE id = ?",
            (status, error, tweet_id),
        )
        self._commit()

    def mark_many(self, ids: Iterable[str], status: str) -> int:
        if status not in ("keep", "delete_marked", "cached"):
            raise ValueError(f"cannot mark as {status}")
        n = 0
        for tid in ids:
            row = self.get(tid)
            if not row:
                continue
            if row.status in ("deleted",):
                continue
            self.set_status(tid, status, None)
            n += 1
        return n

    def query(
        self,
        *,
        text: str | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        media: bool = False,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        sort: str = "date",
    ) -> list[Tweet]:
        joins, where, params = self._filter_parts(
            text=text, from_ts=from_ts, to_ts=to_ts, media=media, status=status
        )
        order = SORTS.get(sort, SORTS["date"])
        sql = "SELECT tweets.* FROM tweets " + " ".join(joins)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
            if offset:
                sql += " OFFSET ?"
                params.append(offset)
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Fallback if FTS syntax still blows up.
            if not text:
                raise
            where2 = ["tweets.text LIKE ?"]
            params2: list[Any] = [f"%{text}%"]
            if from_ts is not None:
                where2.append("tweets.created_at_ts >= ?")
                params2.append(from_ts)
            if to_ts is not None:
                where2.append("tweets.created_at_ts <= ?")
                params2.append(to_ts)
            if media:
                where2.append("tweets.has_media = 1")
            if status:
                where2.append("tweets.status = ?")
                params2.append(status)
            sql = "SELECT * FROM tweets WHERE " + " AND ".join(where2)
            sql += " ORDER BY " + order.replace("tweets.", "")
            if limit is not None:
                sql += " LIMIT ?"
                params2.append(limit)
                if offset:
                    sql += " OFFSET ?"
                    params2.append(offset)
            rows = self._conn.execute(sql, params2).fetchall()
        return [_row_to_tweet(r) for r in rows]

    def count_matching(
        self,
        *,
        text: str | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        media: bool = False,
        status: str | None = None,
    ) -> int:
        joins, where, params = self._filter_parts(
            text=text, from_ts=from_ts, to_ts=to_ts, media=media, status=status
        )
        sql = "SELECT COUNT(*) AS n FROM tweets " + " ".join(joins)
        if where:
            sql += " WHERE " + " AND ".join(where)
        try:
            row = self._conn.execute(sql, params).fetchone()
        except sqlite3.OperationalError:
            if not text:
                raise
            return len(self.query(text=text, from_ts=from_ts, to_ts=to_ts, media=media, status=status))
        return int(row["n"]) if row else 0

    def _filter_parts(
        self,
        *,
        text: str | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        media: bool = False,
        status: str | None = None,
    ) -> tuple[list[str], list[str], list[Any]]:
        joins: list[str] = []
        where: list[str] = []
        params: list[Any] = []
        if text:
            joins.append("JOIN tweets_fts ON tweets_fts.rowid = tweets.rowid")
            safe = '"' + text.replace('"', " ") + '"'
            where.append("tweets_fts MATCH ?")
            params.append(safe)
        if from_ts is not None:
            where.append("tweets.created_at_ts >= ?")
            params.append(from_ts)
        if to_ts is not None:
            where.append("tweets.created_at_ts <= ?")
            params.append(to_ts)
        if media:
            where.append("tweets.has_media = 1")
        if status:
            where.append("tweets.status = ?")
            params.append(status)
        return joins, where, params

    def ids_matching(self, **kwargs: Any) -> list[str]:
        return [t.id for t in self.query(**kwargs)]

    def all_ids(self) -> list[str]:
        return [
            r["id"]
            for r in self._conn.execute("SELECT id FROM tweets ORDER BY CAST(id AS INTEGER)")
        ]

    def iter_all(self) -> Iterator[Tweet]:
        for row in self._conn.execute(
            "SELECT * FROM tweets ORDER BY created_at_ts DESC, CAST(id AS INTEGER) DESC"
        ):
            yield _row_to_tweet(row)
