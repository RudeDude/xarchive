"""Localhost UI. Default bind 127.0.0.1. No auth, no tracking, no 0.0.0.0."""

from __future__ import annotations

import html
import json
import logging
import mimetypes
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from xarchive.config import Config
from xarchive.db import SORTS, Database, Tweet
from xarchive.ops import parse_date_bound
from xarchive.ytd import Ref, reference_targets

PAGE_SIZE = 100

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _as_local_path(raw: str) -> str | None:
    """Return a same-origin path+query, or None if the value is not localhost."""
    raw = (raw or "").strip()
    if not raw or raw.startswith("//") or raw.startswith("\\"):
        return None
    if raw.startswith("/"):
        path = raw.split("?", 1)[0]
        if "\\" in raw or ":" in path or not path.startswith("/"):
            return None
        return raw.split("#", 1)[0]
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host not in LOCAL_HOSTS:
        return None
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return None
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


def safe_redirect_target(
    candidate: str | None,
    referer: str | None,
    *,
    fragment_id: str | None = None,
) -> str:
    """Prefer a hidden `next` field, then Referer; always a local path."""
    loc = "/"
    for raw in (candidate, referer):
        path = _as_local_path(raw or "")
        if path:
            loc = path
            break
    if fragment_id:
        loc = loc.split("#", 1)[0] + "#t" + fragment_id
    return loc


CSS = """
:root { color-scheme: light; }
body { font-family: ui-sans-serif, system-ui, sans-serif; max-width: 880px;
       margin: 1rem auto; padding: 0 .75rem; line-height: 1.35; font-size: 15px; }
header { display: flex; flex-wrap: wrap; gap: .5rem 1.25rem; align-items: baseline; }
h1 { font-size: 1.1rem; margin: 0; }
nav a { margin-right: .85rem; }
.counts { color: #444; font-size: .8rem; margin: .4rem 0; }
form.search { display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; margin: .6rem 0; }
input, select, button { font: inherit; font-size: .8rem; padding: .12rem .4rem; }
.tweet { border: 1px solid #ddd; border-radius: 6px; padding: .4rem .6rem; margin: .35rem 0;
         scroll-margin-top: .5rem; }
.meta { color: #555; font-size: .8rem; display: flex; flex-wrap: wrap; gap: .2rem .45rem; align-items: center; }
.text { white-space: pre-wrap; margin: .25rem 0; }
.thumb { max-height: 72px; max-width: 140px; border-radius: 4px; cursor: zoom-in; }
.thumb.expanded { max-height: min(80vh, 720px); max-width: 100%; cursor: zoom-out; }
.thumbs { display: flex; flex-wrap: wrap; gap: .35rem; align-items: flex-start; }
.status-keep { color: #0a0; }
.status-delete_marked { color: #a40; }
.status-deleted { color: #888; }
.status-delete_failed { color: #a00; }
.actions { display: inline-flex; gap: .25rem; margin-left: auto; }
.notice { background: #f6f6f6; padding: .5rem .75rem; border-radius: 6px; font-size: .85rem; }
.bulk { display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; margin: .5rem 0; }
.pager { margin: .6rem 0; font-size: .85rem; display: flex; flex-wrap: wrap; gap: .35rem .55rem; align-items: center; }
.pager a, .pager .here, .pager .gap { margin: 0; }
.pager .here { font-weight: 650; }
.pager .gap { color: #888; }
.select { font-size: .8rem; color: #555; }
.context { font-size: .8rem; color: #444; margin: .15rem 0; }
.context a { color: inherit; }
.local-parent { color: #333; border-left: 2px solid #ccc; padding-left: .5rem; margin: .2rem 0;
                white-space: pre-wrap; max-height: 4.05em; overflow: hidden; font-size: .85rem; }
.preview-slot iframe { width: 100%; min-height: 280px; border: 0; margin-top: .25rem; }
.preview-slot { margin: .15rem 0; }
.toggle { font-size: .8rem; color: #444; }
"""

JS = r"""
(() => {
  const KEY = "xarchive.previews";
  const MAX = 3;
  const previewSrc = (id) =>
    "https://platform.twitter.com/embed/Tweet.html?id=" +
    encodeURIComponent(id) + "&dnt=true&conversation=none";

  function previewsOn() {
    try { return localStorage.getItem(KEY) === "1"; } catch { return false; }
  }

  function previewButton(slot, id) {
    const card = slot && slot.closest(".tweet");
    if (!card) return null;
    return card.querySelector('.preview-btn[data-status-id="' + CSS.escape(id) + '"]');
  }

  function injectPreview(slot) {
    if (!slot || slot.querySelector("iframe")) return;
    const id = slot.getAttribute("data-status-id");
    if (!id) return;
    const iframe = document.createElement("iframe");
    iframe.src = previewSrc(id);
    iframe.loading = "lazy";
    iframe.setAttribute(
      "sandbox",
      "allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox"
    );
    iframe.title = "X preview " + id;
    slot.appendChild(iframe);
    const btn = previewButton(slot, id);
    if (btn) btn.textContent = "Hide preview";
  }

  function hidePreview(slot) {
    if (!slot) return;
    slot.replaceChildren();
    const id = slot.getAttribute("data-status-id");
    const btn = previewButton(slot, id);
    if (btn) btn.textContent = "Preview on X";
  }

  function updateCard(card, status) {
    card.dataset.status = status;
    const label = card.querySelector(".status-label");
    if (label) {
      label.className = "status-label status-" + status;
      label.textContent = status;
    }
    const clearBtn = card.querySelector(".clear-mark");
    if (clearBtn) {
      if (status === "keep" || status === "delete_marked") clearBtn.removeAttribute("hidden");
      else clearBtn.setAttribute("hidden", "");
    }
  }

  function updateCounts(counts) {
    const el = document.querySelector(".counts");
    if (!el || !counts) return;
    el.textContent = Object.entries(counts).map(([k, v]) => k + "=" + v).join(" · ");
  }

  let nativeSubmit = false;
  document.addEventListener("submit", async (ev) => {
    if (nativeSubmit) return;
    const form = ev.target;
    if (!(form instanceof HTMLFormElement)) return;
    let path = "";
    try { path = new URL(form.action, location.href).pathname; } catch { return; }
    if (path !== "/mark") return;
    ev.preventDefault();
    const body = new URLSearchParams();
    const fd = new FormData(form);
    const submitter = ev.submitter;
    if (submitter instanceof HTMLButtonElement && submitter.name) {
      fd.set(submitter.name, submitter.value);
    }
    for (const [k, v] of fd.entries()) body.append(k, String(v));
    try {
      const res = await fetch("/mark", {
        method: "POST",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body,
      });
      if (!res.ok) throw new Error("mark failed");
      const data = await res.json();
      const status = data.status;
      const ids = data.ids || [];
      const onReview = location.pathname === "/review";
      for (const id of ids) {
        const card = document.getElementById("t" + id);
        if (!card) continue;
        if (onReview && status !== "delete_marked") {
          card.remove();
          continue;
        }
        updateCard(card, status);
      }
      updateCounts(data.counts);
    } catch {
      nativeSubmit = true;
      if (submitter instanceof HTMLButtonElement && submitter.name) {
        const hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = submitter.name;
        hidden.value = submitter.value;
        form.appendChild(hidden);
      }
      form.submit();
    }
  });

  document.addEventListener("click", (ev) => {
    const btn = ev.target instanceof Element ? ev.target.closest(".preview-btn") : null;
    if (!btn) return;
    const id = btn.getAttribute("data-status-id");
    if (!id) return;
    const card = btn.closest(".tweet");
    const slot = card && card.querySelector('.preview-slot[data-status-id="' + CSS.escape(id) + '"]');
    if (slot && slot.querySelector("iframe")) hidePreview(slot);
    else injectPreview(slot);
  });

  const queue = [];
  let active = 0;
  function pump() {
    while (active < MAX && queue.length) {
      const slot = queue.shift();
      if (!slot || slot.querySelector("iframe")) continue;
      active += 1;
      injectPreview(slot);
      const iframe = slot.querySelector("iframe");
      const done = () => { active = Math.max(0, active - 1); pump(); };
      if (iframe) {
        iframe.addEventListener("load", done, { once: true });
        iframe.addEventListener("error", done, { once: true });
      } else {
        done();
      }
    }
  }

  let io = null;
  function observePreviews() {
    if (!previewsOn() || io) return;
    io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        io.unobserve(e.target);
        queue.push(e.target);
        pump();
      }
    }, { rootMargin: "200px" });
    document.querySelectorAll(".preview-slot[data-status-id]").forEach((s) => io.observe(s));
  }

  const toggle = document.getElementById("previews-toggle");
  if (toggle instanceof HTMLInputElement) {
    toggle.checked = previewsOn();
    toggle.addEventListener("change", () => {
      try { localStorage.setItem(KEY, toggle.checked ? "1" : "0"); } catch {}
      if (toggle.checked) observePreviews();
    });
  }
  if (previewsOn()) observePreviews();

  document.addEventListener("click", (ev) => {
    const el = ev.target;
    if (!(el instanceof Element)) return;
    const thumb = el.closest(".thumb");
    if (!thumb) return;
    if (thumb.tagName === "VIDEO") {
      const rect = thumb.getBoundingClientRect();
      if (ev.clientY > rect.bottom - 36) return;
    }
    thumb.classList.toggle("expanded");
  });
})();
"""


def _page(title: str, body: str) -> bytes:
    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{CSS}</style>
</head>
<body>
{body}
<script>{JS}</script>
</body>
</html>
"""
    return doc.encode("utf-8")


def _counts_html(counts: dict[str, int]) -> str:
    bits = " · ".join(f"{html.escape(k)}={v}" for k, v in counts.items())
    return f'<p class="counts">{bits}</p>'


def _sort_key(qs: dict[str, list[str]]) -> str:
    raw = (qs.get("sort") or ["date"])[0]
    return raw if raw in SORTS else "date"


def _page_num(qs: dict[str, list[str]]) -> int:
    try:
        return max(1, int((qs.get("page") or ["1"])[0]))
    except (TypeError, ValueError):
        return 1


def _query_url(path: str, qs: dict[str, list[str]], *, page: int | None = None) -> str:
    d: dict[str, str] = {}
    for k, vs in qs.items():
        if k == "page" or not vs or vs[0] == "":
            continue
        d[k] = vs[0]
    if page is not None and page > 1:
        d["page"] = str(page)
    encoded = urllib.parse.urlencode(d)
    if encoded:
        return f"{path}?{encoded}"
    return path


def page_numbers(current: int, pages: int) -> list[int | None]:
    """Page links: first two, around current, and the last five (None = gap)."""
    if pages < 1:
        return []
    wanted: set[int] = {1, 2, current, current - 1, current + 1}
    wanted.update(range(pages - 4, pages + 1))
    nums = sorted(p for p in wanted if 1 <= p <= pages)
    out: list[int | None] = []
    prev = 0
    for n in nums:
        if prev and n > prev + 1:
            out.append(None)
        out.append(n)
        prev = n
    return out


def _pager(path: str, qs: dict[str, list[str]], page: int, total: int, page_size: int) -> str:
    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    bits: list[str] = []
    if page > 1:
        bits.append(f'<a href="{html.escape(_query_url(path, qs, page=1))}">First</a>')
        bits.append(f'<a href="{html.escape(_query_url(path, qs, page=page - 1))}">Prev</a>')
    for n in page_numbers(page, pages):
        if n is None:
            bits.append('<span class="gap">…</span>')
            continue
        if n == page:
            bits.append(f'<span class="here">{n}</span>')
        else:
            bits.append(f'<a href="{html.escape(_query_url(path, qs, page=n))}">{n}</a>')
    if page < pages:
        bits.append(f'<a href="{html.escape(_query_url(path, qs, page=page + 1))}">Next</a>')
        bits.append(f'<a href="{html.escape(_query_url(path, qs, page=pages))}">Last</a>')
    bits.append(f"page {page} of {pages} · {total} tweet(s)")
    return f'<p class="pager">{" ".join(bits)}</p>'


def _bulk_bar(next_path: str, page_ids: list[str]) -> str:
    hiddens = "\n".join(
        f'<input type="hidden" name="page_ids" value="{html.escape(i)}">' for i in page_ids
    )
    return f"""
<form id="bulk" class="bulk" method="post" action="/mark">
  <input type="hidden" name="next" value="{html.escape(next_path)}">
  {hiddens}
  <button name="status" value="keep">Keep selected</button>
  <button name="status" value="delete_marked">Mark selected delete</button>
  <button name="status" value="cached">Clear selected</button>
  <button name="page_status" value="keep">Keep all on this page</button>
  <button name="page_status" value="delete_marked">Mark all on this page delete</button>
  <button name="page_status" value="cached">Clear all on this page</button>
</form>
"""


def _status_url(ref: Ref) -> str | None:
    if not ref.status_id:
        return None
    if ref.screen_name:
        return f"https://x.com/{urllib.parse.quote(ref.screen_name)}/status/{urllib.parse.quote(ref.status_id)}"
    return f"https://x.com/i/status/{urllib.parse.quote(ref.status_id)}"


def _ref_phrase(ref: Ref) -> str:
    who = f"@{ref.screen_name}" if ref.screen_name else ""
    if ref.kind == "reply":
        return f"reply to {who}".strip() if who else "reply"
    if ref.kind == "quote":
        return f"quoted {who}".strip() if who else "quoted post"
    return f"retweet of {who}".strip() if who else "retweet"


def _context_html(refs: list[Ref], parents: dict[str, Tweet]) -> str:
    if not refs:
        return ""
    bits: list[str] = []
    extras: list[str] = []
    seen_slots: set[str] = set()
    for ref in refs:
        phrase = html.escape(_ref_phrase(ref))
        url = _status_url(ref)
        if ref.status_id and url:
            sid = html.escape(ref.status_id)
            bits.append(
                f'{phrase} · <a href="{html.escape(url)}" target="_blank" rel="noopener">{sid}</a>'
                f' <button type="button" class="preview-btn" data-status-id="{sid}">Preview on X</button>'
            )
            parent = parents.get(ref.status_id)
            if parent and parent.text:
                extras.append(f'<div class="local-parent">{html.escape(parent.text)}</div>')
            if ref.status_id not in seen_slots:
                extras.append(f'<div class="preview-slot" data-status-id="{sid}"></div>')
                seen_slots.add(ref.status_id)
        else:
            bits.append(phrase)
    return f'<div class="context">{" · ".join(bits)}</div>' + "".join(extras)


def _refs_for(t: Tweet) -> list[Ref]:
    return reference_targets(
        t.raw_json,
        text=t.text,
        in_reply_to_id=t.in_reply_to_id,
        retweeted_id=t.retweeted_id,
        tweet_id=t.id,
    )


def _tweet_card(
    db: Database,
    t: Any,
    *,
    show_actions: bool = True,
    next_path: str = "/",
    refs: list[Ref] | None = None,
    parents: dict[str, Tweet] | None = None,
) -> str:
    media = db.media_for(t.id)
    thumbs = []
    for m in media:
        if not m.local_path or not m.filename:
            continue
        src = f"/media/{urllib.parse.quote(t.id)}/{urllib.parse.quote(m.filename)}"
        if (m.media_type or "").startswith("video") or (m.filename or "").endswith(".mp4"):
            thumbs.append(f'<video class="thumb" src="{html.escape(src)}" controls></video>')
        else:
            thumbs.append(
                f'<img class="thumb" src="{html.escape(src)}" alt="" title="Click to enlarge">'
            )
    actions = ""
    if show_actions and t.status not in ("deleted",):
        clear_hidden = "" if t.status in ("keep", "delete_marked") else " hidden"
        actions = f"""
        <form class="actions" method="post" action="/mark">
          <input type="hidden" name="id" value="{html.escape(t.id)}">
          <input type="hidden" name="next" value="{html.escape(next_path)}">
          <button name="status" value="keep">Keep</button>
          <button name="status" value="delete_marked">Mark delete</button>
          <button name="status" value="cached" class="clear-mark"{clear_hidden}>Clear mark</button>
        </form>"""
    select = ""
    if show_actions and t.status not in ("deleted",):
        select = (
            f'<label class="select"><input form="bulk" type="checkbox" name="ids" '
            f'value="{html.escape(t.id)}"> select</label>'
        )
    context = _context_html(refs or [], parents or {})
    metrics = _metrics_html(t)
    return f"""
<article class="tweet" id="t{html.escape(t.id)}" data-id="{html.escape(t.id)}" data-status="{html.escape(t.status)}">
  <div class="meta">{select}{html.escape(t.created_at)} · {html.escape(t.tweet_type)} ·
    <span class="status-label status-{html.escape(t.status)}">{html.escape(t.status)}</span>{metrics} ·
    id={html.escape(t.id)}{actions}</div>
  {context}
  <div class="text">{html.escape(t.text)}</div>
  {('<div class="thumbs">' + ''.join(thumbs) + '</div>') if thumbs else ''}
</article>
"""


def _metrics_html(t: Tweet) -> str:
    bits: list[str] = []
    if t.like_count is not None:
        bits.append(f"{t.like_count} like{'s' if t.like_count != 1 else ''}")
    if t.retweet_count:
        bits.append(f"{t.retweet_count} RT")
    if t.impression_count is not None:
        bits.append(f"{t.impression_count} views")
    if not bits:
        return ""
    return " · " + html.escape(" · ".join(bits))


def _sort_options(current: str) -> str:
    labels = {
        "date": "newest",
        "likes": "most likes",
        "retweets": "most RTs",
        "views": "most views",
    }
    return "".join(
        f'<option value="{html.escape(k)}"{" selected" if current == k else ""}>{html.escape(labels[k])}</option>'
        for k in labels
    )


def _cards_for(db: Database, rows: list[Tweet], *, next_path: str) -> str:
    ref_lists = [_refs_for(t) for t in rows]
    parent_ids = [r.status_id for refs in ref_lists for r in refs if r.status_id]
    parents = db.get_many(parent_ids)
    cards = "\n".join(
        _tweet_card(db, t, next_path=next_path, refs=refs, parents=parents)
        for t, refs in zip(rows, ref_lists, strict=True)
    )
    return cards or "<p>No tweets. Run <code>xarchive import</code>.</p>"


def render_index(db: Database, qs: dict[str, list[str]]) -> bytes:
    text = (qs.get("text") or [""])[0]
    status = (qs.get("status") or [""])[0] or None
    media = (qs.get("media") or [""])[0] in {"1", "on", "true"}
    from_date = (qs.get("from") or [""])[0]
    to_date = (qs.get("to") or [""])[0]
    page = _page_num(qs)
    sort = _sort_key(qs)
    from_ts = parse_date_bound(from_date or None, end=False)
    to_ts = parse_date_bound(to_date or None, end=True)
    total = db.count_matching(
        text=text or None, status=status, media=media, from_ts=from_ts, to_ts=to_ts
    )
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
    page = min(page, pages)
    rows = db.query(
        text=text or None,
        status=status,
        media=media,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
        sort=sort,
    )
    counts = db.status_counts()
    options = "".join(
        f'<option value="{html.escape(s)}"{" selected" if status == s else ""}>{html.escape(s)}</option>'
        for s in ("", "cached", "keep", "delete_marked", "deleted", "delete_failed")
    )
    next_path = _query_url("/", qs, page=page)
    page_ids = [t.id for t in rows if t.status != "deleted"]
    cards = _cards_for(db, rows, next_path=next_path)
    if not rows:
        cards = "<p>No tweets. Run <code>xarchive import</code>.</p>"
    body = f"""
<header>
  <h1>xarchive</h1>
  <nav>
    <a href="/">All</a>
    <a href="/review">Review deletes</a>
  </nav>
  <label class="toggle"><input type="checkbox" id="previews-toggle"> Auto X previews</label>
</header>
{_counts_html(counts)}
<form class="search" method="get" action="/">
  <input name="text" value="{html.escape(text)}" placeholder="search">
  <select name="status">{options}</select>
  <label><input type="checkbox" name="media" value="1" {"checked" if media else ""}> media</label>
  <input type="date" name="from" value="{html.escape(from_date)}" title="from">
  <input type="date" name="to" value="{html.escape(to_date)}" title="to">
  <select name="sort" title="sort">{_sort_options(sort)}</select>
  <button type="submit">Search</button>
</form>
<p class="notice">Local only (127.0.0.1). Apply deletes from the CLI:
<code>xarchive delete</code> then <code>xarchive delete --apply --confirm PERMANENTLY-DELETE</code>.
The server does not call X; <strong>Preview on X</strong> loads an embed in your browser.</p>
{_bulk_bar(next_path, page_ids)}
{_pager("/", qs, page, total, PAGE_SIZE)}
{cards}
{_pager("/", qs, page, total, PAGE_SIZE)}
"""
    return _page("xarchive", body)


def render_review(db: Database, qs: dict[str, list[str]] | None = None) -> bytes:
    qs = qs or {}
    page = _page_num(qs)
    sort = _sort_key(qs)
    total = db.count_matching(status="delete_marked")
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
    page = min(page, pages)
    rows = db.query(
        status="delete_marked",
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
        sort=sort,
    )
    counts = db.status_counts()
    next_path = _query_url("/review", qs, page=page)
    page_ids = [t.id for t in rows]
    cards = _cards_for(db, rows, next_path=next_path)
    if not rows:
        cards = "<p>Nothing marked for delete.</p>"
    body = f"""
<header>
  <h1>Review before apply</h1>
  <nav><a href="/">Back</a></nav>
  <label class="toggle"><input type="checkbox" id="previews-toggle"> Auto X previews</label>
</header>
{_counts_html(counts)}
<p class="notice">{total} tweet(s) marked delete_marked will be sent to the official X API on
<code>xarchive delete --apply --confirm PERMANENTLY-DELETE</code>.
Retweets unretweet the source id; originals DELETE /2/tweets/:id.
This UI does not call X. <strong>Preview on X</strong> loads an embed in your browser.</p>
<form class="search" method="get" action="/review">
  <select name="sort" title="sort">{_sort_options(sort)}</select>
  <button type="submit">Sort</button>
</form>
{_bulk_bar(next_path, page_ids)}
{_pager("/review", qs, page, total, PAGE_SIZE)}
{cards}
"""
    return _page("xarchive review", body)


def configure_serve_logging(log_path: Path) -> logging.Logger:
    log = logging.getLogger("xarchive.serve")
    log.setLevel(logging.INFO)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


def make_handler(cfg: Config, log: logging.Logger | None = None) -> type[BaseHTTPRequestHandler]:
    data_dir = cfg.data_dir
    db_path = cfg.db_path
    media_root = cfg.media_dir.resolve()
    logger = log or configure_serve_logging(cfg.serve_log_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            logger.info("%s - %s", self.address_string(), fmt % args)

        def log_error(self, fmt: str, *args: object) -> None:
            logger.error("%s - %s", self.address_string(), fmt % args)

        def _oops(self, where: str) -> None:
            logger.exception("%s %s", where, self.path)
            try:
                self._send(500, b"internal error (see serve.log)", "text/plain; charset=utf-8")
            except Exception:
                logger.exception("failed to send 500 for %s", self.path)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _db(self) -> Database:
            return Database(db_path)

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/" or parsed.path == "/index":
                    db = self._db()
                    try:
                        self._send(200, render_index(db, qs), "text/html; charset=utf-8")
                    finally:
                        db.close()
                    return
                if parsed.path == "/review":
                    db = self._db()
                    try:
                        self._send(200, render_review(db, qs), "text/html; charset=utf-8")
                    finally:
                        db.close()
                    return
                if parsed.path.startswith("/media/"):
                    self._serve_media(parsed.path)
                    return
                self._send(404, b"not found", "text/plain; charset=utf-8")
            except Exception:
                self._oops("GET")

        def _serve_media(self, path: str) -> None:
            rel = path[len("/media/") :]
            parts = [urllib.parse.unquote(p) for p in rel.split("/") if p]
            if len(parts) != 2 or ".." in parts[0] or ".." in parts[1]:
                self._send(400, b"bad media path", "text/plain; charset=utf-8")
                return
            dest = (media_root / parts[0] / parts[1]).resolve()
            if media_root not in dest.parents and dest != media_root:
                self._send(400, b"bad media path", "text/plain; charset=utf-8")
                return
            if not dest.is_file():
                self._send(404, b"missing", "text/plain; charset=utf-8")
                return
            ctype = mimetypes.guess_type(dest.name)[0] or "application/octet-stream"
            data = dest.read_bytes()
            self._send(200, data, ctype)

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._do_post()
            except Exception:
                self._oops("POST")

        def _do_post(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b""
            form = urllib.parse.parse_qs(raw.decode("utf-8"))
            if parsed.path == "/mark":
                allowed = {"keep", "delete_marked", "cached"}
                page_status = (form.get("page_status") or [""])[0]
                status = page_status or (form.get("status") or [""])[0]
                tid = (form.get("id") or [""])[0]
                if page_status:
                    ids = [i for i in (form.get("page_ids") or []) if i]
                elif tid:
                    ids = [tid]
                else:
                    ids = [i for i in (form.get("ids") or []) if i]
                if status not in allowed:
                    self._send(400, b"bad mark", "text/plain; charset=utf-8")
                    return
                db = self._db()
                try:
                    for i in ids:
                        row = db.get(i)
                        if row and row.status != "deleted":
                            db.set_status(i, status, None)
                    counts = db.status_counts()
                finally:
                    db.close()
                accept = self.headers.get("Accept") or ""
                if "application/json" in accept:
                    payload = json.dumps(
                        {"ids": ids, "status": status, "counts": counts}
                    ).encode("utf-8")
                    self._send(200, payload, "application/json; charset=utf-8")
                    return
                self.send_response(303)
                nxt = (form.get("next") or [""])[0]
                loc = safe_redirect_target(
                    nxt,
                    self.headers.get("Referer"),
                    fragment_id=tid or (ids[0] if ids else None),
                )
                self.send_header("Location", loc)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


class _ServeHTTPServer(ThreadingHTTPServer):
    logger: logging.Logger

    def handle_error(self, request: object, client_address: object) -> None:
        log = getattr(self, "logger", None) or logging.getLogger("xarchive.serve")
        log.exception("unhandled error from %s", client_address)


def serve_forever(cfg: Config, host: str = "127.0.0.1", port: int = 8080) -> int:
    if host not in LOCAL_HOSTS:
        print(
            f"error: refusing to bind {host!r}. Default is 127.0.0.1 (localhost only).",
            file=sys.stderr,
        )
        return 2
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    log = configure_serve_logging(cfg.serve_log_path)
    handler = make_handler(cfg, log=log)
    httpd = _ServeHTTPServer((host, port), handler)
    httpd.logger = log
    bound_host, bound_port = httpd.server_address[:2]
    print(f"xarchive UI on http://{bound_host}:{bound_port}/  (Ctrl-C to stop)")
    print(f"logging to {cfg.serve_log_path}")
    log.info("listening on http://%s:%s/", bound_host, bound_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        log.info("stopped")
    finally:
        httpd.server_close()
    return 0
