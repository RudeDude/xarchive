import zipfile
from pathlib import Path

import pytest

from xarchive.archive import ZipSlipError, import_archive
from tests.conftest import write_archive_zip


def test_zip_slip_rejected(cfg, db, tmp_path):
    z = tmp_path / "slip.zip"
    write_archive_zip(
        z,
        extra_members={
            "twitter-2026-01-01-deadbeef/data/tweets_media/../../../tmp/evil.jpg": b"evil",
        },
    )
    with pytest.raises(ZipSlipError):
        import_archive(z, db, cfg.media_dir)
    # nothing written outside media dir
    evil = tmp_path / "tmp" / "evil.jpg"
    assert not evil.exists()
    # also not next to the zip
    assert not (tmp_path / "evil.jpg").exists()
