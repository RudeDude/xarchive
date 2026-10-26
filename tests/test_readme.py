from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_covers_workflow():
    text = README.lower()
    assert "settings" in text and "archive" in text
    assert "xarchive import" in text
    assert "xarchive serve" in text
    assert "127.0.0.1" in README
    assert "xarchive mark" in text
    assert "xarchive delete" in text
    assert "--apply" in text
    assert "permanently-delete" in text
    assert "graphql" in text
    assert "cookie" in text
    assert "unsupported" in text
    assert "re-run" in text or "every few weeks" in text
    assert "0.0.0.0" in README
    assert "note-tweet" in text or "note tweet" in text
