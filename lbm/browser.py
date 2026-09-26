"""Read LinkedIn cookies straight from an installed browser.

Copying a `li_at` value by hand is surprisingly error-prone: browsers can hold
several cookies with the same name scoped to different domains
(`.linkedin.com` vs `www.linkedin.com`), and the website rotates session tokens.
Reading the browser's own cookie store gets the exact, current values instead.

No network access happens here; the values are only read from disk. On macOS,
decrypting a Chromium-family store uses the OS keychain, so the first run may
show a keychain approval prompt.

It also provides ``BrowserTransport``: instead of sending cookies through
``requests``, it drives a real, persistent browser and issues the same Voyager
requests from inside a linkedin.com page. LinkedIn frequently invalidates
sessions that make API calls from a non-browser client, so this is the durable
way to sync.
"""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from urllib.parse import urlencode

from . import db
from .voyager import (
    AuthError,
    SavedPage,
    VoyagerError,
    extract_activity_ids,
    parse_updates,
)

CHROMIUM_FAMILIES = {
    "chrome": "Google/Chrome",
    "chromium": "Chromium",
    "brave": "BraveSoftware/Brave-Browser",
    "edge": "Microsoft Edge",
    "arc": "Arc",
    "vivaldi": "Vivaldi",
}


class BrowserCookieError(RuntimeError):
    pass


def _support_dir() -> Path:
    return Path(os.path.expanduser("~/Library/Application Support"))


def _newest_chromium_profile(family: str, profile: str | None = None) -> Path | None:
    """Newest cookie DB for a Chromium-family browser, honouring an override."""
    root = _support_dir() / CHROMIUM_FAMILIES[family]
    if not root.exists():
        return None
    if profile:
        candidate = root / profile / "Cookies"
        return candidate if candidate.exists() else None
    candidates = [
        Path(p) for p in glob.glob(str(root / "*" / "Cookies"))
    ] + [Path(p) for p in glob.glob(str(root / "*" / "Network" / "Cookies"))]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def cookies_from_browser(browser: str | None = None, profile: str | None = None) -> dict[str, str]:
    """Return all LinkedIn cookies found in a local browser.

    Defaults to the browser whose cookie store was most recently written, and
    among Chromium-family browsers to the most recently used profile.
    """
    try:
        import browser_cookie3 as bc3
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise BrowserCookieError(
            "browser-cookie3 is not installed; run `uv sync`"
        ) from exc

    order = [browser] if browser else ["chrome", "chromium", "brave", "edge", "arc", "vivaldi", "firefox", "safari"]
    last_error: Exception | None = None

    for name in order:
        if name is None:
            continue
        try:
            if name in CHROMIUM_FAMILIES:
                cookie_file = _newest_chromium_profile(name, profile)
                if cookie_file is None:
                    continue
                jar = bc3.chrome(cookie_file=str(cookie_file), domain_name="linkedin.com")
            elif name == "firefox":
                jar = bc3.firefox(domain_name="linkedin.com")
            elif name == "safari":
                jar = bc3.safari(domain_name="linkedin.com")
            else:
                continue
        except Exception as exc:  # noqa: BLE001 - surface whichever browser failed
            last_error = exc
            continue

        cookies = {c.name: c.value for c in jar if c.value}
        if "li_at" in cookies:
            return cookies

    message = (
        "could not read LinkedIn cookies from any local browser"
        + (f" (last error: {last_error})" if last_error else "")
        + ".\nOn macOS the first read may prompt for keychain access; approve it "
        "and retry. If it still fails, log into linkedin.com in that browser first, "
        "or log in with the cookie-header method (`pbpaste | lbm login --stdin`)."
    )
    raise BrowserCookieError(message)


# --------------------------------------------------------------------------- #
# browser transport
# --------------------------------------------------------------------------- #

class BrowserError(RuntimeError):
    """The browser engine is unavailable or not usable."""


def _profile_dir() -> Path:
    path = db.data_dir() / "browser-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _import_playwright():
    try:  # patchright is a stealth-patched Playwright fork; preferred.
        from patchright.sync_api import sync_playwright
        return sync_playwright, "patchright"
    except ImportError:
        pass
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright, "playwright"
    except ImportError as exc:
        raise BrowserError(
            "the browser engine needs patchright or playwright. Install it with:\n"
            "  uv add patchright && uv run patchright install chromium"
        ) from exc


# Runs inside the linkedin.com page: same origin, real cookies, real fingerprint.
_JS_FETCH = """
async (url) => {
  const m = document.cookie.match(/(?:^|;\\s*)JSESSIONID=("?)([^;"]+)\\1/);
  const csrf = m ? m[2] : "";
  const r = await fetch(url, {
    credentials: "include",
    headers: {
      "accept": "application/vnd.linkedin.normalized+json+2.1",
      "x-restli-protocol-version": "2.0.0",
      "csrf-token": csrf,
    },
  });
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  return { status: r.status, body: body };
}
"""


_QUERY_ID_RE = re.compile(r"[?&]queryId=([^&]+)")
SAVED_POSTS_PAGE = "https://www.linkedin.com/my-items/saved-posts/"


def _query_id_from_url(url: str) -> str | None:
    match = _QUERY_ID_RE.search(url or "")
    return match.group(1) if match else None


class BrowserTransport:
    """Issue Voyager requests from inside a persistent, logged-in browser page."""

    def __init__(self, *, headless: bool = False, timeout_ms: int = 45000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._pw = None
        self._sync_playwright = None
        self.context = None
        self.page = None
        self.engine = None

    # -- lifecycle -------------------------------------------------------- #

    def start(self) -> "BrowserTransport":
        if self.page is not None:
            return self
        sync_playwright, engine = _import_playwright()
        self._sync_playwright = sync_playwright
        self.engine = engine
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(_profile_dir()),
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        return self

    def ensure_page(self) -> None:
        if self.page is None:
            self.start()
        if not (self.page.url or "") or self.page.url == "about:blank":
            self.page.goto(
                "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
            )

    def login_interactive(self, timeout_ms: int = 300_000) -> None:
        """Open a window and wait for the user to log in; the profile persists."""
        self.start()
        self.page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        self.page.wait_for_url("**/feed/**", timeout=timeout_ms)

    def is_logged_in(self) -> bool:
        self.ensure_page()
        url = self.page.url or ""
        return "linkedin.com" in url and "/login" not in url and "checkpoint" not in url

    def ensure_logged_in(
        self, *, allow_interactive: bool = False, timeout_ms: int = 300_000
    ) -> bool:
        """Return True if the persistent session is logged in.

        If it is not, and ``allow_interactive`` is set (i.e. a human is at the
        terminal), a window is opened just long enough to log in, then the
        session goes back to the requested (usually headless) mode.
        """
        if self.is_logged_in():
            return True
        if not allow_interactive:
            raise AuthError(
                "the browser session is not logged in. Run `lbm login "
                "--interactive` once (a window will open), then retry."
            )

        was_headless = self.headless
        self.close()
        self.headless = False
        self.start()
        self.login_interactive(timeout_ms=timeout_ms)
        if was_headless:  # go back to invisible once we have the session
            self.close()
            self.headless = True
            self.start()
        if not self.is_logged_in():
            raise AuthError("login did not complete; run `lbm login --interactive`")
        return True

    def discover_query_id(
        self, page_url: str = SAVED_POSTS_PAGE, wait_ms: int = 15000
    ) -> str | None:
        """Read the saved-posts GraphQL query id the website itself uses.

        Hardcoding this hash is the main fragility of the HTTP engine: LinkedIn
        rotates it. Here a real page load reveals the current one.
        """
        self.ensure_page()
        seen: list[str] = []

        def on_request(request) -> None:
            url = getattr(request, "url", "") or ""
            if "/voyager/api/graphql" in url and "queryId=" in url:
                seen.append(url)

        self.page.on("request", on_request)
        try:
            self.page.goto(page_url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(wait_ms)
            if not seen:  # nudge lazy loading if nothing fired yet
                self.page.mouse.wheel(0, 2000)
                self.page.wait_for_timeout(4000)
        finally:
            try:
                self.page.remove_listener("request", on_request)
            except Exception:  # noqa: BLE001
                pass

        for url in seen:
            if "saved" in url.lower():
                return _query_id_from_url(url)
        for url in seen:
            if "clusters" in url.lower() or "searchdash" in url.lower():
                return _query_id_from_url(url)
        return _query_id_from_url(seen[0]) if seen else None

    # -- transport interface --------------------------------------------- #

    def capture_saved_post_pages(
        self,
        *,
        max_scrolls: int = 40,
        settle_ms: int = 2500,
        page_url: str = SAVED_POSTS_PAGE,
        progress=None,
    ) -> list[object]:
        """Load the saved-posts page and collect its own GraphQL responses.

        This deliberately does not reconstruct the request: it lets the website
        paginate itself (we scroll and click "load more" if present) and harvests
        whatever the page fetches. That sidesteps both the rotating query id and
        any change to the request shape.
        """
        self.ensure_page()
        responses: list[tuple[str, object]] = []

        def on_response(response) -> None:
            url = getattr(response, "url", "") or ""
            if "/voyager/api/graphql" not in url:
                return
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - not every response is JSON
                return
            responses.append((url, body))

        self.page.on("response", on_response)
        try:
            self.page.goto(page_url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(settle_ms)
            stable = 0
            last = len(responses)
            for _ in range(max(0, max_scrolls)):
                self.page.mouse.wheel(0, 4000)
                self.page.wait_for_timeout(settle_ms)
                # Some layouts need an explicit button rather than infinite scroll.
                if self._click_load_more(settle_ms):
                    pass
                if progress:
                    progress(len(responses))
                if len(responses) == last:
                    stable += 1
                    # Require several quiet rounds before concluding the list ended.
                    if stable >= 4:
                        break
                else:
                    stable = 0
                    last = len(responses)
        finally:
            try:
                self.page.remove_listener("response", on_response)
            except Exception:  # noqa: BLE001
                pass

        saved = [body for url, body in responses if "saved" in url.lower()]
        if saved:
            return saved
        # Fall back to anything with activity ids rather than returning nothing.
        return [body for _, body in responses if extract_activity_ids(body)]

    def _click_load_more(self, settle_ms: int) -> bool:
        """Click a "show/load more" control if the page has one; return whether."""
        try:
            button = self.page.query_selector(
                "button:has-text('Show more results'), button:has-text('Load more'), "
                "button:has-text('Show more')"
            )
            if button is not None and button.is_visible():
                button.click()
                self.page.wait_for_timeout(settle_ms)
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def get_json(self, url: str, params: dict | None = None) -> object:
        self.ensure_page()
        full = f"{url}?{urlencode(params)}" if params else url
        result = self.page.evaluate(_JS_FETCH, full)
        status = result.get("status")
        if status in (401, 403):
            raise AuthError(
                f"LinkedIn rejected the browser session (HTTP {status}). log in "
                "again with `lbm login --interactive`."
            )
        if status == 429:
            raise VoyagerError("LinkedIn rate limited the browser session (HTTP 429)")
        if status != 200:
            raise VoyagerError(f"unexpected HTTP {status} from {url}")
        if result.get("body") is None:
            raise VoyagerError(f"non-JSON response from {url}")
        return result["body"]

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass
        finally:
            self.context = None
            self.page = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None


def harvest_saved_pages(
    transport: "BrowserTransport", max_scrolls: int = 40, progress=None
) -> list[SavedPage]:
    """Turn harvested saved-posts responses into ordered SavedPage objects.

    Activity ids are de-duplicated across responses, because the page can fetch
    overlapping windows while scrolling.
    """
    bodies = transport.capture_saved_post_pages(max_scrolls=max_scrolls, progress=progress)
    pages: list[SavedPage] = []
    seen: set[str] = set()
    for number, body in enumerate(bodies, start=1):
        ids = [pid for pid in extract_activity_ids(body) if pid not in seen]
        if not ids and not parse_updates(body):
            continue
        seen.update(ids)
        pages.append(
            SavedPage(
                number=number,
                activity_ids=ids,
                records=parse_updates(body),
                tokens=[],
                raw=body,
            )
        )
    return pages
