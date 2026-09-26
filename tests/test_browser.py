import sys
import types

import pytest

from lbm import browser
from tests import fixtures


def test_harvest_saved_pages_dedupes_across_responses():
    class FakeTransport:
        def capture_saved_post_pages(self, **kwargs):
            # The page can fetch overlapping windows while scrolling.
            return [fixtures.SAVED_PAGE_1, fixtures.SAVED_PAGE_1, fixtures.SAVED_PAGE_2]

    pages = browser.harvest_saved_pages(FakeTransport())
    ids = [pid for page in pages for pid in page.activity_ids]
    assert ids == [fixtures.POST_ID, fixtures.OTHER_POST_ID]
    # The second response parsed inline content too.
    assert any(page.records for page in pages)


class FakeMouse:
    def wheel(self, x, y):
        pass


def _fake_page(responses):
    class FakeResponse:
        def __init__(self, url, body):
            self.url = url
            self._body = body

        def json(self):
            return self._body

    class FakePage:
        url = "https://www.linkedin.com/feed/"

        def __init__(self):
            self._cb = None
            self.mouse = FakeMouse()

        def on(self, event, cb):
            self._cb = cb

        def remove_listener(self, event, cb):
            pass

        def goto(self, url, wait_until=None):
            for resp in responses:
                self._cb(FakeResponse(*resp))

        def wait_for_timeout(self, ms):
            pass

    return FakePage()


def test_ensure_logged_in_raises_without_allow_interactive():
    transport = browser.BrowserTransport()
    transport.page = _fake_page([])
    transport.page.url = "https://www.linkedin.com/uas/login"
    with pytest.raises(browser.AuthError):
        transport.ensure_logged_in(allow_interactive=False)


def test_ensure_logged_in_returns_true_when_already_logged_in():
    transport = browser.BrowserTransport()
    transport.page = _fake_page([])
    transport.page.url = "https://www.linkedin.com/feed/"
    assert transport.ensure_logged_in(allow_interactive=False) is True


def test_capture_saved_post_pages_filters_to_saved_responses():
    transport = browser.BrowserTransport()
    transport.page = _fake_page(
        [
            ("https://www.linkedin.com/voyager/api/graphql?variables=(savedPostType)", fixtures.SAVED_PAGE_1),
            ("https://www.linkedin.com/voyager/api/graphql?variables=(unrelated)", {"data": {"x": 1}}),
        ]
    )
    bodies = transport.capture_saved_post_pages(max_scrolls=0)
    assert bodies == [fixtures.SAVED_PAGE_1]


def test_capture_saved_post_pages_falls_back_to_activity_ids():
    transport = browser.BrowserTransport()
    transport.page = _fake_page(
        [
            ("https://www.linkedin.com/voyager/api/graphql?variables=(oddShape)", fixtures.SAVED_PAGE_2),
            ("https://www.linkedin.com/voyager/api/graphql?variables=(empty)", {"data": {}}),
        ]
    )
    bodies = transport.capture_saved_post_pages(max_scrolls=0)
    assert bodies == [fixtures.SAVED_PAGE_2]


def test_query_id_from_url():
    url = "https://www.linkedin.com/voyager/api/graphql?variables=(x)&queryId=voyagerSearchDashClusters.abc123"
    assert browser._query_id_from_url(url) == "voyagerSearchDashClusters.abc123"
    assert browser._query_id_from_url("https://x/other") is None


def test_discover_query_id_prefers_saved_posts_request():
    class FakePage:
        url = "https://www.linkedin.com/feed/"

        def __init__(self):
            self._cb = None

        def on(self, event, cb):
            self._cb = cb

        def remove_listener(self, event, cb):
            pass

        def goto(self, url, wait_until=None):
            # Simulate the site firing two GraphQL requests; the saved-posts one
            # must win over the unrelated search request.
            for req in (
                "https://www.linkedin.com/voyager/api/graphql?queryId=voyagerSearchDashClusters.zzz",
                "https://www.linkedin.com/voyager/api/graphql?queryId=voyagerSearchDashClusters.abc"
                "&variables=(query:(flagshipSearchIntent:SEARCH_MY_ITEMS_SAVED_POSTS))",
            ):
                self._cb(types.SimpleNamespace(url=req))

        def wait_for_timeout(self, ms):
            pass

        def mouse(self):
            raise AssertionError("should not need to scroll")

    transport = browser.BrowserTransport()
    transport.page = FakePage()
    assert transport.discover_query_id() == "voyagerSearchDashClusters.abc"


class FakeCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


def _install_fake_bc3(monkeypatch, chrome):
    fake = types.ModuleType("browser_cookie3")
    fake.chrome = chrome
    monkeypatch.setitem(sys.modules, "browser_cookie3", fake)


def test_cookies_from_browser_returns_linkedin_cookies(monkeypatch, tmp_path):
    def chrome(cookie_file=None, domain_name=None):
        return [
            FakeCookie("li_at", "ABC"),
            FakeCookie("JSESSIONID", '"ajax:1"'),
            FakeCookie("bcookie", "x"),
        ]

    _install_fake_bc3(monkeypatch, chrome)
    monkeypatch.setattr(browser, "_newest_chromium_profile", lambda family, profile=None: tmp_path / "Cookies")

    cookies = browser.cookies_from_browser(browser="chrome")
    assert cookies["li_at"] == "ABC"
    assert cookies["JSESSIONID"] == '"ajax:1"'


def test_cookies_from_browser_raises_without_li_at(monkeypatch, tmp_path):
    _install_fake_bc3(monkeypatch, lambda **kw: [FakeCookie("bcookie", "x")])
    monkeypatch.setattr(browser, "_newest_chromium_profile", lambda family, profile=None: tmp_path / "Cookies")

    with pytest.raises(browser.BrowserCookieError):
        browser.cookies_from_browser(browser="chrome")


def test_cookies_from_browser_honours_explicit_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "_support_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "CHROMIUM_FAMILIES", {"chrome": "Chrome"})
    (tmp_path / "Chrome" / "Profile 9").mkdir(parents=True)
    (tmp_path / "Chrome" / "Profile 9" / "Cookies").write_text("x")

    captured = {}

    def chrome(cookie_file=None, domain_name=None):
        captured["cookie_file"] = cookie_file
        return [FakeCookie("li_at", "ABC"), FakeCookie("JSESSIONID", "j")]

    _install_fake_bc3(monkeypatch, chrome)
    browser.cookies_from_browser(browser="chrome", profile="Profile 9")
    assert captured["cookie_file"].endswith("Profile 9/Cookies")
