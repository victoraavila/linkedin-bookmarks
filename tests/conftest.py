import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    """An isolated archive + account store per test."""
    monkeypatch.setenv("LB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LB_DB", str(tmp_path / "bookmarks.db"))
    monkeypatch.setenv("LB_ACCOUNTS_DB", str(tmp_path / "accounts.db"))
    from lbm import db

    db.init().close()
    return tmp_path
