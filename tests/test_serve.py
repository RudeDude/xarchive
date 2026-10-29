import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from xarchive.archive import import_archive
from xarchive.ops import mark_tweets
from xarchive.serve import make_handler, page_numbers, safe_redirect_target, serve_forever
from tests.conftest import T2018, T2019_XYLO, T2022_MEDIA, T2025_PART1, XYLOPHONE


def test_safe_redirect_target_keeps_local_query_rejects_open_redirect():
    assert safe_redirect_target("/?text=xylophone", None) == "/?text=xylophone"
    assert (
        safe_redirect_target(None, "http://127.0.0.1:8080/?text=foo&status=keep")
        == "/?text=foo&status=keep"
    )
    assert safe_redirect_target("//evil.example/", "http://example.com/") == "/"
    assert safe_redirect_target("https://evil.example/steal", None) == "/"
    assert safe_redirect_target("/?q=1", None, fragment_id="99") == "/?q=1#t99"


def test_refuse_non_localhost(cfg):
    rc = serve_forever(cfg, host="0.0.0.0", port=9)
    assert rc == 2


def test_index_renders_and_mark_flips_status(cfg, db, archive_zip):
    import_archive(archive_zip, db, cfg.media_dir)
    handler = make_handler(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        base = f"http://{host}:{port}"
        r = httpx.get(base + "/", timeout=5.0)
        assert r.status_code == 200
        assert XYLOPHONE in r.text
        assert T2019_XYLO in r.text
        # thumbnail for media tweet
        assert "-photo.jpg" in r.text
        post = httpx.post(
            base + "/mark",
            data={"id": T2019_XYLO, "status": "keep"},
            timeout=5.0,
            follow_redirects=False,
        )
        assert post.status_code in (303, 302)
        search = httpx.get(base + "/?text=xylophone&status=keep", timeout=5.0)
        assert T2019_XYLO in search.text
        assert 'name="next" value="/?text=xylophone&amp;status=keep"' in search.text
        stay = httpx.post(
            base + "/mark",
            data={
                "id": T2019_XYLO,
                "status": "delete_marked",
                "next": "/?text=xylophone",
            },
            headers={"Referer": f"{base}/?text=xylophone"},
            timeout=5.0,
            follow_redirects=False,
        )
        assert stay.status_code in (303, 302)
        assert stay.headers["Location"].startswith("/?text=xylophone")
        assert stay.headers["Location"].endswith(f"#t{T2019_XYLO}")
        assert "reply to @someone" in r.text
        assert f'data-status-id="{T2018}"' in r.text
        assert "Preview on X" in r.text
        assert "hello from the beginning of the archive" in r.text
        assert "12 likes" in r.text
        assert 'name="sort"' in r.text
        by_likes = httpx.get(base + "/?sort=likes", timeout=5.0)
        assert by_likes.text.find(f'id="t{T2019_XYLO}"') < by_likes.text.find(
            f'id="t{T2025_PART1}"'
        )
        assert "most RTs" in r.text
        assert "oldest" in r.text
        assert "least likes" in r.text
        assert "least RTs" in r.text
        assert "least views" in r.text
        assert 'aria-label="Browse by month"' in r.text
        assert "from=2018-03-01" in r.text
        assert "to=2018-03-31" in r.text
        assert r.text.count("<details open>") == 1
        by_rts = httpx.get(base + "/?sort=retweets", timeout=5.0)
        assert by_rts.text.find(f'id="t{T2022_MEDIA}"') < by_rts.text.find(
            f'id="t{T2019_XYLO}"'
        )
        assert "sort=retweets&amp;from=2018-03-01&amp;to=2018-03-31" in by_rts.text
        oldest = httpx.get(base + "/?sort=date_asc", timeout=5.0)
        assert oldest.text.find(f'id="t{T2018}"') < oldest.text.find(
            f'id="t{T2025_PART1}"'
        )
        march = httpx.get(base + "/?from=2018-03-01&to=2018-03-31", timeout=5.0)
        assert T2018 in march.text
        assert T2019_XYLO not in march.text
        assert 'class="month here"' in march.text
        assert march.text.count("<details open>") == 1
        opened = march.text.find("<details open>")
        assert ">2018</a>" in march.text[opened:opened + 500]
        json_mark = httpx.post(
            base + "/mark",
            data={"id": T2019_XYLO, "status": "keep"},
            headers={"Accept": "application/json"},
            timeout=5.0,
            follow_redirects=False,
        )
        assert json_mark.status_code == 200
        assert "Location" not in json_mark.headers
        payload = json_mark.json()
        assert payload["status"] == "keep"
        assert T2019_XYLO in payload["ids"]
        assert payload["counts"]["keep"] >= 1
        via_referer = httpx.post(
            base + "/mark",
            data={"id": T2019_XYLO, "status": "keep"},
            headers={"Referer": f"{base}/?text=xylophone"},
            timeout=5.0,
            follow_redirects=False,
        )
        assert via_referer.headers["Location"].startswith("/?text=xylophone")
        db.close()
        from xarchive.db import Database

        db2 = Database(cfg.db_path)
        assert db2.get(T2019_XYLO).status == "keep"
        db2.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_page_numbers_includes_high_pages_and_gaps():
    nums = page_numbers(1, 39)
    assert nums[0] == 1
    assert nums[-1] == 39
    assert None in nums
    assert 36 in nums and 37 in nums and 38 in nums
    around = page_numbers(20, 39)
    assert 19 in around and 20 in around and 21 in around
    assert around[-1] == 39


def _serve(cfg):
    handler = make_handler(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    return httpd, f"http://{host}:{port}"


def test_ui_date_filter_pagination_and_bulk(cfg, db, archive_zip, monkeypatch):
    import xarchive.serve as serve_mod
    from xarchive.db import Database

    import_archive(archive_zip, db, cfg.media_dir)
    monkeypatch.setattr(serve_mod, "PAGE_SIZE", 3)
    httpd, base = _serve(cfg)
    try:
        dated = httpx.get(base + "/?from=2018-01-01&to=2018-12-31", timeout=5.0)
        assert dated.status_code == 200
        assert T2018 in dated.text
        assert T2019_XYLO not in dated.text
        p1 = httpx.get(base + "/", timeout=5.0)
        assert "page 1 of 3" in p1.text
        assert "Next" in p1.text
        assert "Last" in p1.text
        assert 'href="/?page=3"' in p1.text
        p2 = httpx.get(base + "/?page=2", timeout=5.0)
        assert "page 2 of 3" in p2.text
        assert 'name="page_status" value="delete_marked"' in p1.text
        bulk = httpx.post(
            base + "/mark",
            data={"ids": [T2018, T2019_XYLO], "status": "delete_marked", "next": "/"},
            timeout=5.0,
            follow_redirects=False,
        )
        assert bulk.status_code in (303, 302)
        assert bulk.headers["Location"].endswith(f"#t{T2018}")
        db.close()
        db2 = Database(cfg.db_path)
        assert db2.get(T2018).status == "delete_marked"
        assert db2.get(T2019_XYLO).status == "delete_marked"
        page_all = httpx.post(
            base + "/mark",
            data={
                "page_ids": [T2018, T2019_XYLO],
                "page_status": "keep",
                "next": "/",
            },
            timeout=5.0,
            follow_redirects=False,
        )
        assert page_all.status_code in (303, 302)
        db2.close()
        db3 = Database(cfg.db_path)
        assert db3.get(T2018).status == "keep"
        assert db3.get(T2019_XYLO).status == "keep"
        db3.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_serve_logs_exceptions_to_data_dir(cfg, monkeypatch):
    import xarchive.serve as serve_mod

    def boom(*_a, **_k):
        raise RuntimeError("serve-boom")

    monkeypatch.setattr(serve_mod, "render_index", boom)
    httpd, base = _serve(cfg)
    try:
        r = httpx.get(base + "/", timeout=5.0)
        assert r.status_code == 500
    finally:
        httpd.shutdown()
        httpd.server_close()
    log = cfg.serve_log_path.read_text(encoding="utf-8")
    assert "serve-boom" in log
    assert "Traceback" in log
