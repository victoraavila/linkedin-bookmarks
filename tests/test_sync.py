import pytest

from lbm import db, sync
from lbm import voyager
from tests import fixtures


class FakeClient:
    def __init__(self, pages, posts=None):
        self.pages = pages
        self.posts = posts or {}
        self.fetch_calls = []

    def iter_saved_pages(self, page_size=50, page_delay=2.0, max_pages=None):
        for page in self.pages:
            yield page

    def fetch_post(self, post_id):
        self.fetch_calls.append(post_id)
        if post_id in self.posts:
            return self.posts[post_id]
        raise voyager.VoyagerError(f"no content for {post_id}")


def _page(ids, records=None, number=1, tokens=None):
    return voyager.SavedPage(
        number=number,
        activity_ids=list(ids),
        records=list(records or []),
        tokens=list(tokens or []),
    )


def _install(monkeypatch, client):
    monkeypatch.setattr(sync, "VoyagerClient", lambda cookies: client)
    sync.add_cookie("main", "li_at=abc; JSESSIONID=\"" + "tok" + "\"")


def test_full_sync_adds_ids_even_without_inline_content(archive, monkeypatch):
    client = FakeClient(
        pages=[_page([fixtures.POST_ID, fixtures.OTHER_POST_ID])],
        posts={
            fixtures.POST_ID: {
                "urn": f"urn:li:activity:{fixtures.POST_ID}",
                "full_text": "pricing thoughts",
                "author_name": "Jane Doe",
            },
            # Fetchable, but genuinely has no text: the post must still be kept.
            fixtures.OTHER_POST_ID: {
                "urn": f"urn:li:activity:{fixtures.OTHER_POST_ID}",
                "author_name": "Sam Smith",
            },
        },
    )
    _install(monkeypatch, client)

    result = sync.sync(mode="full", fetch_content=True)
    assert result.added == 2
    assert result.seen == 2
    assert result.enriched == 2
    assert result.content_errors == 0
    totals = db.counts(db.init())
    assert totals["total"] == 2
    assert totals["with_content"] == 1  # second post had no text to store

    row = db.connect().execute(
        "SELECT full_text, sort_index FROM posts WHERE post_id = ?",
        (fixtures.POST_ID,),
    ).fetchone()
    assert row["full_text"] == "pricing thoughts"
    assert row["sort_index"] == 0


def test_inline_records_avoid_a_content_fetch(archive, monkeypatch):
    inline = voyager.parse_update(
        fixtures.FEED_UPDATE["included"][0], urn=f"urn:li:activity:{fixtures.POST_ID}"
    )
    client = FakeClient(pages=[_page([fixtures.POST_ID], records=[inline])])
    _install(monkeypatch, client)

    result = sync.sync(mode="full", fetch_content=True)
    assert result.added == 1
    assert result.enriched == 0
    assert client.fetch_calls == []
    row = db.connect().execute(
        "SELECT full_text FROM posts WHERE post_id = ?", (fixtures.POST_ID,)
    ).fetchone()
    assert row["full_text"].startswith("Pricing teardown")


def test_quick_mode_stops_at_known_boundary(archive, monkeypatch):
    conn = db.init()
    for pid in ("9001", "9002"):
        db.upsert_post(conn, {"post_id": pid, "full_text": "already here", "origin": "voyager"})
    conn.commit()
    conn.close()

    client = FakeClient(pages=[_page(["9003", "9002", "9001"])])
    _install(monkeypatch, client)

    result = sync.sync(mode="quick", boundary=2, fetch_content=False)
    assert result.stopped_reason == "reached known boundary"
    assert result.added == 1  # only 9003, both knowns consumed the boundary
    assert result.seen == 3


def test_second_sync_is_idempotent(archive, monkeypatch):
    inline = voyager.parse_update(
        fixtures.FEED_UPDATE["included"][0], urn=f"urn:li:activity:{fixtures.POST_ID}"
    )
    client = FakeClient(
        pages=[_page([fixtures.POST_ID], records=[inline])],
        posts={fixtures.POST_ID: {"urn": f"urn:li:activity:{fixtures.POST_ID}", "full_text": "x"}},
    )
    _install(monkeypatch, client)

    first = sync.sync(mode="full", fetch_content=True)
    second = sync.sync(mode="full", fetch_content=True)
    assert first.added == 1
    assert second.added == 0
    assert db.counts(db.init())["total"] == 1


def test_textless_post_is_not_refetched_every_sync(archive, monkeypatch):
    client = FakeClient(
        pages=[_page([fixtures.OTHER_POST_ID])],
        posts={
            fixtures.OTHER_POST_ID: {
                "urn": f"urn:li:activity:{fixtures.OTHER_POST_ID}"
            }
        },
    )
    _install(monkeypatch, client)

    first = sync.sync(mode="full", fetch_content=True)
    second = sync.sync(mode="full", fetch_content=True)
    assert first.enriched == 1
    # A post that parses with no text is not re-fetched on the next sync.
    assert second.enriched == 0
    assert client.fetch_calls == [fixtures.OTHER_POST_ID]


def test_auth_failure_during_enrichment_still_finalizes(archive, monkeypatch):
    post = {"urn": f"urn:li:activity:{fixtures.POST_ID}", "full_text": "text"}

    class AuthFailingClient(FakeClient):
        def fetch_post(self, post_id):
            raise voyager.AuthError("session expired")

    client = AuthFailingClient(pages=[_page([fixtures.POST_ID])], posts={fixtures.POST_ID: post})
    _install(monkeypatch, client)

    result = sync.sync(mode="full", fetch_content=True)
    assert result.added == 1  # the stub was still archived
    assert result.errors  # the session problem is reported
    assert result.stopped_reason == "error"
    # Finalization ran: the sync timestamp was recorded.
    conn = db.init()
    assert db.get_meta(conn, "last_sync_at")
    assert db.counts(conn)["total"] == 1
    conn.close()


def test_sync_requires_a_configured_account(archive, monkeypatch):
    with pytest.raises(sync.SyncError):
        sync.sync(mode="quick")


def test_browser_engine_uses_browser_transport(archive, monkeypatch):
    import lbm.browser as browser_mod

    state = {}

    class FakeTransport:
        def __init__(self, *, headless: bool = False):
            state["headless"] = headless

        def start(self):
            state["started"] = True
            return self

        def ensure_logged_in(self, **kwargs):
            return True

        def capture_saved_post_pages(self, **kwargs):
            return [fixtures.SAVED_PAGE_1]

        def get_json(self, url, params=None):
            return fixtures.SAVED_PAGE_1

        def close(self):
            state["closed"] = True

    monkeypatch.setattr(browser_mod, "BrowserTransport", FakeTransport)

    result = sync.sync(mode="full", engine="browser", fetch_content=False)
    assert result.added == 1
    assert result.account == "browser"
    assert state.get("started") and state.get("closed")


def test_add_cookie_rejects_incomplete_input(archive):
    with pytest.raises(sync.SyncError):
        sync.add_cookie("main", "li_at=abc")
    with pytest.raises(sync.SyncError):
        sync.add_cookie("main", "JSESSIONID=abc")
