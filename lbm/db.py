"""SQLite storage for the LinkedIn saved-posts archive.

The archive is cumulative: a newer observation may mark a post unavailable or
stop returning it entirely, but it must never destroy content we already
captured. ``upsert_post`` therefore fills missing fields from the incoming
record and leaves existing content alone when the incoming value is empty.

This mirrors the design of the sibling ``x-bookmarks`` archive on purpose, so
the two behave identically for an agent that knows one of them.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Fields that carry post content. Empty incoming values never clobber them.
CONTENT_FIELDS = (
    "url",
    "urn",
    "full_text",
    "lang",
    "created_at",
    "post_type",
    "is_repost",
    "reposted_from_name",
    "reposted_from_handle",
    "reposted_full_text",
    "author_id",
    "author_name",
    "author_handle",
    "author_headline",
    "author_followers",
    "author_profile_image",
    "likes",
    "comments",
    "reposts",
    "article_title",
    "article_subtitle",
    "article_url",
    "media_json",
    "entities_json",
    "hashtags_json",
    "mentions_json",
    "links_json",
    "raw_json",
    "saved_at",
    "sort_index",
)

ALL_FIELDS = (
    "post_id",
    *CONTENT_FIELDS,
    "status",
    "unavailable_reason",
    "origin",
    "first_seen_at",
    "last_seen_at",
    "content_updated_at",
    # Bookkeeping: set once a content fetch has been attempted and parsed, so a
    # genuinely textless post (image-only) is not re-fetched on every sync.
    "content_fetched_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    post_id              TEXT PRIMARY KEY,
    url                  TEXT,
    urn                  TEXT,
    full_text            TEXT,
    lang                 TEXT,
    created_at           TEXT,
    status               TEXT DEFAULT 'available',
    unavailable_reason   TEXT,
    post_type            TEXT,
    is_repost            INTEGER,
    reposted_from_name   TEXT,
    reposted_from_handle TEXT,
    reposted_full_text   TEXT,
    author_id            TEXT,
    author_name          TEXT,
    author_handle        TEXT,
    author_headline      TEXT,
    author_followers     INTEGER,
    author_profile_image TEXT,
    likes                INTEGER,
    comments             INTEGER,
    reposts              INTEGER,
    article_title        TEXT,
    article_subtitle     TEXT,
    article_url          TEXT,
    media_json           TEXT,
    entities_json        TEXT,
    hashtags_json        TEXT,
    mentions_json        TEXT,
    links_json           TEXT,
    raw_json             TEXT,
    saved_at             TEXT,
    origin               TEXT,
    first_seen_at        TEXT,
    last_seen_at         TEXT,
    content_updated_at   TEXT,
    content_fetched_at   TEXT,
    sort_index           INTEGER
);

CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_saved   ON posts(saved_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_author  ON posts(author_handle);
CREATE INDEX IF NOT EXISTS idx_posts_status  ON posts(status);

CREATE TABLE IF NOT EXISTS labels (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post_labels (
    post_id  TEXT NOT NULL,
    label_id TEXT NOT NULL,
    PRIMARY KEY (post_id, label_id)
);

CREATE INDEX IF NOT EXISTS idx_pl_label ON post_labels(label_id);

CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    post_id UNINDEXED,
    full_text,
    author_name,
    author_handle,
    author_headline,
    links_text,
    reposted_full_text,
    article_title,
    article_subtitle,
    tokenize = 'porter unicode61'
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def data_dir() -> Path:
    raw = os.environ.get("LB_DATA_DIR")
    path = Path(raw).expanduser() if raw else PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    raw = os.environ.get("LB_DB")
    return Path(raw).expanduser() if raw else data_dir() / "bookmarks.db"


def accounts_db_path() -> Path:
    raw = os.environ.get("LB_ACCOUNTS_DB")
    return Path(raw).expanduser() if raw else data_dir() / "accounts.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(posts)")}
    for column, decl in (("content_fetched_at", "TEXT"),):
        if column not in existing:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {column} {decl}")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _links_text(links_json: str | None) -> str:
    if not links_json:
        return ""
    try:
        links = json.loads(links_json)
    except (TypeError, ValueError):
        return ""
    if isinstance(links, list):
        return " ".join(str(x) for x in links if x)
    return str(links)


def _reindex_fts(conn: sqlite3.Connection, post_id: str) -> None:
    conn.execute("DELETE FROM posts_fts WHERE post_id = ?", (post_id,))
    row = conn.execute(
        "SELECT post_id, full_text, author_name, author_handle, author_headline, "
        "links_json, reposted_full_text, article_title, article_subtitle "
        "FROM posts WHERE post_id = ?",
        (post_id,),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO posts_fts"
        "(post_id, full_text, author_name, author_handle, author_headline, "
        " links_text, reposted_full_text, article_title, article_subtitle) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["post_id"],
            row["full_text"],
            row["author_name"],
            row["author_handle"],
            row["author_headline"],
            _links_text(row["links_json"]),
            row["reposted_full_text"],
            row["article_title"],
            row["article_subtitle"],
        ),
    )


def _earliest(*values: str | None) -> str | None:
    present = [v for v in values if v]
    return min(present) if present else None


def _latest(*values: str | None) -> str | None:
    present = [v for v in values if v]
    return max(present) if present else None


def upsert_post(conn: sqlite3.Connection, rec: dict) -> str:
    """Insert or merge a saved post. Returns 'added', 'updated' or 'unchanged'."""
    post_id = rec.get("post_id")
    if not post_id:
        raise ValueError("post record is missing post_id")

    observed = rec.get("last_seen_at") or now_iso()
    existing = conn.execute(
        "SELECT * FROM posts WHERE post_id = ?", (post_id,)
    ).fetchone()

    if existing is None:
        row = {field: rec.get(field) for field in ALL_FIELDS}
        row["post_id"] = post_id
        row["status"] = rec.get("status") or "available"
        row["origin"] = rec.get("origin") or "unknown"
        row["first_seen_at"] = rec.get("first_seen_at") or observed
        row["last_seen_at"] = observed
        row["content_updated_at"] = rec.get("content_updated_at") or (
            observed if (row.get("full_text") or row.get("media_json")) else None
        )
        columns = ", ".join(ALL_FIELDS)
        placeholders = ", ".join("?" for _ in ALL_FIELDS)
        conn.execute(
            f"INSERT INTO posts ({columns}) VALUES ({placeholders})",
            [row[field] for field in ALL_FIELDS],
        )
        _reindex_fts(conn, post_id)
        return "added"

    merged = dict(existing)
    changed = False  # anything differs and needs a write
    content_changed = False  # a *meaningful* change (not just last_seen_at)

    # Content: fill in what we have; never let an empty value erase saved text.
    for field in CONTENT_FIELDS:
        incoming = rec.get(field)
        if incoming is None or incoming == "" or incoming == []:
            continue
        if merged.get(field) != incoming:
            merged[field] = incoming
            changed = True
            content_changed = True

    incoming_status = rec.get("status")
    if incoming_status and merged.get("status") != incoming_status:
        merged["status"] = incoming_status
        changed = True
        content_changed = True
    if incoming_status == "unavailable":
        # A tombstone may carry a reason; it must not drop the saved text.
        reason = rec.get("unavailable_reason")
        if reason and merged.get("unavailable_reason") != reason:
            merged["unavailable_reason"] = reason
            changed = True
            content_changed = True
    elif incoming_status == "available" and merged.get("unavailable_reason"):
        merged["unavailable_reason"] = None
        changed = True
        content_changed = True

    origins = {merged.get("origin"), rec.get("origin")}
    combined_origin = "+".join(sorted(o for o in origins if o))
    if merged.get("origin") != combined_origin:
        merged["origin"] = combined_origin
        changed = True
        content_changed = True

    first_seen = _earliest(merged.get("first_seen_at"), rec.get("first_seen_at")) or observed
    last_seen = _latest(merged.get("last_seen_at"), observed) or observed
    content_updated = _latest(
        merged.get("content_updated_at"),
        rec.get("content_updated_at"),
    )
    if content_updated is None and (merged.get("full_text") or merged.get("media_json")):
        content_updated = observed

    for key, value in (
        ("first_seen_at", first_seen),
        ("last_seen_at", last_seen),
        ("content_updated_at", content_updated),
    ):
        if merged.get(key) != value:
            merged[key] = value
            changed = True

    if not changed:
        return "unchanged"

    assignments = ", ".join(f"{field} = ?" for field in ALL_FIELDS if field != "post_id")
    conn.execute(
        f"UPDATE posts SET {assignments} WHERE post_id = ?",
        [merged[field] for field in ALL_FIELDS if field != "post_id"] + [post_id],
    )
    _reindex_fts(conn, post_id)
    # A re-observation that only advanced last_seen_at is not an "update"; saying
    # so keeps the sync report honest instead of flagging every known post.
    return "updated" if content_changed else "unchanged"


def mark_content_fetched(conn: sqlite3.Connection, post_id: str) -> None:
    """Record that content was fetched and parsed for this post.

    Set even when the parse yielded no text (an image-only post), so the sync
    does not re-fetch it on every run. It is deliberately *not* set when a fetch
    fails, so transient failures are retried.
    """
    conn.execute(
        "UPDATE posts SET content_fetched_at = ? WHERE post_id = ? AND "
        "(content_fetched_at IS NULL OR content_fetched_at = '')",
        (now_iso(), post_id),
    )


def refresh_save_order(conn: sqlite3.Connection, ordered_ids: list[str]) -> int:
    """Record the newest-first saved order as ``sort_index`` (0 = most recent).

    Only positions actually observed are written; posts that dropped out of the
    listing keep their previous index rather than being blanked.
    """
    updated = 0
    for index, post_id in enumerate(ordered_ids):
        cur = conn.execute(
            "UPDATE posts SET sort_index = ? WHERE post_id = ? AND "
            "(sort_index IS NULL OR sort_index != ?)",
            (index, post_id, index),
        )
        updated += cur.rowcount
    return updated


def upsert_label(conn: sqlite3.Connection, label_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO labels(id, name) VALUES(?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        (label_id, name),
    )


def set_post_labels(
    conn: sqlite3.Connection, post_id: str, label_ids: list[str]
) -> None:
    """Replace the membership set. Use only for a *complete* label observation."""
    conn.execute("DELETE FROM post_labels WHERE post_id = ?", (post_id,))
    add_post_labels(conn, post_id, label_ids)


def add_post_labels(
    conn: sqlite3.Connection, post_id: str, label_ids: list[str]
) -> None:
    """Add memberships without removing existing ones (partial observations)."""
    for label_id in sorted(set(label_ids)):
        conn.execute(
            "INSERT OR IGNORE INTO post_labels(post_id, label_id) VALUES(?, ?)",
            (post_id, label_id),
        )


def counts(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()["c"]
    available = conn.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE status = 'available'"
    ).fetchone()["c"]
    with_text = conn.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE "
        "(full_text IS NOT NULL AND full_text != '') "
        "OR (article_title IS NOT NULL AND article_title != '')"
    ).fetchone()["c"]
    labels = conn.execute("SELECT COUNT(*) AS c FROM labels").fetchone()["c"]
    return {
        "total": total,
        "available": available,
        "unavailable": total - available,
        "with_content": with_text,
        "labels": labels,
    }


def total_posts() -> int:
    """Count posts without leaking a connection."""
    conn = init()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()["c"]
    finally:
        conn.close()


def fts_counts(conn: sqlite3.Connection) -> dict:
    """Compare the post table with its full-text index.

    A drift here is the one failure mode that silently degrades search, so it is
    checked explicitly rather than assumed.
    """
    posts = conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()["c"]
    indexed = conn.execute("SELECT COUNT(*) AS c FROM posts_fts").fetchone()["c"]
    orphaned = conn.execute(
        "SELECT COUNT(*) AS c FROM posts_fts "
        "WHERE post_id NOT IN (SELECT post_id FROM posts)"
    ).fetchone()["c"]
    missing = conn.execute(
        "SELECT COUNT(*) AS c FROM posts "
        "WHERE post_id NOT IN (SELECT post_id FROM posts_fts)"
    ).fetchone()["c"]
    return {
        "posts": posts,
        "indexed": indexed,
        "orphaned": orphaned,
        "missing": missing,
        "in_sync": posts == indexed and orphaned == 0 and missing == 0,
    }


def reindex(conn: sqlite3.Connection) -> int:
    """Rebuild the search index from scratch. Returns the row count indexed."""
    conn.execute("DELETE FROM posts_fts")
    rows = conn.execute("SELECT post_id FROM posts").fetchall()
    for row in rows:
        _reindex_fts(conn, row["post_id"])
    conn.commit()
    return len(rows)
