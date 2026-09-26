from lbm import db


def _rec(post_id, **kwargs):
    base = {
        "post_id": post_id,
        "url": f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id}/",
        "urn": f"urn:li:activity:{post_id}",
        "origin": "voyager",
    }
    base.update(kwargs)
    return base


def test_upsert_inserts_then_merges(archive):
    conn = db.init()
    assert db.upsert_post(conn, _rec("1", full_text="hello", likes=1)) == "added"
    # A later observation with no text must not erase the stored text.
    assert db.upsert_post(conn, _rec("1", likes=5)) == "updated"
    row = conn.execute("SELECT * FROM posts WHERE post_id = '1'").fetchone()
    assert row["full_text"] == "hello"
    assert row["likes"] == 5
    conn.close()


def test_unchanged_when_nothing_new(archive):
    conn = db.init()
    db.upsert_post(conn, _rec("2", full_text="stable"))
    assert db.upsert_post(conn, _rec("2", full_text="stable")) == "unchanged"
    conn.close()


def test_reobservation_with_only_last_seen_change_is_not_an_update(archive):
    conn = db.init()
    db.upsert_post(conn, _rec("20", full_text="stable", last_seen_at="2026-01-01T00:00:00+00:00"))
    # A later observation advances last_seen_at but changes no content. That must
    # report "unchanged", not "updated", or every known post looks updated.
    outcome = db.upsert_post(
        conn, _rec("20", full_text="stable", last_seen_at="2026-06-01T00:00:00+00:00")
    )
    assert outcome == "unchanged"
    row = conn.execute("SELECT last_seen_at FROM posts WHERE post_id = '20'").fetchone()
    assert row["last_seen_at"].startswith("2026-06-01")
    conn.close()


def test_mark_content_fetched_is_idempotent(archive):
    conn = db.init()
    db.upsert_post(conn, _rec("21"))
    db.mark_content_fetched(conn, "21")
    first = conn.execute(
        "SELECT content_fetched_at FROM posts WHERE post_id = '21'"
    ).fetchone()["content_fetched_at"]
    assert first
    db.mark_content_fetched(conn, "21")
    second = conn.execute(
        "SELECT content_fetched_at FROM posts WHERE post_id = '21'"
    ).fetchone()["content_fetched_at"]
    assert second == first
    conn.close()


def test_tombstone_keeps_content(archive):
    conn = db.init()
    db.upsert_post(conn, _rec("3", full_text="saved body", likes=9))
    db.upsert_post(conn, {
        "post_id": "3",
        "status": "unavailable",
        "unavailable_reason": "post deleted",
        "origin": "voyager",
    })
    row = conn.execute("SELECT * FROM posts WHERE post_id = '3'").fetchone()
    assert row["status"] == "unavailable"
    assert row["full_text"] == "saved body"
    assert row["likes"] == 9
    conn.close()


def test_origin_is_combined_across_sources(archive):
    conn = db.init()
    db.upsert_post(conn, _rec("4", origin="voyager"))
    db.upsert_post(conn, {
        "post_id": "4",
        "origin": "linkedin_export",
        "saved_at": "2026-01-01T00:00:00+00:00",
    })
    row = conn.execute("SELECT origin, saved_at FROM posts WHERE post_id = '4'").fetchone()
    assert row["origin"] == "linkedin_export+voyager"
    assert row["saved_at"].startswith("2026-01-01")
    conn.close()


def test_fts_stays_in_sync_and_reindex(archive):
    conn = db.init()
    for i in range(5):
        db.upsert_post(conn, _rec(str(i), full_text=f"durable objects {i}"))
    conn.commit()
    counts = db.fts_counts(conn)
    assert counts["in_sync"] is True
    assert counts["indexed"] == 5

    conn.execute("DELETE FROM posts_fts")  # simulate drift
    assert db.fts_counts(conn)["in_sync"] is False
    assert db.reindex(conn) == 5
    assert db.fts_counts(conn)["in_sync"] is True
    conn.close()


def test_refresh_save_order_only_touches_changed_rows(archive):
    conn = db.init()
    for pid in ("10", "11", "12"):
        db.upsert_post(conn, _rec(pid, full_text="x"))
    conn.commit()
    changed = db.refresh_save_order(conn, ["12", "11", "10"])
    assert changed == 3
    assert db.refresh_save_order(conn, ["12", "11", "10"]) == 0
    rows = dict(conn.execute("SELECT post_id, sort_index FROM posts").fetchall())
    assert rows["12"] == 0 and rows["11"] == 1 and rows["10"] == 2
    conn.close()
