from lbm import importers as im


def test_snowflake_matches_linkedin_own_example():
    # LinkedIn's compliance docs pair this 2018 activity id with a 2018-07-11
    # timestamp, so it is a real regression anchor for the >> 22 decode.
    iso = im.snowflake_to_iso("6422861848709726208")
    assert iso is not None
    assert iso.startswith("2018-07-11")


def test_snowflake_rejects_implausible_ids():
    assert im.snowflake_to_iso("1") is None
    assert im.snowflake_to_iso("not-a-number") is None
    assert im.snowflake_to_iso(None) is None


def test_activity_id_from_url_and_urn():
    assert im.activity_id_from_url(
        "https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/"
    ) == "7123456789012345678"
    assert im.activity_id_from_url(
        "https://www.linkedin.com/posts/jane-doe_pricing-activity-7123456789012345678-abc"
    ) == "7123456789012345678"
    assert im.activity_id_from_urn("urn:li:ugcPost:99") == "99"
    assert im.activity_id_from_url("https://example.com/other") is None


def test_from_voyager_normalizes_and_derives_created_at():
    parsed = {
        "urn": "urn:li:activity:6422861848709726208",
        "full_text": "hello world",
        "author_name": "Jane Doe",
        "author_handle": "jane-doe",
        "likes": 5,
        "hashtags": ["pricing"],
        "mentions": ["sam"],
        "links": ["https://example.com"],
    }
    rec = im.from_voyager(parsed, observed_at="2026-01-01T00:00:00+00:00")
    assert rec["post_id"] == "6422861848709726208"
    assert rec["author_handle"] == "jane-doe"
    assert rec["created_at"].startswith("2018-07-11")  # decoded from the id
    assert rec["origin"] == im.ORIGIN_VOYAGER
    assert rec["hashtags_json"] == '["pricing"]'
    assert rec["url"].endswith("urn:li:activity:6422861848709726208/")


def test_from_voyager_stub_keeps_saved_post_without_content():
    rec = im.from_voyager_stub("6422861848709726208", sort_index=3, observed_at="2026-01-01T00:00:00+00:00")
    assert rec["post_id"] == "6422861848709726208"
    assert rec["sort_index"] == 3
    assert rec.get("full_text") is None
    assert rec["created_at"].startswith("2018-07-11")


def test_from_export_row_with_activity_url():
    rec = im.from_export_row(
        "https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/",
        saved_at="2026-02-01 09:30:00",
        observed_at="2026-02-02T00:00:00+00:00",
    )
    assert rec["post_id"] == "7123456789012345678"
    assert rec["saved_at"].startswith("2026-02-01")
    assert rec["origin"] == im.ORIGIN_EXPORT


def test_from_export_row_with_unknown_domain_gets_stable_id():
    a = im.from_export_row("https://example.com/article?id=1")
    b = im.from_export_row("https://example.com/article?id=1")
    assert a["post_id"] == b["post_id"]
    assert a["post_id"].startswith("url:")


def test_int_parsing_handles_linkedin_abbreviations():
    assert im._int("1.5K") == 1500
    assert im._int("2M") == 2_000_000
    assert im._int("1,234") == 1234
    assert im._int("") is None
