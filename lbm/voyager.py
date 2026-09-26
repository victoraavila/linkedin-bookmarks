"""Cookie-based client for LinkedIn's internal "Voyager" API.

This is the LinkedIn analogue of ``twscrape`` for X: it talks to the same
private GraphQL/REST endpoints the website itself uses, authenticated only with
the cookies from a logged-in browser session. No official API access, no
partner program, no per-call cost.

Two operations matter here:

* **Listing saved posts.** ``voyagerSearchDashClusters`` with the search intent
  ``SEARCH_MY_ITEMS_SAVED_POSTS`` returns the saved-posts list newest-first and
  paginates by an opaque ``paginationToken`` that has to be chained.
* **Fetching one post.** ``/voyager/api/feed/updates/<urn>`` returns the
  normalized entity graph for a single post: commentary text, author, reaction
  and comment counts, media, articles and comments.

Both responses use LinkedIn's ``normalized+json`` shape: a ``data`` object plus a
flat ``included`` array of entities keyed by ``$type``. Parsing here is
deliberately tolerant — it looks for known entity types and degrades to regex
extraction rather than assuming one exact schema — because LinkedIn rotates
field names and query ids without notice.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping
from urllib.parse import quote

import requests

BASE_URL = "https://www.linkedin.com"
GRAPHQL_URL = f"{BASE_URL}/voyager/api/graphql"
FEED_UPDATE_URL = f"{BASE_URL}/voyager/api/feed/updates/{{urn}}"
ME_URL = f"{BASE_URL}/voyager/api/me"

# The GraphQL query hash for the saved-posts list. LinkedIn's own docs do not
# publish this; it is captured from the website. If listing starts failing with
# an HTTP 4xx from GraphQL, this hash has rotated — see the README's
# troubleshooting, which is the same failure mode as X's twscrape query ids.
SAVED_POSTS_QUERY_ID = "voyagerSearchDashClusters.843215f2a3455f1bed85762a45d71be8"

SAVED_POSTS_INTENT = "SEARCH_MY_ITEMS_SAVED_POSTS"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/vnd.linkedin.normalized+json+2.1",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Restli-Protocol-Version": "2.0.0",
    "x-li-lang": "en_US",
}
# Note: no `x-li-track` header. It carries a client version that goes stale and
# has no bearing on auth; the minimal proven header set avoids version-related
# rejections.

URN_TYPES = ("activity", "share", "ugcPost")

_ACTIVITY_ID_RE = re.compile(r"urn:li:(?:activity|share|ugcPost):(\d+)")
_PAGINATION_TOKEN_RE = re.compile(r'"paginationToken"\s*:\s*"([^"]+)"')
_RESTLI_URN_RE = re.compile(r"urn:li:[A-Za-z_]+:[^\s\"',)]+")
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


class VoyagerError(RuntimeError):
    """A sync cannot proceed: bad credentials, rotated query id, or upstream error."""


class AuthError(VoyagerError):
    """The stored LinkedIn session is missing, invalid or expired."""


def parse_cookie_string(raw: str) -> dict[str, str]:
    """Parse a browser cookie header (or two-line li_at/JSESSIONID paste)."""
    cookies: dict[str, str] = {}
    for chunk in re.split(r"[;\n\r]+", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip().strip('"')
        value = value.strip().strip('"').strip()
        if name:
            cookies[name] = value
    return cookies


def cookie_string(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def csrf_token(cookies: Mapping[str, str]) -> str | None:
    for name in ("JSESSIONID", "jsessionid"):
        value = cookies.get(name)
        if value:
            return value.strip('"')
    return None


# --------------------------------------------------------------------------- #
# response parsing
# --------------------------------------------------------------------------- #

def _walk(node: Any) -> Iterator[Any]:
    """Yield every node inside a decoded JSON structure, scalars included."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _entities(data: Any, type_needle: str) -> list[dict]:
    found = []
    for item in _walk(data):
        if isinstance(item, dict):
            type_name = str(item.get("$type") or "")
            if type_needle in type_name:
                found.append(item)
    return found


_URN_KEYS = ("entityUrn", "urn", "targetUrn", "backendUrn")


def extract_activity_ids(data: Any) -> list[str]:
    """Saved-post activity ids in a listing response, first-seen order.

    Structured first: only URNs on entities inside the response's ``data``
    subtree, so an activity id that merely *appears in a post's text or a linked
    URL* is not mistaken for a saved post. If that finds nothing (schema
    variation), it falls back to a regex sweep of the ``data`` subtree only —
    never ``included``, which is where related/suggested entities live.
    """
    scope = data.get("data") if isinstance(data, dict) and "data" in data else data

    seen: dict[str, None] = {}
    for node in _walk(scope):
        if not isinstance(node, dict):
            continue
        for key in _URN_KEYS:
            value = node.get(key)
            if isinstance(value, str):
                match = _ACTIVITY_ID_RE.search(value)
                if match:
                    seen.setdefault(match.group(1), None)
    if seen:
        return list(seen)

    for match in _ACTIVITY_ID_RE.finditer(json.dumps(scope)):
        seen.setdefault(match.group(1), None)
    return list(seen)


def extract_pagination_tokens(data: Any) -> list[str]:
    """Pagination tokens in a listing response.

    Scoped to the ``data`` subtree and preferring the structured
    ``paginationToken`` field, so a literal token inside a post's text is not
    followed as if it were a page cursor.
    """
    scope = data.get("data") if isinstance(data, dict) and "data" in data else data
    seen: dict[str, None] = {}
    for node in _walk(scope):
        if isinstance(node, dict):
            token = node.get("paginationToken")
            if isinstance(token, str) and token:
                seen.setdefault(token, None)
    if seen:
        return list(seen)
    for match in _PAGINATION_TOKEN_RE.finditer(json.dumps(scope)):
        seen.setdefault(match.group(1), None)
    return list(seen)


def _text(node: Any) -> str | None:
    """Pull text out of LinkedIn's ``{"text": {"text": "..."}}`` wrappers."""
    if node is None:
        return None
    if isinstance(node, str):
        return node or None
    if isinstance(node, dict):
        for key in ("text", "value", "title"):
            if key in node:
                inner = _text(node[key])
                if inner:
                    return inner
    return None


def _vector_image_url(node: Any) -> str | None:
    """Build a usable URL from a LinkedIn ``vectorImage`` entity."""
    if not isinstance(node, dict):
        return None
    root = node.get("rootUrl")
    artifacts = node.get("artifacts") or []
    segment = None
    if isinstance(artifacts, list) and artifacts:
        # Prefer the largest artifact by width.
        best = max(
            (a for a in artifacts if isinstance(a, dict)),
            key=lambda a: a.get("width") or 0,
            default=None,
        )
        if best:
            segment = (
                best.get("fileIdentifyingUrlPathSegment")
                or best.get("fileIdentifyingUrl")
            )
    if root and segment:
        return f"{root}{segment}"
    for candidate in (
        node.get("url"),
        node.get("fileIdentifyingUrl"),
    ):
        if candidate:
            return str(candidate)
    return None


def _profile_image(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    for value in _walk(node):
        if isinstance(value, dict):
            found = _vector_image_url(value)
            if found:
                return found
    return None


def _find_urn(node: Any) -> str | None:
    for value in _walk(node):
        if isinstance(value, str):
            match = _RESTLI_URN_RE.search(value)
            if match:
                return match.group(0)
    return None


def _collect_media(item: dict) -> list[dict]:
    media: list[dict] = []
    content = item.get("content") or {}
    if not isinstance(content, dict):
        return media

    # Media may live directly on content, or nested under a media union.
    candidates = []
    if isinstance(content.get("media"), dict):
        candidates.append(content["media"])
    for value in _walk(content):
        if isinstance(value, dict) and isinstance(value.get("vectorImage"), dict):
            candidates.append(value)

    seen: set[str] = set()
    for node in candidates:
        url = _vector_image_url(node.get("vectorImage")) or _vector_image_url(node)
        if not url or url in seen:
            continue
        seen.add(url)
        media.append(
            {
                "type": node.get("type") or "image",
                "url": url,
                "thumbnail_url": url,
            }
        )

    # Video / document thumbnails.
    for node in _entities(content, "videoComponent"):
        thumb = _vector_image_url((node.get("thumbnail") or {}).get("vectorImage"))
        if thumb and thumb not in seen:
            seen.add(thumb)
            media.append({"type": "video", "url": thumb, "thumbnail_url": thumb})
    for node in _entities(content, "documentComponent"):
        thumb = _vector_image_url((node.get("thumbnail") or {}).get("vectorImage"))
        if thumb and thumb not in seen:
            seen.add(thumb)
            media.append({"type": "document", "url": thumb, "thumbnail_url": thumb})
    return media


def _collect_links(item: dict, text: str | None) -> list[str]:
    links: dict[str, None] = {}
    if text:
        for match in _URL_RE.finditer(text):
            links.setdefault(match.group(0).rstrip(".,);"), None)
    content = item.get("content") or {}
    article = content.get("articleComponent") if isinstance(content, dict) else None
    if isinstance(article, dict):
        for key in ("navigationUrl", "shortenedUrl", "url"):
            if article.get(key):
                links.setdefault(str(article[key]), None)
    for value in _walk(item):
        if isinstance(value, dict):
            for key in ("navigationUrl", "shortenedUrl"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.startswith("http"):
                    links.setdefault(candidate, None)
    return list(links)


def _collect_hashtags(text: str | None, item: dict) -> list[str]:
    found: dict[str, None] = {}
    for value in _walk(item):
        if isinstance(value, dict) and str(value.get("type", "")).upper() == "HASHTAG":
            tag = _text(value.get("detailData")) or value.get("trackingUrn")
            if tag:
                found.setdefault(str(tag).lstrip("#"), None)
    for match in re.finditer(r"#([A-Za-z0-9_]+)", text or ""):
        found.setdefault(match.group(1), None)
    return list(found)


def _collect_mentions(text: str | None) -> list[str]:
    found: dict[str, None] = {}
    for match in re.finditer(r"@([A-Za-z0-9_.\-]{2,})", text or ""):
        found.setdefault(match.group(1), None)
    return list(found)


_HANDLE_RE = re.compile(r"/posts/([^/?#]+?)_[^/?#]*-activity-\d+")
_PROFILE_HANDLE_RE = re.compile(r"linkedin\.com/in/([^/?#]+)")


def _extract_handle(item: dict) -> str | None:
    """The public identifier is not a field; it lives in the post's URL slug."""
    for value in _walk(item):
        if isinstance(value, str):
            match = _HANDLE_RE.search(value) or _PROFILE_HANDLE_RE.search(value)
            if match:
                return match.group(1)
    return None


def _is_repost(item: dict) -> bool:
    for value in _walk(item):
        if isinstance(value, dict):
            header = _text(value.get("header"))
            if header and "repost" in header.lower():
                return True
    return bool(item.get("repostedUpdate"))


def parse_update(item: dict, urn: str | None = None) -> dict:
    """Turn one ``UpdateV2`` entity into a flat, importer-ready dict."""
    actor = item.get("actor") or {}
    if not isinstance(actor, dict):
        actor = {}
    commentary = item.get("commentary")
    text = _text(commentary)

    headline = _text(actor.get("description")) or _text(actor.get("subDescription"))
    author_name = _text(actor.get("name"))
    author_urn = actor.get("urn") or actor.get("actorUrn")
    author_id = None
    if isinstance(author_urn, str):
        match = re.search(r":(\d+)$", author_urn)
        author_id = match.group(1) if match else author_urn

    content = item.get("content") or {}
    article = content.get("articleComponent") if isinstance(content, dict) else None
    if not isinstance(article, dict):
        article = {}

    # Metrics: prefer the explicit social count entity, then the nested summary.
    likes = comments = reposts = None
    for node in _entities(item, "SocialActivityCounts"):
        likes = node.get("numLikes", likes)
        comments = node.get("numComments", comments)
        reposts = node.get("numShares", reposts)
    social = item.get("socialDetail") or {}
    counts = social.get("totalSocialActivityCounts") if isinstance(social, dict) else None
    if isinstance(counts, dict):
        likes = counts.get("numLikes", likes)
        comments = counts.get("numComments", comments)
        reposts = counts.get("numShares", reposts)

    post_urn = urn or _find_urn(item.get("updateMetadata") or item.get("metadata") or {})
    if not post_urn:
        post_urn = _find_urn(item)

    created = (
        item.get("createdTime")
        or item.get("createdAt")
        or (item.get("metadata") or {}).get("createdTime")
    )

    return {
        "urn": post_urn,
        "full_text": text,
        "created_at": created,
        "author_id": author_id,
        "author_name": author_name,
        "author_handle": _extract_handle(item),
        "author_headline": headline,
        "author_profile_image": _profile_image(actor.get("image")),
        "likes": likes,
        "comments": comments,
        "reposts": reposts,
        "post_type": "article" if article else "post",
        "is_repost": _is_repost(item),
        "article_title": _text(article.get("title")) or article.get("title"),
        "article_subtitle": _text(article.get("subtitle")) or article.get("subtitle"),
        "article_url": article.get("navigationUrl") or article.get("url"),
        "media": _collect_media(item),
        "hashtags": _collect_hashtags(text, item),
        "mentions": _collect_mentions(text),
        "links": _collect_links(item, text),
        "raw": item if isinstance(item, dict) else None,
    }


def parse_updates(data: Any, urn: str | None = None) -> list[dict]:
    """Parse every ``UpdateV2`` entity in a response into importer-ready dicts."""
    results = []
    for item in _entities(data, "UpdateV2"):
        try:
            results.append(parse_update(item, urn=urn))
        except Exception:  # noqa: BLE001 - never let one bad entity kill a page
            continue
    return results


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #

@dataclass
class SavedPage:
    number: int
    activity_ids: list[str] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    raw: Any = None


class VoyagerClient:
    """A thin, polite, retrying wrapper over the Voyager HTTP API."""

    def __init__(
        self,
        cookies: Mapping[str, str] | None = None,
        *,
        transport: object | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        min_interval: float = 0.75,
        query_id: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.min_interval = min_interval
        self._last_request = 0.0
        self.transport = transport
        self.cookies = dict(cookies or {})
        self.query_id = query_id or SAVED_POSTS_QUERY_ID

        if transport is not None:
            # The browser transport carries its own session; no cookies needed.
            return

        token = csrf_token(self.cookies)
        if not self.cookies.get("li_at"):
            raise AuthError(
                "cookie set has no 'li_at' value; that is the LinkedIn session "
                "cookie and it is required"
            )
        if not token:
            raise AuthError(
                "cookie set has no 'JSESSIONID' value; that is the CSRF token "
                "LinkedIn requires on every Voyager request"
            )
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.headers["csrf-token"] = token
        self.session.cookies.update(self.cookies)
        self.timeout = timeout
        self.min_interval = min_interval
        self._last_request = 0.0

    # -- HTTP ------------------------------------------------------------- #

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _request(self, url: str, *, params: dict | None = None) -> Any:
        if self.transport is not None:
            self._throttle()
            try:
                return self.transport.get_json(url, params)
            finally:
                self._last_request = time.monotonic()

        attempts = 0
        while True:
            attempts += 1
            self._throttle()
            try:
                response = self.session.get(
                    url, params=params, timeout=self.timeout, allow_redirects=False
                )
            except requests.RequestException as exc:
                if attempts >= 4:
                    raise VoyagerError(f"network error talking to LinkedIn: {exc}") from exc
                time.sleep(min(2 ** attempts, 20))
                continue
            finally:
                self._last_request = time.monotonic()

            if 300 <= response.status_code < 400:
                # LinkedIn answers a rejected session by redirecting (often back
                # to the same URL, clearing the cookies). Never follow it: that
                # loops forever.
                location = response.headers.get("Location", "")
                raise AuthError(
                    f"LinkedIn redirected the request (HTTP {response.status_code} "
                    f"-> {location!r}), which is how it rejects a session it considers "
                    "invalid. Run `lbm login --from-browser` again."
                )

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise VoyagerError(
                        "LinkedIn returned a non-JSON body; the session may have "
                        "been redirected to a login or checkpoint page"
                    ) from exc

            if response.status_code in (401, 403):
                raise AuthError(
                    f"LinkedIn rejected the session (HTTP {response.status_code} on "
                    f"{response.url}). The li_at/JSESSIONID cookies are expired or "
                    "invalid — run `lbm login` again."
                )

            if response.status_code == 429:
                if attempts >= 6:
                    raise VoyagerError(
                        "rate limited repeatedly (HTTP 429); LinkedIn is throttling "
                        "this session. Wait, then retry with a smaller --limit."
                    )
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if (retry_after or "").isdigit() else min(30 * attempts, 120)
                time.sleep(wait)
                continue

            if response.status_code in (400, 404):
                raise VoyagerError(
                    f"LinkedIn returned HTTP {response.status_code} for {url}. "
                    "For the saved-posts listing this usually means the GraphQL "
                    "query id has rotated — see the README troubleshooting."
                )

            if attempts >= 4:
                raise VoyagerError(
                    f"unexpected HTTP {response.status_code} from LinkedIn for {url}"
                )
            time.sleep(min(2 ** attempts, 20))

    # -- operations ------------------------------------------------------- #

    def whoami(self) -> dict:
        """Validate the session and return basic profile info."""
        data = self._request(ME_URL)
        name = None
        plain = None
        for item in _walk(data):
            if not isinstance(item, dict):
                continue
            if item.get("firstName"):
                name = " ".join(
                    str(x) for x in (item.get("firstName"), item.get("lastName")) if x
                )
                break
            if plain is None and item.get("plainId") not in (None, ""):
                plain = item.get("plainId")
        if name is None:
            name = str(plain).strip() if plain not in (None, "") else "unknown"
        return {"name": name}

    def probe(self, page_size: int = 1) -> dict:
        """Check each endpoint the sync depends on, reporting per-endpoint status.

        This separates the two failure modes behind a 401: if both ``/me`` and
        the saved-posts listing fail, the session cookie is bad. If ``/me``
        succeeds but the listing fails, the ``csrf-token`` (JSESSIONID) or the
        GraphQL query is the problem.
        """
        results: dict[str, dict] = {}

        def check(name: str, fn) -> None:
            try:
                results[name] = {"ok": True, "status": 200, **fn()}
            except AuthError as exc:
                results[name] = {"ok": False, "status": 401, "error": str(exc)}
            except VoyagerError as exc:
                results[name] = {"ok": False, "error": str(exc)}

        check("me", lambda: {"member": self.whoami().get("name")})

        def listing() -> dict:
            data = self._request(
                GRAPHQL_URL, params=self._saved_variables(None, page_size)
            )
            return {"saved_ids_seen": len(extract_activity_ids(data))}

        check("saved_posts", listing)
        return results

    def iter_saved_pages(
        self,
        *,
        page_size: int = 50,
        page_delay: float = 2.0,
        max_pages: int | None = None,
    ) -> Iterator[SavedPage]:
        """Yield saved-post pages newest-first, following pagination tokens."""
        pending: list[str | None] = [None]
        used: set[str] = set()
        number = 0
        while pending:
            token = pending.pop(0)
            key = token or ""
            if key in used:
                continue
            used.add(key)

            variables = self._saved_variables(token, page_size)
            data = self._request(GRAPHQL_URL, params=variables)
            number += 1

            records = parse_updates(data)
            ids = extract_activity_ids(data)
            if not ids and records:
                # Schema drift safety net: if the saved-item URNs moved out of
                # the structures we look for but the post bodies still parsed,
                # recover the ids from the parsed records so nothing is silently
                # dropped while sync reports success.
                ids = [
                    pid
                    for parsed in records
                    if (pid := activity_id_from_urn(parsed.get("urn")))
                ]
            tokens = [t for t in extract_pagination_tokens(data) if t not in used]
            page = SavedPage(
                number=number,
                activity_ids=ids,
                records=records,
                tokens=tokens,
                raw=data,
            )
            yield page

            if max_pages is not None and number >= max_pages:
                break
            if not tokens:
                break
            # A page with no posts and no token is the end of the list, even if
            # LinkedIn echoed a fresh token back with it.
            if not page.activity_ids and not page.records:
                break
            pending.extend(tokens)
            if page_delay:
                time.sleep(page_delay)

    def _saved_variables(self, token: str | None, count: int) -> dict:
        inner = (
            f"(flagshipSearchIntent:{SAVED_POSTS_INTENT},"
            "queryParameters:List((key:savedPostType,value:List(ALL))))"
        )
        if token:
            variables = (
                f"(start:0,count:{count},paginationToken:{token},query:{inner})"
            )
        else:
            variables = f"(start:0,count:{count},query:{inner})"
        return {
            "includeWebMetadata": "true",
            "variables": variables,
            "queryId": self.query_id,
        }

    def fetch_post(self, post_id: str) -> dict:
        """Fetch and parse one post, trying every URN type LinkedIn mints."""
        last_error: Exception | None = None
        for urn_type in URN_TYPES:
            urn = f"urn:li:{urn_type}:{post_id}"
            url = FEED_UPDATE_URL.format(urn=quote(urn, safe=""))
            try:
                data = self._request(url)
            except VoyagerError as exc:
                last_error = exc
                continue
            records = parse_updates(data, urn=urn)
            if records:
                parsed = records[0]
                parsed.setdefault("urn", urn)
                return parsed
        if last_error is not None:
            raise last_error
        raise VoyagerError(
            f"no post content could be parsed for activity id {post_id}"
        )
