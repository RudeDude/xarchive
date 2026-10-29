"""Import an official X archive ZIP (in-memory) or extracted directory."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from xarchive.db import Database, Media
from xarchive.ytd import (
    ACCOUNT_JS,
    COMMUNITY_JS,
    DELETED_JS,
    MEDIA_MEMBER,
    HEADERS_JS,
    NOTE_TWEET_JS,
    TWEETS_JS,
    classify_tweet,
    engagement_from_obj,
    index_note_tweets,
    is_skipped_archive_file,
    joined_text,
    media_refs,
    normalize_header_tweet,
    parse_account_id,
    parse_created_at,
    parse_ytd_js,
    posix_name,
    tweet_id_str,
    unwrap_item,
)

class ArchiveError(ValueError):
    """Archive could not be imported."""


class ZipSlipError(ArchiveError):
    """A zip member would escape the destination directory."""


@dataclass
class ImportResult:
    inserted: int = 0
    updated: int = 0
    media_copied: int = 0
    media_skipped: int = 0
    notes_joined: int = 0
    account_id: str | None = None
    files: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_zip_slip(member: str) -> bool:
    posix = posix_name(member)
    if posix.startswith("/") or posix.startswith("\\"):
        return True
    # Windows drive-letter
    if len(posix) >= 2 and posix[1] == ":":
        return True
    parts = [p for p in posix.split("/") if p and p != "."]
    if any(p == ".." or p.startswith("..") for p in parts):
        return True
    return False


def _safe_media_dest(media_root: Path, tweet_id: str, filename: str) -> Path:
    if _is_zip_slip(filename) or _is_zip_slip(tweet_id):
        raise ZipSlipError(filename)
    name = Path(filename).name
    if name != filename.replace("\\", "/").split("/")[-1]:
        raise ZipSlipError(filename)
    if name in {".", ".."} or ".." in name:
        raise ZipSlipError(filename)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        # Allow typical archive names like 123-photo.jpg; reject odd pathy names.
        raise ZipSlipError(filename)
    dest = (media_root / tweet_id / name).resolve()
    root = media_root.resolve()
    if root != dest and root not in dest.parents:
        raise ZipSlipError(filename)
    return dest


class _BlobSource:
    """Read archive members from a zip or a directory without a full extract."""

    def list_names(self) -> list[str]:
        raise NotImplementedError

    def read_text(self, name: str) -> str:
        raise NotImplementedError

    def read_bytes(self, name: str) -> bytes:
        raise NotImplementedError

    def size(self, name: str) -> int | None:
        raise NotImplementedError


class _ZipSource(_BlobSource):
    def __init__(self, zf: ZipFile):
        self.zf = zf
        self._names = [n for n in zf.namelist() if not n.endswith("/")]

    def list_names(self) -> list[str]:
        return self._names

    def read_text(self, name: str) -> str:
        with self.zf.open(name) as fh:
            return fh.read().decode("utf-8")

    def read_bytes(self, name: str) -> bytes:
        with self.zf.open(name) as fh:
            return fh.read()

    def size(self, name: str) -> int | None:
        try:
            return int(self.zf.getinfo(name).file_size)
        except KeyError:
            return None


class _DirSource(_BlobSource):
    def __init__(self, root: Path):
        self.root = root
        self._names: list[str] = []
        for p in root.rglob("*"):
            if p.is_file():
                self._names.append(str(p.relative_to(root)).replace("\\", "/"))

    def list_names(self) -> list[str]:
        return self._names

    def _path(self, name: str) -> Path:
        if _is_zip_slip(name):
            raise ZipSlipError(name)
        p = (self.root / name).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ZipSlipError(name)
        return p

    def read_text(self, name: str) -> str:
        return self._path(name).read_text(encoding="utf-8")

    def read_bytes(self, name: str) -> bytes:
        return self._path(name).read_bytes()

    def size(self, name: str) -> int | None:
        try:
            return int(self._path(name).stat().st_size)
        except OSError:
            return None


def _match(names: list[str], rx: re.Pattern[str]) -> list[str]:
    out = []
    for n in names:
        pn = posix_name(n)
        if is_skipped_archive_file(pn):
            continue
        if rx.search(pn):
            out.append(n)
    return sorted(out)


def _media_filename_tweet_id(filename: str) -> str | None:
    """Archive media is named ``{tweetId}-*.jpg|mp4|gif``."""
    base = Path(filename).name
    if "-" not in base:
        return None
    tid = base.split("-", 1)[0]
    if tid.isdigit():
        return tid
    return None


def import_archive(source: str | Path, db: Database, media_dir: Path) -> ImportResult:
    path = Path(source)
    if not path.exists():
        raise ArchiveError(f"not found: {path}")
    if path.is_dir():
        return _import_from_source(_DirSource(path), db, media_dir)
    if path.suffix.lower() == ".zip" or _looks_like_zip(path):
        with ZipFile(path, "r") as zf:
            _validate_zip_members(zf)
            return _import_from_source(_ZipSource(zf), db, media_dir)
    raise ArchiveError(f"expected a .zip or a directory, got {path}")


def _looks_like_zip(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"PK"
    except OSError:
        return False


def _validate_zip_members(zf: ZipFile) -> None:
    for info in zf.infolist():
        name = posix_name(info.filename)
        if info.is_dir() or name.endswith("/"):
            continue
        if MEDIA_MEMBER.search(name) and _is_zip_slip(name):
            raise ZipSlipError(f"zip-slip / path traversal rejected: {info.filename}")
        if _is_zip_slip(name) and MEDIA_MEMBER.search(name):
            raise ZipSlipError(f"zip-slip / path traversal rejected: {info.filename}")
        # Any tweets_media member with .. is fatal even if regex missed it.
        if "tweets_media" in name and _is_zip_slip(name):
            raise ZipSlipError(f"zip-slip / path traversal rejected: {info.filename}")


def _import_from_source(src: _BlobSource, db: Database, media_dir: Path) -> ImportResult:
    names = src.list_names()
    result = ImportResult()
    now = _now()
    media_dir.mkdir(parents=True, exist_ok=True)

    # Fail closed on zip-slip media names before touching the DB.
    for name in names:
        pn = posix_name(name)
        m = MEDIA_MEMBER.search(pn)
        if not m:
            continue
        inner = m.group(2)
        if _is_zip_slip(pn) or _is_zip_slip(inner):
            raise ZipSlipError(f"zip-slip / path traversal rejected: {name}")

    tweets_files = _match(names, TWEETS_JS)
    if not tweets_files:
        raise ArchiveError(
            "no data/tweets.js (or part files) found — this tool reads the official "
            "X archive ZIP (Settings → Your account → Download an archive of your data)"
        )
    result.files.extend(tweets_files)

    notes_by_tweet: dict[str, str] = {}
    notes_by_note: dict[str, str] = {}
    for nf in _match(names, NOTE_TWEET_JS):
        result.files.append(nf)
        items = parse_ytd_js(src.read_text(nf))
        bt, bn = index_note_tweets(items)
        notes_by_tweet.update(bt)
        notes_by_note.update(bn)

    account_files = _match(names, ACCOUNT_JS)
    for af in account_files:
        result.files.append(af)
        acc_id = parse_account_id(parse_ytd_js(src.read_text(af)))
        if acc_id:
            result.account_id = acc_id

    tweet_items: list[tuple[dict, bool]] = []  # (tweet dict, already_deleted)
    for tf in tweets_files:
        for item in parse_ytd_js(src.read_text(tf)):
            tweet_items.append((unwrap_item(item), False))

    # Optional; missing community file must not crash.
    for cf in _match(names, COMMUNITY_JS):
        result.files.append(cf)
        for item in parse_ytd_js(src.read_text(cf)):
            tweet_items.append((unwrap_item(item), False))

    for df in _match(names, DELETED_JS):
        result.files.append(df)
        for item in parse_ytd_js(src.read_text(df)):
            tweet_items.append((unwrap_item(item), True))

    # tweet-headers.js is the full id+date index. tweets.js may only carry
    # full text for a newer slice. Append headers after full objects so the
    # seen-set keeps the richer row and only missing ids are inserted.
    for hf in _match(names, HEADERS_JS):
        result.files.append(hf)
        for item in parse_ytd_js(src.read_text(hf)):
            tweet_items.append((normalize_header_tweet(unwrap_item(item)), False))

    # Copy media keyed by tweet id. {tweetId}-*.jpg|mp4|gif
    copied_by_tweet: dict[str, list[Media]] = {}
    for name in names:
        pn = posix_name(name)
        m = MEDIA_MEMBER.search(pn)
        if not m:
            continue
        inner = m.group(2)
        filename = Path(inner).name
        tid = _media_filename_tweet_id(filename)
        if not tid:
            continue
        dest = _safe_media_dest(media_dir, tid, filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src_size = src.size(name)
        if dest.is_file() and src_size is not None and dest.stat().st_size == src_size:
            result.media_skipped += 1
        else:
            dest.write_bytes(src.read_bytes(name))
            result.media_copied += 1
        rel = str(Path("media") / tid / filename)
        ext = dest.suffix.lower()
        mtype = {".mp4": "video", ".gif": "animated_gif"}.get(ext, "photo")
        copied_by_tweet.setdefault(tid, []).append(
            Media(
                tweet_id=tid,
                media_type=mtype,
                original_url=None,
                local_path=rel,
                filename=filename,
            )
        )

    with db.transaction():
        if result.account_id:
            db.set_meta("account_id", result.account_id)
        _upsert_tweets(
            db,
            tweet_items,
            notes_by_tweet,
            notes_by_note,
            copied_by_tweet,
            now,
            result,
        )
        db.set_meta("last_import", now)
    return result


def _upsert_tweets(
    db: Database,
    tweet_items: list[tuple[dict, bool]],
    notes_by_tweet: dict[str, str],
    notes_by_note: dict[str, str],
    copied_by_tweet: dict[str, list[Media]],
    now: str,
    result: ImportResult,
) -> None:
    seen: set[str] = set()
    for tweet, already_deleted in tweet_items:
        tid = tweet_id_str(tweet)
        if not tid:
            continue
        if tid in seen:
            if already_deleted:
                row = db.get(tid)
                if row and row.status == "cached":
                    db.set_status(tid, "deleted")
            continue
        seen.add(tid)
        text = joined_text(tweet, notes_by_tweet, notes_by_note)
        if tid in notes_by_tweet or (extract_note_used(tweet, notes_by_note, text)):
            result.notes_joined += 1
        created_at, ts = parse_created_at(tweet.get("created_at"))
        ttype, reply_id, rt_id = classify_tweet(tweet, text)
        refs = media_refs(tweet)
        local_media = copied_by_tweet.get(tid, [])
        has_media = bool(refs or local_media)
        initial = "deleted" if already_deleted else "cached"
        kind = db.upsert_content(
            tweet_id=tid,
            created_at=created_at,
            created_at_ts=ts,
            text=text,
            tweet_type=ttype,
            in_reply_to_id=reply_id,
            retweeted_id=rt_id,
            lang=tweet.get("lang"),
            source=tweet.get("source"),
            raw_json=json.dumps(tweet, ensure_ascii=False),
            has_media=has_media,
            now=now,
            initial_status=initial,
            engagement=engagement_from_obj(tweet),
        )
        if kind == "inserted":
            result.inserted += 1
        else:
            result.updated += 1
            # If this pass is from deleted-tweets.js and the row is still cached, mark gone.
            if already_deleted:
                row = db.get(tid)
                if row and row.status == "cached":
                    db.set_status(tid, "deleted")

        if local_media:
            db.replace_media(tid, local_media)
        elif refs:
            existing = db.media_for(tid)
            if not any(m.local_path for m in existing):
                stubs = []
                for ref in refs:
                    url = ref.get("media_url_https") or ref.get("media_url")
                    stubs.append(
                        Media(
                            tweet_id=tid,
                            media_type=ref.get("type"),
                            original_url=url,
                            local_path=None,
                            filename=None,
                        )
                    )
                db.replace_media(tid, stubs)


def extract_note_used(tweet: dict, notes_by_note: dict[str, str], text: str) -> bool:
    from xarchive.ytd import extract_inline_note

    note_id, inline = extract_inline_note(tweet)
    if inline and inline == text and len(text) > len(tweet.get("full_text") or ""):
        return True
    if note_id and note_id in notes_by_note:
        return True
    return False
