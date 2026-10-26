from xarchive.cli import main
from xarchive import CONFIRM_PHRASE


def test_help():
    try:
        main(["--help"])
    except SystemExit as e:
        assert e.code == 0
    else:
        raise AssertionError("expected SystemExit from --help")


def test_import_query_roundtrip_cli(cfg, archive_zip, capsys):
    rc = main(["--data-dir", str(cfg.data_dir), "import", str(archive_zip)])
    assert rc == 0
    rc = main(["--data-dir", str(cfg.data_dir), "query", "--text", "xylophone"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1140000000000000002" in out
    assert "xylophone" in out.lower() or "played" in out


def test_query_default_limit_and_all(cfg, archive_zip, capsys):
    main(["--data-dir", str(cfg.data_dir), "import", str(archive_zip)])
    capsys.readouterr()
    rc = main(["--data-dir", str(cfg.data_dir), "query", "--limit", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "showing 2 of 9" in out
    rc = main(["--data-dir", str(cfg.data_dir), "query", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "9 tweet(s)" in out
    assert "showing" not in out


def test_apply_requires_confirm(cfg, archive_zip, capsys):
    main(["--data-dir", str(cfg.data_dir), "import", str(archive_zip)])
    rc = main(["--data-dir", str(cfg.data_dir), "delete", "--apply"])
    assert rc == 2
    err = capsys.readouterr().err
    assert CONFIRM_PHRASE in err
