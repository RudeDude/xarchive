"""xarchive command-line interface."""

from __future__ import annotations

import argparse
import sys

import httpx

from xarchive import CONFIRM_PHRASE, __version__
from xarchive.archive import ArchiveError, ZipSlipError, import_archive
from xarchive.config import Config, load_config
from xarchive.db import Database
from xarchive.ops import apply_deletes, count_tweets, mark_tweets, query_tweets, sync_timeline
from xarchive.xclient import XApiError, XClientProtocol, build_client

_CLIENT_FACTORY = build_client  # tests monkeypatch this


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xarchive",
        description=(
            "Local-first X (Twitter) archive tool. Import the official data ZIP, "
            "search locally, mark keep/delete, and bulk-delete only marked posts "
            "via the official X API."
        ),
    )
    p.add_argument("--data-dir", default=None, help="SQLite + media dir (default <checkout>/data)")
    p.add_argument("--version", action="version", version=f"xarchive {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="Import an official X archive ZIP or extracted directory")
    imp.add_argument("path", help="archive.zip or extracted folder containing data/tweets.js")

    sub.add_parser("sync", help="Fetch newer posts via official user timeline (no-op without credentials)")

    q = sub.add_parser("query", help="Search the local cache")
    _add_query_flags(q)
    q.add_argument("--limit", type=int, default=50, help="max rows to print (default 50)")
    q.add_argument("--all", action="store_true", help="print every match (overrides --limit)")
    q.add_argument(
        "--sort",
        choices=("date", "likes", "retweets", "views"),
        default="date",
        help="order: date (default), likes, retweets, or views",
    )

    mk = sub.add_parser("mark", help="Mark tweets keep or delete")
    mk.add_argument("action", choices=["keep", "delete"], help="keep or delete (marks delete_marked)")
    mk.add_argument("ids", nargs="*", help="tweet ids")
    mk.add_argument("--query", action="store_true", help="mark every tweet matching the query flags")
    mk.add_argument(
        "--all",
        action="store_true",
        help="required with --query when no --text/--from/--to/--media/--status is given",
    )
    _add_query_flags(mk)

    d = sub.add_parser("delete", help="Delete tweets marked delete_marked via the official API")
    d.add_argument(
        "--apply",
        action="store_true",
        help="actually call the X API (default is dry-run)",
    )
    d.add_argument(
        "--confirm",
        default=None,
        help=f"required with --apply; must equal {CONFIRM_PHRASE}",
    )

    srv = sub.add_parser("serve", help="Localhost UI (default bind 127.0.0.1)")
    srv.add_argument("--host", default="127.0.0.1", help="bind address (localhost only)")
    srv.add_argument("--port", type=int, default=8080)

    auth = sub.add_parser("auth", help="OAuth 2.0 PKCE user-context login (binds 127.0.0.1)")
    auth.add_argument("--host", default="127.0.0.1")
    auth.add_argument("--port", type=int, default=8787)

    return p


def _add_query_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--text", default=None, help="FTS5 search")
    p.add_argument("--from", dest="from_date", default=None, help="YYYY-MM-DD inclusive")
    p.add_argument("--to", dest="to_date", default=None, help="YYYY-MM-DD inclusive")
    p.add_argument("--media", action="store_true", help="only tweets with media")
    p.add_argument("--status", default=None, help="cached|keep|delete_marked|deleted|delete_failed")


def _open(cfg: Config) -> Database:
    return Database(cfg.db_path)


def _print_tweets(rows) -> None:
    if not rows:
        print("0 tweets")
        return
    for t in rows:
        preview = " ".join(t.text.split())
        if len(preview) > 96:
            preview = preview[:93] + "..."
        bits = [
            t.id,
            t.created_at,
            f"{t.tweet_type:<8}",
            f"{t.status:<14}",
        ]
        if t.like_count is not None:
            bits.append(f"likes={t.like_count}")
        if t.retweet_count is not None:
            bits.append(f"rts={t.retweet_count}")
        if t.impression_count is not None:
            bits.append(f"views={t.impression_count}")
        bits.append(preview)
        print("  ".join(bits))
    print(f"{len(rows)} tweet(s)")


def cmd_import(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        result = import_archive(args.path, db, cfg.media_dir)
    except ZipSlipError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ArchiveError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        db.close()
    media_bit = f"{result.media_copied} media file(s)"
    if result.media_skipped:
        media_bit += f" ({result.media_skipped} already present)"
    print(
        f"Imported {result.total} tweet(s) "
        f"({result.inserted} new, {result.updated} updated); "
        f"{media_bit}; "
        f"{result.notes_joined} note-tweet join(s)."
    )
    return 0


def cmd_query(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        limit = None if args.all else args.limit
        total = count_tweets(
            db,
            text=args.text,
            from_date=args.from_date,
            to_date=args.to_date,
            media=args.media,
            status=args.status,
        )
        rows = query_tweets(
            db,
            text=args.text,
            from_date=args.from_date,
            to_date=args.to_date,
            media=args.media,
            status=args.status,
            limit=limit,
            sort=getattr(args, "sort", "date") or "date",
        )
        _print_tweets(rows)
        if limit is not None and total > len(rows):
            print(f"showing {len(rows)} of {total} (pass --all or --limit N)")
        counts = db.status_counts()
        print(
            "counts: "
            + ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        )
    finally:
        db.close()
    return 0


def cmd_mark(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        ids = list(args.ids)
        if args.query:
            constrained = bool(
                args.text or args.from_date or args.to_date or args.media or args.status
            )
            if not constrained and not args.all:
                print(
                    "error: --query without filters would mark every tweet; "
                    "pass --all or add --text/--from/--to/--media/--status",
                    file=sys.stderr,
                )
                return 2
            ids.extend(
                t.id
                for t in query_tweets(
                    db,
                    text=args.text,
                    from_date=args.from_date,
                    to_date=args.to_date,
                    media=args.media,
                    status=args.status,
                )
            )
        if not ids:
            print("error: provide tweet ids or --query", file=sys.stderr)
            return 2
        n = mark_tweets(db, args.action, ids)
        print(f"Marked {n} tweet(s) as {'delete_marked' if args.action == 'delete' else 'keep'}.")
    finally:
        db.close()
    return 0


def cmd_delete(cfg: Config, args: argparse.Namespace, client: XClientProtocol | None = None) -> int:
    db = _open(cfg)
    try:
        marked = query_tweets(db, status="delete_marked")
        print(f"{len(marked)} tweet(s) marked delete_marked:")
        _print_tweets(marked)
        if not args.apply:
            print(
                "Dry-run only. Pass --apply --confirm "
                f"{CONFIRM_PHRASE} to delete via the official X API."
            )
            print("Retweets use DELETE /2/users/:id/retweets/:source_tweet_id; originals use DELETE /2/tweets/:id.")
            print("Rate limit: 50 deletes / 15 minutes; about $0.010 per request (no batch).")
            return 0
        if args.confirm != CONFIRM_PHRASE:
            if sys.stdin.isatty() and args.confirm is None:
                typed = input(f"Type {CONFIRM_PHRASE} to confirm permanent delete: ").strip()
                if typed != CONFIRM_PHRASE:
                    print("aborted", file=sys.stderr)
                    return 2
            else:
                print(
                    f"error: --apply requires --confirm {CONFIRM_PHRASE}",
                    file=sys.stderr,
                )
                return 2
        if client is None:
            if not cfg.has_user_auth():
                print(
                    "error: no X API credentials (see .env.example). Live X is only used for sync/delete.",
                    file=sys.stderr,
                )
                return 2
            client = _CLIENT_FACTORY(cfg, user_id=db.get_meta("account_id"))
        report = apply_deletes(db, client)
        print(
            f"Deleted {len(report.deleted)} (already-gone {len(report.already_gone)}), "
            f"failed {len(report.failed)}."
        )
        return 0 if not report.failed else 1
    finally:
        db.close()


def cmd_sync(cfg: Config, args: argparse.Namespace, client: XClientProtocol | None = None) -> int:
    if client is None and not cfg.has_user_auth():
        print("No X API credentials configured; skipping sync.")
        return 0
    db = _open(cfg)
    try:
        if client is None:
            client = _CLIENT_FACTORY(cfg, user_id=db.get_meta("account_id"))
        try:
            me = client.get_me()
        except (XApiError, httpx.HTTPError) as exc:
            print(f"error: API connection failed: {exc}", file=sys.stderr)
            return 2
        username = str(me.get("username") or "").strip()
        uid = str(me.get("id") or getattr(client, "user_id", "") or "").strip() or "?"
        who = f"@{username}" if username else "authenticated user"
        print(f"API connected as {who} ({uid}).", flush=True)
        newest = db.max_id()
        print(
            f"Local cache: {db.count()} tweet(s); newest id {newest or '(none)'}.",
            flush=True,
        )
        result = sync_timeline(
            db,
            client,
            media_dir=cfg.media_dir,
            on_progress=lambda msg: print(msg, flush=True),
        )
        print(f"Sync inserted {result.inserted} newer tweet(s).")
        if result.media_downloaded:
            print(f"Downloaded {result.media_downloaded} media file(s).")
        if result.metrics_lookups:
            print(
                f"Updated metrics for {result.metrics_updated} tweet(s) "
                f"({result.metrics_lookups} lookup request(s))."
            )
    finally:
        db.close()
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from xarchive.serve import serve_forever

    return serve_forever(cfg, host=args.host, port=args.port)


def cmd_auth(cfg: Config, args: argparse.Namespace) -> int:
    from xarchive.oauth import run_pkce_login

    try:
        run_pkce_login(cfg, host=args.host, port=args.port)
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("OAuth tokens saved under the data dir (not printed).")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_config(args.data_dir)
    handlers = {
        "import": cmd_import,
        "query": cmd_query,
        "mark": cmd_mark,
        "delete": cmd_delete,
        "sync": cmd_sync,
        "serve": cmd_serve,
        "auth": cmd_auth,
    }
    return handlers[args.cmd](cfg, args)


def entry() -> None:
    raise SystemExit(main())
