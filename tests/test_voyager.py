import pytest

from lbm import voyager
from tests import fixtures


def test_parse_cookie_string_handles_header_and_two_lines():
    assert voyager.parse_cookie_string("li_at=abc; JSESSIONID=\"xyz\"") == {
        "li_at": "abc",
        "JSESSIONID": "xyz",
    }
    assert voyager.parse_cookie_string("li_at=abc\nJSESSIONID=xyz")["JSESSIONID"] == "xyz"


def test_csrf_token_strips_quotes():
    assert voyager.csrf_token({"JSESSIONID": '"ajax:123"'}) == "ajax:123"
    assert voyager.csrf_token({}) is None


def test_client_requires_both_session_cookies():
    with pytest.raises(voyager.AuthError):
        voyager.VoyagerClient({"li_at": "abc"})
    with pytest.raises(voyager.AuthError):
        voyager.VoyagerClient({"JSESSIONID": "abc"})
    # Both present is enough to construct (no network yet).
    voyager.VoyagerClient({"li_at": "abc", "JSESSIONID": '"tok"'})


def test_extract_ids_and_tokens_are_ordered_and_unique():
    ids = voyager.extract_activity_ids(fixtures.SAVED_PAGE_1)
    assert ids == [fixtures.POST_ID]
    tokens = voyager.extract_pagination_tokens(fixtures.SAVED_PAGE_1)
    assert tokens == ["TOKEN-A"]


def test_extract_activity_ids_ignores_ids_embedded_in_links_or_text():
    data = {
        "data": {
            "elements": [
                {"item": {"entityResult": {"entityUrn": "urn:li:activity:111"}}}
            ]
        },
        # A saved post that links to a *different* post must not add that one.
        "included": [
            {
                "commentary": {
                    "text": {
                        "text": "see https://www.linkedin.com/feed/update/urn:li:activity:999/"
                    }
                }
            }
        ],
    }
    assert voyager.extract_activity_ids(data) == ["111"]


def test_extract_activity_ids_falls_back_to_regex_within_data():
    data = {"data": {"oddShape": {"deep": ["urn:li:share:222"]}}}
    assert voyager.extract_activity_ids(data) == ["222"]


def test_parse_update_reads_content_author_metrics_and_media():
    item = fixtures.FEED_UPDATE["included"][0]
    parsed = voyager.parse_update(item, urn=f"urn:li:activity:{fixtures.POST_ID}")

    assert parsed["full_text"].startswith("Pricing teardown")
    assert parsed["author_name"] == "Jane Doe"
    assert parsed["author_headline"] == "Head of Growth at Acme"
    assert parsed["author_id"] == "1001"
    assert parsed["likes"] == 42
    assert parsed["comments"] == 7
    assert parsed["reposts"] == 3
    assert parsed["post_type"] == "article"
    assert parsed["article_title"] == "The pricing playbook"
    assert parsed["article_url"] == "https://example.com/playbook"
    assert "pricing" in parsed["hashtags"]
    assert "sam" in parsed["mentions"]
    assert "https://example.com/pricing" in parsed["links"]
    # Largest vector image artifact is chosen.
    assert parsed["media"][0]["url"].endswith("post.jpg")
    assert parsed["author_profile_image"].endswith("big.jpg")


def test_parse_update_extracts_handle_from_url_slug():
    parsed = voyager.parse_update(
        fixtures.LISTING_UPDATE, urn=f"urn:li:activity:{fixtures.OTHER_POST_ID}"
    )
    assert parsed["author_handle"] == "alex-kim"


def test_parse_updates_ignores_non_update_entities():
    records = voyager.parse_updates(fixtures.FEED_UPDATE)
    assert len(records) == 1
    assert records[0]["urn"] == f"urn:li:activity:{fixtures.POST_ID}"


def test_client_with_transport_does_not_require_cookies():
    calls = []

    class FakeTransport:
        def get_json(self, url, params=None):
            calls.append(url)
            return fixtures.SAVED_PAGE_1

    client = voyager.VoyagerClient(transport=FakeTransport(), min_interval=0)
    pages = list(client.iter_saved_pages(page_delay=0, max_pages=1))
    assert pages[0].activity_ids == [fixtures.POST_ID]
    assert calls and calls[0].endswith("/graphql")


def test_transport_auth_error_propagates():
    class FailingTransport:
        def get_json(self, url, params=None):
            raise voyager.AuthError("rejected")

    client = voyager.VoyagerClient(transport=FailingTransport(), min_interval=0)
    with pytest.raises(voyager.AuthError):
        client.whoami()


def test_probe_reports_per_endpoint_status():
    client = voyager.VoyagerClient({"li_at": "a", "JSESSIONID": "t"}, min_interval=0)

    def fake_request(url, params=None):
        if url.endswith("/me"):
            return {"firstName": "Jane", "lastName": "Doe"}
        return fixtures.SAVED_PAGE_1

    client._request = fake_request  # type: ignore[method-assign]
    probe = client.probe()
    assert probe["me"]["ok"] is True
    assert probe["me"]["member"] == "Jane Doe"
    assert probe["saved_posts"]["ok"] is True


def test_probe_distinguishes_bad_session_from_bad_listing():
    client = voyager.VoyagerClient({"li_at": "a", "JSESSIONID": "t"}, min_interval=0)

    def fake_request(url, params=None):
        if url.endswith("/me"):
            return {"firstName": "Jane"}
        raise voyager.AuthError("401 on graphql")

    client._request = fake_request  # type: ignore[method-assign]
    probe = client.probe()
    assert probe["me"]["ok"] is True
    assert probe["saved_posts"]["ok"] is False
    assert probe["saved_posts"]["status"] == 401


def test_iter_saved_pages_chains_pagination_tokens():
    client = voyager.VoyagerClient(
        {"li_at": "abc", "JSESSIONID": '"tok"'}, min_interval=0
    )
    pages = {
        None: fixtures.SAVED_PAGE_1,
        "TOKEN-A": fixtures.SAVED_PAGE_2,
        "TOKEN-B": fixtures.SAVED_PAGE_3_EMPTY,
    }
    seen_queries = []

    def fake_request(url, params=None):
        token = None
        if params and "paginationToken" in params["variables"]:
            token = params["variables"].split("paginationToken:", 1)[1]
            token = token.split(",", 1)[0].rstrip(")")
        seen_queries.append(token)
        return pages[token]

    client._request = fake_request  # type: ignore[method-assign]
    collected = list(client.iter_saved_pages(page_delay=0))

    assert [p.number for p in collected] == [1, 2, 3]
    assert collected[0].activity_ids == [fixtures.POST_ID]
    assert collected[1].activity_ids == [fixtures.OTHER_POST_ID]
    assert collected[2].activity_ids == []
    # Token-B is a dead end (empty page) so pagination stops there.
    assert seen_queries == [None, "TOKEN-A", "TOKEN-B"]


def test_iter_saved_pages_honors_max_pages():
    client = voyager.VoyagerClient({"li_at": "abc", "JSESSIONID": "t"}, min_interval=0)
    client._request = lambda url, params=None: fixtures.SAVED_PAGE_1  # type: ignore[method-assign]
    pages = list(client.iter_saved_pages(page_delay=0, max_pages=1))
    assert len(pages) == 1
