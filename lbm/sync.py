"""Incremental sync of saved posts from LinkedIn via the Voyager API.

``sync(mode="full")`` walks the whole saved-posts list (backfill).
``sync(mode="quick")`` stops once it has seen a run of posts that are already
archived, which is how "always fresh" stays cheap: the list is newest-save-first,
so fresh items are at the front.

This is the LinkedIn analogue of the X plugin's ``twscrape`` sync engine, and it
keeps the same guarantees: a saved post is recorded even if content fetching
fails, and content is never overwritten with an empty value.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field

from . import db
from .importers import (
    activity_id_from_urn,
    from_voyager,
    from_voyager_stub,
)
from .voyager import AuthError, VoyagerClient, VoyagerError, parse_cookie_string

DEFAULT_QUICK_BOUNDARY = 25
# `full` means full: 0 disables the page cap and walks until LinkedIn stops
# returning pages.
DEFAULT_FULL_LIMIT = 0
COMMIT_EVERY = 25
PAGE_SIZE = 50
# Hard safety cap on listing pages (~25k saved posts) so a misbehaving endpoint
# that always mints a fresh pagination token cannot paginate forever.
MAX_SYNC_PAGES = 500
# Browser engine: how many scroll rounds to harvest. Full needs enough to reach
# the bottom of a large library; quick only needs the newest slice.
FULL_HARVEST_SCROLLS = 300
QUICK_HARVEST_SCROLLS = 6


class SyncError(RuntimeError):
    """Raised when a sync cannot run (usually: no LinkedIn account configured)."""


@dataclass
class SyncResult:
    mode: str
    seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    enriched: int = 0
    pending_content: int = 0
    content_errors: int = 0
    stopped_reason: str = "exhausted"
    account: str | None = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"mode={self.mode}",
            f"scanned={self.seen}",
            f"added={self.added}",
            f"updated={self.updated}",
            f"enriched={self.enriched}",
            f"unchanged={self.unchanged}",
            f"stopped={self.stopped_reason}",
        ]
        if self.content_errors:
            parts.append(f"content_errors={self.content_errors}")
        if self.account:
            parts.append(f"account={self.account}")
        return " ".join(parts)


# --------------------------------------------------------------------------- #
# account store
# --------------------------------------------------------------------------- #

_ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    label      TEXT PRIMARY KEY,
    cookies    TEXT NOT NULL,
    created_at TEXT,
    last_used  TEXT
);
"""


def _accounts_conn() -> sqlite3.Connection:
    path = db.accounts_db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_ACCOUNTS_SCHEMA)
    # The file holds the li_at credential; keep it owner-only.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def list_accounts() -> list[dict]:
    """Configured sessions without performing any network request."""
    conn = _accounts_conn()
    try:
        rows = conn.execute(
            "SELECT label, created_at, last_used FROM accounts ORDER BY label"
        ).fetchall()
        return [
            {
                "label": row["label"],
                "active": True,
                "created_at": row["created_at"],
                "last_used": row["last_used"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def add_cookie(label: str, cookie_string: str) -> str:
    """Persist a browser cookie string for a label. Needs li_at + JSESSIONID."""
    cookies = parse_cookie_string(cookie_string)
    if "li_at" not in cookies:
        raise SyncError(
            "the cookie string has no 'li_at' value. Paste the cookie *values* "
            "for li_at and JSESSIONID (a bare token is not enough)."
        )
    if not any(k in cookies for k in ("JSESSIONID", "jsessionid")):
        raise SyncError(
            "the cookie string has no 'JSESSIONID' value, which LinkedIn uses as "
            "the CSRF token for every Voyager request."
        )
    label = (label or "main").strip() or "main"
    conn = _accounts_conn()
    try:
        conn.execute(
            "INSERT INTO accounts(label, cookies, created_at, last_used) "
            "VALUES(?, ?, ?, NULL) "
            "ON CONFLICT(label) DO UPDATE SET cookies = excluded.cookies, "
            "created_at = excluded.created_at",
            (label, "; ".join(f"{k}={v}" for k, v in cookies.items()), db.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return label


def remove_account(label: str) -> bool:
    conn = _accounts_conn()
    try:
        cur = conn.execute("DELETE FROM accounts WHERE label = ?", (label,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def load_cookies(label: str | None = None) -> tuple[str, dict[str, str]]:
    """Return ``(label, cookies)`` for the requested or only configured account."""
    conn = _accounts_conn()
    try:
        if label:
            row = conn.execute(
                "SELECT label, cookies FROM accounts WHERE label = ?", (label,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT label, cookies FROM accounts ORDER BY label LIMIT 1"
            ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise SyncError(
            "No LinkedIn session is configured. Run:\n"
            "  lbm login            # paste li_at and JSESSIONID cookies\n"
            "inside the linkedin-bookmarks project to add one."
        )
    return row["label"], parse_cookie_string(row["cookies"])


def _touch_account(label: str) -> None:
    conn = _accounts_conn()
    try:
        conn.execute(
            "UPDATE accounts SET last_used = ? WHERE label = ?",
            (db.now_iso(), label),
        )
        conn.commit()
    finally:
        conn.close()


def verify_account(
    label: str | None = None,
    engine: str = "http",
    headless: bool = True,
    allow_login: bool = False,
) -> dict:
    """Check the stored session against LinkedIn without syncing."""
    if engine == "browser":
        from .browser import BrowserError, BrowserTransport, harvest_saved_pages

        try:
            transport = BrowserTransport(headless=headless)
            transport.start()
            transport.ensure_logged_in(allow_interactive=allow_login)
        except (BrowserError, VoyagerError) as exc:
            transport.close()
            raise SyncError(str(exc)) from exc
        try:
            client = VoyagerClient(transport=transport)
            try:
                member = client.whoami().get("name")
                probe = {"me": {"ok": True, "status": 200, "member": member}}
            except VoyagerError as exc:
                probe = {"me": {"ok": False, "error": str(exc)}}
            try:
                pages = harvest_saved_pages(transport, max_scrolls=1)
                probe["saved_posts"] = {
                    "ok": bool(pages),
                    "status": 200,
                    "saved_ids_seen": sum(len(p.activity_ids) for p in pages),
                }
            except VoyagerError as exc:
                probe["saved_posts"] = {"ok": False, "error": str(exc)}
        finally:
            transport.close()

        me = probe.get("me", {})
        if not me.get("ok"):
            raise SyncError(me.get("error") or "LinkedIn session could not be verified")
        return {
            "label": "browser",
            "name": me.get("member") or "unknown",
            "probe": probe,
        }

    account_label, cookies = load_cookies(label)
    try:
        client = VoyagerClient(cookies)
    except VoyagerError as exc:
        # Surface every session/network problem as one type the CLI and MCP
        # already handle, rather than leaking AuthError as a traceback.
        raise SyncError(str(exc)) from exc

    probe = client.probe()
    me = probe.get("me", {})
    if not me.get("ok"):
        raise SyncError(me.get("error") or "LinkedIn session could not be verified")
    return {
        "label": account_label,
        "name": me.get("member") or "unknown",
        "probe": probe,
    }


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #

def _needs_content(conn: sqlite3.Connection, post_id: str) -> bool:
    row = conn.execute(
        "SELECT full_text, article_title, content_fetched_at FROM posts "
        "WHERE post_id = ?",
        (post_id,),
    ).fetchone()
    if row is None:
        return True
    if row["full_text"] or row["article_title"]:
        return False
    # A post that parsed with no text (image-only) is not retried forever.
    return not row["content_fetched_at"]


def _run(
    mode: str,
    limit: int | None,
    boundary: int,
    fetch_content: bool,
    account_label: str | None,
    progress=None,
    engine: str = "http",
    headless: bool = True,
    allow_login: bool = False,
    harvest_progress=None,
) -> SyncResult:
    if mode not in ("quick", "full"):
        raise SyncError(f"unknown sync mode: {mode!r}")
    if engine not in ("http", "browser"):
        raise SyncError(f"unknown engine: {engine!r}")

    transport = None
    listing_pages = None
    if engine == "browser":
        from .browser import BrowserError, BrowserTransport, harvest_saved_pages

        try:
            transport = BrowserTransport(headless=headless)
            transport.start()
            transport.ensure_logged_in(allow_interactive=allow_login)
        except (BrowserError, VoyagerError) as exc:
            transport.close()
            raise SyncError(str(exc)) from exc
        label = "browser"
        client = VoyagerClient(transport=transport)
        # Let the real page fetch the list itself; we harvest its responses.
        scrolls = QUICK_HARVEST_SCROLLS if mode == "quick" else FULL_HARVEST_SCROLLS
        try:
            listing_pages = harvest_saved_pages(
                transport, max_scrolls=scrolls, progress=harvest_progress
            )
        except VoyagerError as exc:
            transport.close()
            raise SyncError(str(exc)) from exc
        if not listing_pages:
            transport.close()
            raise SyncError(
                "the saved-posts page returned no posts to harvest. Make sure you "
                "are logged in in the browser window and actually have saved posts."
            )
    else:
        label, cookies = load_cookies(account_label)
        try:
            client = VoyagerClient(cookies)
        except AuthError as exc:
            raise SyncError(str(exc)) from exc

    result = SyncResult(mode=mode, account=label)
    conn = db.init()
    max_items = -1 if limit is None or limit <= 0 else limit
    observed = db.now_iso()

    ordered_ids: list[str] = []
    seen_ids: set[str] = set()
    to_enrich: list[str] = []
    known_streak = 0
    pages_seen = 0

    try:
        pages_source = (
            listing_pages
            if listing_pages is not None
            else client.iter_saved_pages(page_size=PAGE_SIZE, max_pages=MAX_SYNC_PAGES)
        )
        for page in pages_source:
            pages_seen += 1
            inline: dict[str, dict] = {}
            for parsed in page.records:
                pid = activity_id_from_urn(parsed.get("urn"))
                if pid and pid not in inline:
                    inline[pid] = parsed

            stop = False
            for pid in page.activity_ids:
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                ordered_ids.append(pid)

                was_known = (
                    conn.execute(
                        "SELECT 1 FROM posts WHERE post_id = ?", (pid,)
                    ).fetchone()
                    is not None
                )

                if pid in inline:
                    rec = from_voyager(inline[pid], observed_at=observed)
                else:
                    rec = from_voyager_stub(pid, observed_at=observed)

                outcome = db.upsert_post(conn, rec)
                result.seen += 1

                if not was_known:
                    result.added += 1
                elif outcome == "updated":
                    result.updated += 1
                else:
                    result.unchanged += 1

                if fetch_content and _needs_content(conn, pid):
                    to_enrich.append(pid)

                if max_items > 0 and result.seen >= max_items:
                    result.stopped_reason = "reached requested limit"
                    stop = True
                    break

                if mode == "quick":
                    known_streak = known_streak + 1 if was_known else 0
                    if known_streak >= boundary:
                        result.stopped_reason = "reached known boundary"
                        stop = True
                        break

            if result.seen % COMMIT_EVERY == 0:
                conn.commit()
                if progress:
                    progress(result)

            if stop:
                break

        if mode == "full" and result.stopped_reason == "exhausted":
            if pages_seen >= MAX_SYNC_PAGES:
                result.stopped_reason = "reached page cap"
                result.errors.append(
                    f"stopped after {MAX_SYNC_PAGES} pages as a safety cap"
                )
            else:
                result.stopped_reason = "pagination ended"

        # Record the observed newest-first save order (0 = most recently saved).
        if ordered_ids:
            db.refresh_save_order(conn, ordered_ids)
        conn.commit()

        # Content enrichment for posts that still have no text.
        if fetch_content:
            result.pending_content = len(to_enrich)
            for index, pid in enumerate(to_enrich, start=1):
                try:
                    parsed = client.fetch_post(pid)
                    rec = from_voyager(parsed, observed_at=observed)
                    db.upsert_post(conn, rec)
                    db.mark_content_fetched(conn, pid)
                    result.enriched += 1
                except AuthError as exc:
                    # Keep everything archived so far, finalize normally, and
                    # report the session problem rather than aborting mid-way.
                    result.errors.append(f"{type(exc).__name__}: {exc}")
                    result.stopped_reason = "error"
                    break
                except VoyagerError as exc:
                    result.content_errors += 1
                    result.errors.append(f"{pid}: {exc}")
                if index % COMMIT_EVERY == 0:
                    conn.commit()
                    if progress:
                        progress(result)

    except VoyagerError as exc:
        result.errors.append(f"{type(exc).__name__}: {exc}")
        result.stopped_reason = "error"
    finally:
        # Finalization must run even when a session error unwound the loop.
        try:
            conn.commit()
            db.set_meta(conn, "last_sync_at", db.now_iso())
            db.set_meta(conn, "last_sync_mode", mode)
            db.set_meta(conn, "last_sync_summary", result.summary())
            if mode == "full" and not result.errors:
                db.set_meta(conn, "last_full_sync_at", db.now_iso())
            conn.commit()
        finally:
            conn.close()
            if transport is not None:
                transport.close()

    if engine != "browser" and (result.added or result.enriched):
        _touch_account(label)
    return result


def sync(
    mode: str = "quick",
    limit: int | None = None,
    boundary: int = DEFAULT_QUICK_BOUNDARY,
    fetch_content: bool = True,
    account: str | None = None,
    progress=None,
    engine: str = "http",
    headless: bool = True,
    allow_login: bool = False,
    harvest_progress=None,
) -> SyncResult:
    """Blocking entry point used by the CLI and the MCP server."""
    effective_limit = limit
    if effective_limit is None and mode == "full":
        effective_limit = DEFAULT_FULL_LIMIT
    return _run(
        mode,
        effective_limit,
        boundary,
        fetch_content,
        account,
        progress,
        engine=engine,
        headless=headless,
        allow_login=allow_login,
        harvest_progress=harvest_progress,
    )
