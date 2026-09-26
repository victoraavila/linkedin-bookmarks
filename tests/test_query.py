import pytest

from lbm import db, query as q


def _seed(conn):
    rows = [
        ("100", "Durable Objects and edge state", "jane-doe", "Jane Doe", 0),
        ("101", "Vector databases compared", "sam-smith", "Sam Smith", 1),
        ("102", "More on durable execution", "jane-doe", "Jane Doe", 2),
    ]
    for pid, text, handle, name, sort_index in rows:
        db.upsert_post(conn, {
            "post_id": pid,
            "url": f"https://www.linkedin.com/feed/update/urn:li:activity:{pid}/",
            "urn": f"urn:li:activity:{pid}",
            "full_text": text,
            "author_handle": handle,
            "author_name": name,
            "sort_index": sort_index,
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "origin": "voyager",
        })
    conn.commit()


def test_search_matches_and_filters(archive):
    conn = db.init()
    _seed(conn)
    conn.close()

    hits = q.search_bookmarks("durable")
    assert {h["post_id"] for h in hits} == {"100", "102"}

    by_author = q.search_bookmarks("durable", author="@jane-doe")
    assert {h["post_id"] for h in by_author} == {"100", "102"}

    none = q.search_bookmarks("durable", author="sam-smith")
    assert none == []


def test_recent_follows_saved_order(archive):
    conn = db.init()
    _seed(conn)
    conn.close()
    results = q.recent_bookmarks(limit=10)
    assert [r["post_id"] for r in results] == ["100", "101", "102"]


def test_get_bookmark_by_url_and_id(archive):
    conn = db.init()
    _seed(conn)
    conn.close()
    assert q.get_bookmark("102")["full_text"].startswith("More on durable")
    by_url = q.get_bookmark("https://www.linkedin.com/feed/update/urn:li:activity:101/")
    assert by_url["post_id"] == "101"
    assert q.get_bookmark("does-not-exist") is None


def test_sql_is_read_only(archive):
    conn = db.init()
    _seed(conn)
    conn.close()
    assert q.run_sql("SELECT COUNT(*) AS n FROM posts")[0]["n"] == 3
    with pytest.raises(ValueError):
        q.run_sql("DELETE FROM posts")
    with pytest.raises(ValueError):
        q.run_sql("UPDATE posts SET full_text = 'x'")


def test_sql_is_time_bounded(archive):
    conn = db.init()
    _seed(conn)
    conn.close()
    # A read-only but unbounded recursive query must be interrupted, not hang.
    with pytest.raises(ValueError):
        q.run_sql(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT count(*) FROM c",
            timeout=0.05,
        )


def test_self_test_round_trip(archive):
    conn = db.init()
    db.upsert_post(conn, {
        "post_id": "200",
        "url": "https://www.linkedin.com/feed/update/urn:li:activity:200/",
        "full_text": (
            "An extended note about zephyrian orchestration patterns and how "
            "they interact with durable queues in production systems."
        ),
        "author_handle": "jane-doe",
        "origin": "voyager",
    })
    conn.commit()
    conn.close()
    result = q.self_test()
    assert result["ok"] is True
