from xarchive.archive import import_archive
from xarchive.cli import main
from xarchive.ops import FakeClock, apply_deletes, mark_tweets, query_tweets
from tests.conftest import T2018, T2019_XYLO, T2020_REPLY, T2021_RT, T2023
from tests.fake_client import FakeXClient


def _import(cfg, db, archive_zip):
    import_archive(archive_zip, db, cfg.media_dir)


def test_mark_query_without_filters_refuses(cfg, db, archive_zip, capsys):
    _import(cfg, db, archive_zip)
    rc = main(["--data-dir", str(cfg.data_dir), "mark", "delete", "--query"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--all" in err
    assert db.get(T2018).status == "cached"


def test_mark_query_all_marks_everything(cfg, db, archive_zip):
    _import(cfg, db, archive_zip)
    rc = main(["--data-dir", str(cfg.data_dir), "mark", "delete", "--query", "--all"])
    assert rc == 0
    assert db.get(T2018).status == "delete_marked"
    assert db.get(T2019_XYLO).status == "delete_marked"


def test_mark_query_with_filter_ok(cfg, db, archive_zip):
    _import(cfg, db, archive_zip)
    rc = main(
        ["--data-dir", str(cfg.data_dir), "mark", "delete", "--query", "--from", "2018-01-01", "--to", "2018-12-31"]
    )
    assert rc == 0
    assert db.get(T2018).status == "delete_marked"
    assert db.get(T2019_XYLO).status == "cached"


def test_mark_and_dry_run_lists_exactly_those_three(cfg, db, archive_zip, capsys):
    _import(cfg, db, archive_zip)
    mark_tweets(db, "delete", [T2018, T2020_REPLY, T2021_RT])
    mark_tweets(db, "keep", [T2019_XYLO, T2023])
    assert db.get(T2019_XYLO).status == "keep"
    rc = main(["--data-dir", str(cfg.data_dir), "delete"])
    assert rc == 0
    out = capsys.readouterr().out
    assert T2018 in out and T2020_REPLY in out and T2021_RT in out
    assert T2019_XYLO not in out
    assert T2023 not in out
    assert "Dry-run" in out
    assert "3 tweet(s)" in out or "3 tweet" in out


def test_apply_deletes_records_and_second_apply_noop(cfg, db, archive_zip):
    _import(cfg, db, archive_zip)
    mark_tweets(db, "delete", [T2018, T2020_REPLY, T2021_RT])
    mark_tweets(db, "keep", [T2019_XYLO, T2023])
    client = FakeXClient()
    report = apply_deletes(db, client)
    assert set(report.deleted) == {T2018, T2020_REPLY, T2021_RT}
    assert db.get(T2018).status == "deleted"
    assert db.get(T2020_REPLY).status == "deleted"
    assert db.get(T2021_RT).status == "deleted"
    assert db.get(T2019_XYLO).status == "keep"
    # RT used unretweet endpoint, not DELETE /2/tweets/someone-elses-id
    assert client.unretweeted == ["9998887770000000000"]
    assert ("DELETE", f"/2/users/{client.user_id}/retweets/9998887770000000000") in client.calls
    assert all(not c[1].endswith("/9998887770000000000") or "retweets" in c[1] for c in client.calls)
    assert ("DELETE", f"/2/tweets/{T2018}") in client.calls
    assert ("DELETE", f"/2/tweets/{T2021_RT}") not in client.calls
    n_calls = len(client.calls)
    report2 = apply_deletes(db, client)
    assert report2.deleted == []
    assert len(client.calls) == n_calls  # no-op


def test_429_then_success_uses_fake_clock(cfg, db, archive_zip):
    _import(cfg, db, archive_zip)
    mark_tweets(db, "delete", [T2018])
    client = FakeXClient()
    client.fail_429_then_ok(f"/2/tweets/{T2018}", times=1)
    clock = FakeClock()
    report = apply_deletes(db, client, clock=clock)
    assert report.deleted == [T2018]
    assert db.get(T2018).status == "deleted"
    assert clock.sleeps == [7]
    assert client.deleted == [T2018]


def test_404_already_gone_is_success(cfg, db, archive_zip):
    _import(cfg, db, archive_zip)
    mark_tweets(db, "delete", [T2018])
    client = FakeXClient()
    client.gone_404(f"/2/tweets/{T2018}")
    report = apply_deletes(db, client)
    assert T2018 in report.deleted
    assert T2018 in report.already_gone
    assert db.get(T2018).status == "deleted"
