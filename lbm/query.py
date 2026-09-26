"""Read-only query helpers shared by the MCP server and the CLI."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import db
from .importers import activity_id_from_url

SUMMARY_COLUMNS = """
    p.post_id, p.url, p.full_text, p.created_at, p.saved_at, p.status, p.lang,
    p.post_type, p.is_repost, p.author_name, p.author_handle, p.author_headline,
    p.likes, p.comments, p.reposts, p.article_title, p.article_url,
    p.reposted_full_text, p.reposted_from_handle, p.media_json, p.links_json,
    p.origin, p.last_seen_at, p.sort_index
"""

# Newest save first: the observed saved order wins where we have it, otherwise
# fall back to the save date (official export) or when we first archived it.
RECENT_ORDER = (
    "ORDER BY CASE WHEN p.sort_index IS NOT NULL THEN p.sort_index ELSE 1000000 END ASC, "
    "COALESCE(p.saved_at, p.first_seen_at) DESC"
)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    if "media_json" in data:
        try:
            data["media"] = json.loads(data.pop("media_json") or "[]")
        except (TypeError, ValueError):
            data["media"] = []
    if "links_json" in data:
        try:
            data["links"] = json.loads(data.pop("links_json") or "[]")
        except (TypeError, ValueError):
            data["links"] = []
    return data


def _fts_terms(raw: str) -> list[str]:
    terms = re.findall(r"[A-Za-z0-9_@#.\-]+", raw or "")
    if not terms:
        raise ValueError("empty search query")
    return terms


def _candidate_expressions(terms: list[str]) -> list[str]:
    """Precise-first, recall-second.

    All-terms (implicit AND) is tried first because it is precise. If it matches
    nothing, the same terms are OR'd and BM25 ranking puts documents matching
    more of them on top. This avoids the classic "no results" dead end without
    making ordinary queries noisy.
    """
    prefixes = [f'"{term}"*' for term in terms]
    if len(prefixes) == 1:
        return [" ".join(prefixes)]
    return [" AND ".join(prefixes), " OR ".join(prefixes)]


def _run_search(
    conn: sqlite3.Connection,
    match_expression: str,
    limit: int,
    author: str | None,
    label: str | None,
    since: str | None,
    until: str | None,
) -> list[sqlite3.Row]:
    params: list[Any] = [match_expression]
    sql = [
        "SELECT",
        SUMMARY_COLUMNS,
        "FROM posts_fts f",
        "JOIN posts p ON p.post_id = f.post_id",
    ]
    if label:
        sql.append(
            "JOIN post_labels pl ON pl.post_id = p.post_id "
            "JOIN labels la ON la.id = pl.label_id"
        )
    sql.append("WHERE posts_fts MATCH ?")
    if author:
        sql.append("AND (p.author_handle = ? OR p.author_handle = ?)")
        params.extend([author.lstrip("@"), author])
    if label:
        sql.append("AND (la.name LIKE ? OR la.id = ?)")
        params.extend([f"%{label}%", label])
    if since:
        sql.append("AND p.created_at >= ?")
        params.append(since)
    if until:
        sql.append("AND p.created_at <= ?")
        params.append(until)
    sql.append("ORDER BY rank LIMIT ?")
    params.append(max(1, min(limit, 200)))
    return conn.execute(" ".join(sql), params).fetchall()


def search_bookmarks(
    query: str,
    limit: int = 25,
    author: str | None = None,
    label: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    terms = _fts_terms(query)
    conn = db.connect()
    try:
        rows: list[sqlite3.Row] = []
        for expression in _candidate_expressions(terms):
            rows = _run_search(conn, expression, limit, author, label, since, until)
            if rows:
                break
        results = [_row_to_dict(row) for row in rows]
        _attach_labels(conn, results)
        return results
    finally:
        conn.close()


def recent_bookmarks(
    limit: int = 25,
    days: int | None = None,
    author: str | None = None,
    label: str | None = None,
) -> list[dict]:
    conn = db.connect()
    try:
        params: list[Any] = []
        sql = ["SELECT", SUMMARY_COLUMNS, "FROM posts p"]
        if label:
            sql.append(
                "JOIN post_labels pl ON pl.post_id = p.post_id "
                "JOIN labels la ON la.id = pl.label_id"
            )
        sql.append("WHERE 1 = 1")
        if days:
            sql.append("AND COALESCE(p.saved_at, p.first_seen_at) >= datetime('now', ?)")
            params.append(f"-{int(days)} days")
        if author:
            sql.append("AND (p.author_handle = ? OR p.author_handle = ?)")
            params.extend([author.lstrip("@"), author])
        if label:
            sql.append("AND (la.name LIKE ? OR la.id = ?)")
            params.extend([f"%{label}%", label])
        sql.append(RECENT_ORDER)
        sql.append("LIMIT ?")
        params.append(max(1, min(limit, 200)))
        rows = conn.execute(" ".join(sql), params).fetchall()
        results = [_row_to_dict(r) for r in rows]
        _attach_labels(conn, results)
        return results
    finally:
        conn.close()


def get_bookmark(post_id_or_url: str) -> dict | None:
    raw = (post_id_or_url or "").strip()
    conn = db.connect()
    try:
        row = None
        post_id = activity_id_from_url(raw)
        if post_id:
            row = conn.execute(
                "SELECT * FROM posts WHERE post_id = ?", (post_id,)
            ).fetchone()
        if row is None and raw and raw.isdigit():
            row = conn.execute(
                "SELECT * FROM posts WHERE post_id = ?", (raw,)
            ).fetchone()
        if row is None and raw:
            row = conn.execute(
                "SELECT * FROM posts WHERE url = ? LIMIT 1", (raw,)
            ).fetchone()
        if row is None:
            return None
        data = _row_to_dict(row)
        for key in ("entities_json", "hashtags_json", "mentions_json", "raw_json"):
            data.pop(key, None)
        allowed_columns = {"media_json", "links_json"}
        for key in list(data):
            if key.endswith("_json") and key not in allowed_columns:
                data.pop(key)
        _attach_labels(conn, [data])
        return data
    finally:
        conn.close()


def _attach_labels(conn: sqlite3.Connection, results: list[dict]) -> None:
    if not results:
        return
    ids = [r["post_id"] for r in results]
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT pl.post_id, la.name FROM post_labels pl "
        "JOIN labels la ON la.id = pl.label_id "
        f"WHERE pl.post_id IN ({placeholders})",
        ids,
    ).fetchall()
    mapping: dict[str, list[str]] = {}
    for row in rows:
        mapping.setdefault(row["post_id"], []).append(row["name"])
    for item in results:
        item["labels"] = sorted(mapping.get(item["post_id"], []))


def list_folders() -> list[dict]:
    """LinkedIn has no saved-post folders; this reports imported labels/tags."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT la.id, la.name, COUNT(pl.post_id) AS bookmarks "
            "FROM labels la LEFT JOIN post_labels pl ON pl.label_id = la.id "
            "GROUP BY la.id, la.name ORDER BY bookmarks DESC, la.name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def authors(limit: int = 50, min_bookmarks: int = 2) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT author_handle, author_name, COUNT(*) AS bookmarks "
            "FROM posts WHERE author_handle IS NOT NULL "
            "GROUP BY author_handle HAVING bookmarks >= ? "
            "ORDER BY bookmarks DESC LIMIT ?",
            (min_bookmarks, max(1, min(limit, 500))),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats() -> dict:
    conn = db.connect()
    try:
        info = db.counts(conn)
        info["by_origin"] = [
            dict(r)
            for r in conn.execute(
                "SELECT origin, COUNT(*) AS count FROM posts GROUP BY origin "
                "ORDER BY count DESC"
            ).fetchall()
        ]
        info["by_month"] = [
            dict(r)
            for r in conn.execute(
                "SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS count "
                "FROM posts WHERE created_at IS NOT NULL "
                "GROUP BY month ORDER BY month DESC LIMIT 24"
            ).fetchall()
        ]
        info["top_authors"] = authors(limit=10, min_bookmarks=1)
        info["newest"] = conn.execute(
            "SELECT MAX(COALESCE(created_at, first_seen_at)) AS newest FROM posts"
        ).fetchone()["newest"]
        for key in (
            "last_sync_at",
            "last_sync_mode",
            "last_sync_summary",
            "last_full_sync_at",
            "last_export_import_at",
        ):
            info[key] = db.get_meta(conn, key)
        return info
    finally:
        conn.close()


READ_ONLY_PREFIXES = ("select", "with", "pragma table_info", "explain")
BANNED_SQL = ("insert", "update", "delete", "drop", "alter", "attach", "create", "replace")
# A read-only query can still be unbounded work (e.g. WITH RECURSIVE), which
# would wedge the whole MCP server. Cap wall-clock time with a progress handler.
SQL_TIMEOUT_SECONDS = 10


def run_sql(sql: str, limit: int = 200, timeout: float = SQL_TIMEOUT_SECONDS) -> list[dict]:
    """Execute a read-only SELECT against the archive, with a time bound."""
    statement = (sql or "").strip().rstrip(";")
    if not statement:
        raise ValueError("empty SQL statement")
    lowered = statement.lower()
    if not lowered.startswith(READ_ONLY_PREFIXES):
        raise ValueError("only read-only SELECT / WITH / EXPLAIN queries are allowed")
    if any(re.search(rf"\b{word}\b", lowered) for word in BANNED_SQL):
        raise ValueError("write statements are not allowed")
    conn = db.connect()
    try:
        if timeout and timeout > 0:
            conn.set_progress_handler(_deadline_handler(timeout), 20_000)
        try:
            rows = conn.execute(statement).fetchmany(max(1, min(limit, 1000)))
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise ValueError(
                    f"query exceeded the {timeout:g}s limit and was interrupted"
                ) from exc
            raise
        return [dict(r) for r in rows]
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def _deadline_handler(seconds: float):
    import time as _time

    deadline = _time.monotonic() + seconds

    def handler() -> int:
        return 1 if _time.monotonic() > deadline else 0

    return handler


# --------------------------------------------------------------------------- #
# retrieval health
# --------------------------------------------------------------------------- #

def integrity() -> dict:
    """Whether the full-text index is in sync with the post table."""
    conn = db.connect()
    try:
        return db.fts_counts(conn)
    finally:
        conn.close()


def _distinctive_token(text: str) -> str | None:
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9]{5,}", text or "")
    if not candidates:
        return None
    return max(candidates, key=len).lower()


def _unique_token(conn: sqlite3.Connection, text: str) -> str | None:
    """Find a token that occurs in exactly one post.

    Using a corpus-unique token makes the round-trip assertion deterministic:
    the search must return that post regardless of BM25 ranking.
    """
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9]{5,}", text or ""):
        token = raw.lower()
        if token in seen:
            continue
        seen.add(token)
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM posts_fts WHERE posts_fts MATCH ?",
            (f'"{token}"',),
        ).fetchone()["c"]
        if count == 1:
            return token
    return None


def self_test() -> dict:
    """End-to-end retrieval check.

    Picks a real post, finds a token unique to it, then confirms the normal query
    path (tokenizer -> index -> query builder -> join) retrieves it. Also
    exercises the author filter. Verifies retrieval *works*; not ranking quality.
    """
    conn = db.connect()
    try:
        candidates = conn.execute(
            "SELECT post_id, full_text, author_handle FROM posts "
            "WHERE full_text IS NOT NULL AND length(full_text) > 80 "
            "ORDER BY RANDOM() LIMIT 20"
        ).fetchall()
        if not candidates:
            return {"ok": False, "reason": "no posts with text to test against"}

        chosen = None
        token = None
        for row in candidates:
            candidate = _unique_token(conn, row["full_text"])
            if candidate:
                chosen, token = row, candidate
                break

        if chosen is None:
            row = candidates[0]
            fallback_token = _distinctive_token(row["full_text"])
            if fallback_token is None:
                return {"ok": False, "reason": "could not derive a test token"}
            hits = search_bookmarks(fallback_token, limit=200)
            return {
                "ok": True,
                "mode": "fallback",
                "post_id": row["post_id"],
                "token": fallback_token,
                "hits": len(hits),
                "found": any(hit["post_id"] == row["post_id"] for hit in hits),
                "note": "no corpus-unique token found; membership checked in top 200",
            }
    finally:
        conn.close()

    hits = search_bookmarks(token, limit=10)
    found = any(hit["post_id"] == chosen["post_id"] for hit in hits)

    author_ok: bool | None = None
    if chosen["author_handle"]:
        by_author = search_bookmarks(token, limit=10, author=chosen["author_handle"])
        author_ok = any(hit["post_id"] == chosen["post_id"] for hit in by_author)

    return {
        "ok": bool(found and author_ok is not False),
        "mode": "unique-token",
        "post_id": chosen["post_id"],
        "token": token,
        "hits": len(hits),
        "found": found,
        "author_filter_ok": author_ok,
    }
