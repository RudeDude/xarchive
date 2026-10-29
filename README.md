# xarchive

Local-first archive of **your** X (Twitter) posts and media. Re-run it every few weeks.

Import the **official** data archive ZIP, search and browse on your machine, mark posts keep/delete, then bulk-delete **only** the posts you marked — through the **official X API**. Nothing leaves localhost except those explicit `sync` / `delete --apply` calls.

CLI name: `xarchive`. Default UI bind: **127.0.0.1**. Licensed under the [MIT License](LICENSE).

## Not supported

Cookie sessions, undocumented GraphQL `DeleteTweet`, scrapers, Cyd, tweetXer, twitter-archive-parser, and similar tools are **unsupported**. This project will not bind `0.0.0.0`. DMs, ads, Grok chats, unlikes, and unfollows are out of scope (X removed likes/follows write endpoints from self-serve in April 2026). Bookmarks are not in the ZIP.

## Install

Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

The virtualenv lives in `.venv` inside this checkout. Run `./bin/xarchive` (it uses `.venv/bin/python -m xarchive`).

SQLite, media, and credentials default to `data/` in this checkout (`--data-dir` overrides). `data/` is gitignored. Copy the whole directory to move the code, the install, and the archive together.

## 1. Request your archive

On X: **Settings → Your account → Download an archive of your data**.

Wait for the email, download the ZIP. Inside you want `data/tweets.js` (or older `data/tweet.js`, plus `tweets-partN.js` when the export is split) and `data/tweets_media/{tweetId}-*.jpg|mp4|gif`. `data/tweet-headers.js` is the full id-and-date index; posts that appear only there are kept, and a later full object is not overwritten by an empty header. Long posts live in `data/note-tweet.js` and are joined automatically.

## 2. Import

```bash
xarchive import ~/Downloads/twitter-*.zip
# re-run is safe: same count, keep/delete marks are never overwritten
xarchive import ~/Downloads/twitter-*.zip
```

The ZIP is read in-memory (`zipfile`); it is not fully extracted. Path-traversal (zip-slip) media names are rejected.

## 3. Browse

```bash
xarchive serve          # http://127.0.0.1:8080/
xarchive serve --port 8080 --host 127.0.0.1
# errors/stack traces: data/serve.log
```

Chronological list (100 per page, First/Last plus page-number jumps), search, date range, and a left-hand year/month tree (click a year or month to jump there). Sort by newest or oldest, and by most or least likes, RTs, and views. Local thumbnails (click to enlarge), keep/delete toggles, checkboxes for bulk mark on the current page, counts, and a **review** page of posts marked for delete. Replies/quotes/retweets show a compact context row (`reply to @user`, local parent text when that post is in the cache). **Preview on X** loads an official embed in the browser (the server still does not call X). Keep/delete marks apply in place without a full reload. No auth, no tracking. Apply still happens in the CLI (with a confirmation phrase).

Likes and retweet counts come from the ZIP (`favorite_count` / `retweet_count`). View (impression) counts are not in the archive; `xarchive sync` requests `public_metrics` on new timeline posts and backfills the rest with batched `GET /2/tweets?ids=` (100 ids per request, once per post). A 4k history is about 40 lookups the first time, then zero on later syncs.

## 4. Search

```bash
xarchive query                 # first 50 matches + counts
xarchive query --all
xarchive query --text xylophone
xarchive query --from 2019-01-01 --to 2019-12-31
xarchive query --sort likes
xarchive query --sort retweets
xarchive query --sort views
xarchive query --media --status cached
```

## 5. Mark

```bash
xarchive mark keep 1140000000000000002
xarchive mark delete 970000000000000001 1450000000000000004
xarchive mark delete --query --from 2018-01-01 --to 2018-12-31
# --query with no filters is refused; pass --all to mark every tweet
xarchive mark delete --query --all
```

Statuses: `cached` | `keep` | `delete_marked` | `deleted` | `delete_failed`.

## 6. Dry-run delete (default)

```bash
xarchive delete
```

Prints exactly the posts with status `delete_marked`. Does **not** call X.

## 7. Apply (official API only)

```bash
xarchive delete --apply --confirm PERMANENTLY-DELETE
```

You must type the confirmation string. Then:

| Kind | Official endpoint |
| --- | --- |
| Your original / reply | `DELETE https://api.x.com/2/tweets/:id` |
| Your retweet | `DELETE https://api.x.com/2/users/:id/retweets/:source_tweet_id` |

A retweet is classified from the archive (`retweeted_status` or `referenced_tweets` type `retweeted`). A `full_text` that merely starts with `RT @` is not enough. **xarchive will not** `DELETE /2/tweets/:id` on someone else's id.

- **404** (already gone) counts as success and is recorded `deleted`.
- **HTTP 429** and `x-rate-limit-*` are honored; the run waits and retries.
- Limit: **50 deletes / 15 minutes**, no batch. About **$0.010 per request** on current X API pricing — budget accordingly.
- There is no v2 “delete media” call; deleting the post unpublishes attachments. **Local media copies are kept.**
- Re-runs skip ids already stored as `deleted`.

## 8. Auth (needed for sync / apply)

OAuth **2.0 PKCE** user-context is the intended path.

Scopes: `tweet.read tweet.write users.read offline.access`

```bash
cp .env.example data/.env
# set X_OAUTH2_CLIENT_ID (callback http://127.0.0.1:8787/callback)
xarchive auth
```

OAuth 1.0a user-context keys (API key/secret + access token/secret) are also accepted. See `.env.example`. Tokens live in env or `<data-dir>/credentials.json` (mode 600) and are **never printed**.

Access tokens expire after about two hours. With `offline.access`, `sync` and `delete --apply` refresh them automatically from the stored refresh token (and persist the new pair). Re-run `xarchive auth` only if refresh fails.

`sync` and `delete --apply` are the only commands that talk to live X. Without credentials, `sync` is a no-op.

## 9. Later re-run (every few weeks)

1. Download a fresh official archive ZIP (full history).
2. `xarchive import new-archive.zip` — merge; marks stay put; text/media update.
3. Optional: `xarchive sync` — `GET /2/users/:id/tweets?since_id=` for posts newer than the cache max id (~3200 cap, owned-read), including attached media. **Not** a full-history source; never `search/all`.
4. Browse, mark, `xarchive delete`, then `--apply` when you mean it.

## Tests

Run locally:

```bash
.venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -q
```

## Layout

SQLite + FTS5 under `--data-dir`, media files next to the DB keyed by tweet id.

```
xarchive import <archive.zip|dir>
xarchive sync
xarchive query [--text] [--from] [--to] [--media] [--status] [--sort date|likes|retweets|views] [--limit|--all]
xarchive mark keep|delete <id...> or --query [--all]
xarchive delete              # dry-run
xarchive delete --apply      # confirmation required
xarchive serve [--host 127.0.0.1] [--port]
xarchive --help
```
