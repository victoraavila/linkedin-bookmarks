"""Import LinkedIn's official "Get a copy of your data" export.

The export's ``Saved Items.csv`` lists saved content as URLs plus save dates
only — no text, no author. It is still worth importing for two reasons the live
sync cannot cover:

* it gives the **save date**, which the live API does not expose;
* it proves a post was saved even if LinkedIn later stops serving it, so a
  deleted post is retained as a URL instead of vanishing.

This is the analogue of the X plugin's xarchive import: a second, independent
source that enriches the archive rather than replacing the live sync.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from . import db
from .importers import _iso, from_export_row


class ImportError_(RuntimeError):
    pass


_URL_HINTS = ("url", "link", "post")
_DATE_HINTS = ("date", "saved", "time")
_TYPE_HINTS = ("type", "kind", "category")


def _pick_column(fieldnames: list[str], hints: tuple[str, ...]) -> str | None:
    for name in fieldnames:
        lowered = (name or "").lower()
        if any(hint in lowered for hint in hints):
            return name
    return None


def _looks_like_url(value: str) -> bool:
    value = value or ""
    return value.startswith("http://") or value.startswith("https://")


def _detect_columns(fieldnames: list[str], rows: list[dict]) -> tuple[str, str | None, str | None]:
    url_col = _pick_column(fieldnames, _URL_HINTS)
    date_col = _pick_column(fieldnames, _DATE_HINTS)
    type_col = _pick_column(fieldnames, _TYPE_HINTS)

    sample = rows[:20]

    def column_looks_like_url(name: str | None) -> bool:
        if not name:
            return False
        values = [str(r.get(name) or "") for r in sample]
        return any(_looks_like_url(v) for v in values)

    # A header hint can be wrong (e.g. a "Date Posted" column matching on
    # "post"). Trust the hint only if its values actually look like URLs.
    if url_col is not None and not column_looks_like_url(url_col):
        url_col = None

    if url_col is None or date_col is None:
        for name in fieldnames:
            values = [str(r.get(name) or "") for r in sample]
            if url_col is None and any(_looks_like_url(v) for v in values):
                url_col = name
                continue
            if date_col is None and values and all(_parseable_date(v) for v in values if v):
                date_col = name

    if url_col is None:
        raise ImportError_(
            "could not find a URL column in the export. Expected a header such as "
            "'URL' — the file may not be LinkedIn's Saved Items export."
        )
    return url_col, date_col, type_col


def _parseable_date(value: str) -> bool:
    if not value:
        return False
    return bool(_iso(value)) and any(ch.isdigit() for ch in value)


def _load_rows(source: Path) -> tuple[list[str], list[dict]]:
    text = source.read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("items") or data.get("rows") or data.get("saved") or []
        if not isinstance(data, list):
            raise ImportError_("JSON export must be a list of objects or an object with an 'items' list")
        rows = [row for row in data if isinstance(row, dict)]
        fieldnames = sorted({key for row in rows for key in row})
        return fieldnames, rows

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = list(reader.fieldnames or [])
    rows = list(reader)
    return fieldnames, rows


def import_export(path: str | Path) -> dict:
    """Import a LinkedIn Saved Items export (CSV or JSON)."""
    source = Path(path).expanduser()
    if not source.exists():
        raise ImportError_(f"file not found: {source}")

    try:
        fieldnames, rows = _load_rows(source)
    except json.JSONDecodeError as exc:
        raise ImportError_(f"{source} is not valid JSON: {exc}") from exc

    if not fieldnames or not rows:
        raise ImportError_(f"{source} has no rows to import")

    url_col, date_col, type_col = _detect_columns(fieldnames, rows)
    observed = db.now_iso()

    conn = db.init()
    summary = {
        "file": str(source),
        "rows": len(rows),
        "url_column": url_col,
        "date_column": date_col,
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "errors": [],
        "earliest_saved": None,
        "latest_saved": None,
    }
    saved_dates: list[str] = []

    try:
        for index, row in enumerate(rows, start=2):  # header is line 1
            raw_url = str(row.get(url_col) or "").strip()
            if not raw_url:
                summary["skipped"] += 1
                continue
            saved_at = row.get(date_col) if date_col else None
            kind = row.get(type_col) if type_col else None
            try:
                rec = from_export_row(raw_url, saved_at=saved_at, kind=kind, observed_at=observed)
            except ValueError as exc:
                summary["errors"].append(f"line {index}: {exc}")
                continue

            if rec.get("saved_at"):
                saved_dates.append(rec["saved_at"])
            outcome = db.upsert_post(conn, rec)
            summary[outcome] += 1

        db.set_meta(conn, "last_export_import_at", db.now_iso())
        conn.commit()
    finally:
        conn.close()

    if saved_dates:
        summary["earliest_saved"] = min(saved_dates)
        summary["latest_saved"] = max(saved_dates)
    return summary
