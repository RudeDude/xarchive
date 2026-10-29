# Changelog

## Unreleased

### Changed

- The local UI sort menu adds the reverse of each order: oldest, least likes, least RTs, and least views. A left-hand year/month tree jumps to that year or month; the year that contains the current range (or the newest year) starts expanded.
- SQLite, media, credentials, and `serve.log` default to `<checkout>/data` instead of `~/.local/share/xarchive`. The virtualenv stays in `<checkout>/.venv`. `./bin/xarchive` runs that interpreter so the checkout moves as one directory. `data/` is gitignored.
- Local UI pager includes First/Last plus numbered jumps (first pages, around the current page, and the last five) so you can start at the oldest posts and walk backward.
- Click a thumbnail (or the video surface above the controls) to enlarge it in place; click again to shrink.
- `xarchive serve` writes request errors and stack traces to `<data-dir>/serve.log` (also printed to stderr). Startup prints the log path.
- Import stores `favorite_count` / `retweet_count` from the ZIP. `sync` asks for `public_metrics` (likes, RTs, replies, quotes, bookmarks, impressions) on new timeline posts and backfills posts that have never been metrics-synced with `GET /2/tweets?ids=` (100/request, once). The UI and `xarchive query --sort likes|retweets|views` can sort by likes, retweets, or views.
- Local UI is denser (smaller buttons, tighter cards) and lists **100** posts per page. Keep / Mark delete / Clear sit on the meta row; Clear is hidden until a mark exists.
- Reply, quote, and retweet cards show a compact context row (`reply to @user`, quoted `@user`, `retweet of @user`). If the referenced post is in the local cache, its text is shown immediately. **Preview on X** loads `platform.twitter.com` embed iframes in the browser only (click, or optional Auto X previews). The serve process still does not call X.
- Keep / Mark delete in the UI updates the card in place over `fetch` (`Accept: application/json`) so the page does not reload or jump. Without JavaScript, POST `/mark` still 303s to `#t{id}` (including the first id of a bulk mark).
- CLI `query` prints 50 rows by default (`--limit N` or `--all`). Tests are local-only (`pytest`); GitHub Actions is not used.
- `xarchive sync` prints progress: API account connected, local cache size and newest id, how many timeline pulls it will make (at most 32 pages of 100, since the user-tweets endpoint does not return a total), each pull's tweet count and rate-limit remaining, then each tweet saved and each media file downloaded.

### Fixed

- Import now reads `data/tweet-headers.js` (and `data/tweet.js`). Official archives list every non-deleted post in the header file, while `tweets.js` may only include full text for a newer slice. Header-only posts are stored with their archive date. A header does not replace a post that already has text. Re-import the ZIP to pick up years that were missing.
- Opening an existing SQLite cache after the metrics columns were added no longer fails with `no such column: like_count`. The likes/views indexes are created only after `ALTER TABLE` adds the columns.
- OAuth 2.0 access tokens are refreshed automatically on expiry or HTTP 401 using the stored refresh token (`offline.access`). New tokens (including rotated refresh tokens and `expires_at`) are written to `credentials.json`. Weekly `sync` / `delete --apply` no longer requires a fresh `xarchive auth` every two hours.
- Keep / Mark delete in the local UI now returns to the same search (path + query), instead of always bouncing to `/`. Open redirects via `//host` or a non-localhost Referer are rejected.
- Archive import writes the SQLite cache in one transaction (not a commit per tweet) and skips media files whose on-disk size already matches the ZIP member. A one-time large import no longer does tens of thousands of commits, and a re-import does not recopy gigabytes of unchanged video.
- Re-importing a tweets.js-only archive no longer replaces local media rows with URL-only stubs. Existing `local_path` links (and files on disk) are kept.
- `xarchive mark delete --query` with no `--text/--from/--to/--media/--status` is refused. Pass `--all` to mark every tweet, or add a filter.
- Posts whose text starts with `RT @` are no longer classified as retweets unless the archive/API object has `retweeted_status` or `referenced_tweets` type `retweeted`. Quote-tweets and jokes that happen to start with `RT @` delete as originals (`DELETE /2/tweets/:id`), not as unretweets missing a source id.
- `xarchive sync` requests `expansions=attachments.media_keys` and downloads photos (and the highest-bitrate mp4 for video/GIF) into the same `{tweetId}/{tweetId}-{media_key}.*` layout as archive import. Incremental posts between ZIP downloads are no longer text-only.
- A tweet present in both `tweets.js` and `deleted-tweets.js` is now marked `deleted` (if it was still `cached`). Previously the first copy won and the deleted file was ignored.
