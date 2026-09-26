import pytest

from lbm import ingest


def test_import_csv_with_named_columns(archive, tmp_path):
    csv_path = tmp_path / "Saved Items.csv"
    csv_path.write_text(
        "Saved Date,URL\n"
        "2026-02-01 09:30:00,https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/\n"
        "2026-01-15 10:00:00,https://example.com/article\n",
        encoding="utf-8",
    )
    summary = ingest.import_export(csv_path)
    assert summary["added"] == 2
    assert summary["date_column"] == "Saved Date"
    assert summary["earliest_saved"].startswith("2026-01-15")
    assert summary["latest_saved"].startswith("2026-02-01")


def test_import_csv_sniffs_unnamed_columns(archive, tmp_path):
    csv_path = tmp_path / "export.csv"
    csv_path.write_text(
        "first,second\n"
        "https://www.linkedin.com/posts/jane-doe_pricing-activity-7123456789012345678-abc,2026-03-03\n",
        encoding="utf-8",
    )
    summary = ingest.import_export(csv_path)
    assert summary["added"] == 1
    assert summary["url_column"] == "first"
    assert summary["date_column"] == "second"


def test_import_json_export(archive, tmp_path):
    path = tmp_path / "saved.json"
    path.write_text(
        '[{"url": "https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/",'
        ' "saved_at": "2026-05-05"}]',
        encoding="utf-8",
    )
    summary = ingest.import_export(path)
    assert summary["added"] == 1


def test_import_missing_file_raises(archive, tmp_path):
    with pytest.raises(ingest.ImportError_):
        ingest.import_export(tmp_path / "nope.csv")


def test_import_without_url_column_raises(archive, tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("name,notes\nalice,hi\n", encoding="utf-8")
    with pytest.raises(ingest.ImportError_):
        ingest.import_export(csv_path)


def test_url_column_hint_is_validated_against_values(archive, tmp_path):
    # "Date Posted" matches the "post" hint but is a date; the real URL column
    # must still win.
    csv_path = tmp_path / "wrong.csv"
    csv_path.write_text(
        "Date Posted,Actual Link\n"
        "2026-01-01,https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/\n",
        encoding="utf-8",
    )
    summary = ingest.import_export(csv_path)
    assert summary["url_column"] == "Actual Link"
    assert summary["date_column"] == "Date Posted"
    assert summary["added"] == 1
