"""MCP server exposing the LinkedIn saved-posts archive to OpenCode over stdio."""

from __future__ import annotations

try:  # mcp >= 2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _MCPServer
except ImportError:  # pragma: no cover - mcp 1.x fallback
    from mcp.server.fastmcp import FastMCP as _MCPServer

from . import db
from . import query as q
from . import sync as sync_mod

INSTRUCTIONS = """\
This server exposes a local, cumulative archive of the user's LinkedIn saved
posts ("My Items > Saved posts"), kept fresh by a cookie-based sync against
LinkedIn's internal API.

How to use it well:
- Start with `archive_status` to see how many saved posts exist, how many have
  full content, and when they were last synced. If the archive is empty, tell the
  user to run `lbm login` and `lbm sync --mode full` in a terminal inside the
  linkedin-bookmarks project to backfill it.
- Prefer `search_bookmarks` for topic questions ("what did I save about pricing")
  and `recent_bookmarks` for "what have I saved lately" questions. Saved posts
  are ordered newest-save-first, which is not the same as newest-posted.
- Always include the `url` of each saved post you mention so the user can open the
  original. Do not truncate `full_text` when quoting.
- LinkedIn has no saved-post folders. `list_folders` reports any labels imported
  from the official export; expect it to be empty otherwise.
- A saved post may be present with no text yet if content fetching has not run;
  `archive_status.with_content` vs `.total` shows the gap. Call
  `refresh_bookmarks` (mode="quick") to pull and enrich new saves.
- LinkedIn posts started mixing in articles and reposts, so results can include
  `post_type` of "post" or "article" and a `reposted_full_text` for reshares.
"""

mcp = _MCPServer("linkedin-bookmarks", instructions=INSTRUCTIONS)


@mcp.tool()
def archive_status() -> dict:
    """Counts, coverage, sync freshness and top authors for the archive."""
    stats = q.stats()
    if not stats["total"]:
        stats["hint"] = (
            "Archive is empty. Run `lbm login` then `lbm sync --mode full` in a "
            "terminal inside the linkedin-bookmarks project to backfill it."
        )
    return stats


@mcp.tool()
def search_bookmarks(
    query: str,
    limit: int = 25,
    author: str | None = None,
    label: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Full-text search over saved LinkedIn posts.

    Args:
        query: Free-text terms; all terms must match (prefix matching).
        limit: Maximum results (1-200).
        author: Restrict to an author's public handle, with or without a leading @.
        label: Restrict to an imported label/tag name.
        since: Inclusive ISO timestamp lower bound on the post's created_at.
        until: Inclusive ISO timestamp upper bound on the post's created_at.
    """
    return q.search_bookmarks(query, limit, author, label, since, until)


@mcp.tool()
def recent_bookmarks(
    limit: int = 25,
    days: int | None = None,
    author: str | None = None,
    label: str | None = None,
) -> list[dict]:
    """Most recently saved posts, newest save first.

    Args:
        limit: Maximum results (1-200).
        days: Only include posts saved (or first archived) within the last N days.
        author: Restrict to an author's public handle, with or without a leading @.
        label: Restrict to an imported label/tag name.
    """
    return q.recent_bookmarks(limit, days, author, label)


@mcp.tool()
def get_bookmark(post_id_or_url: str) -> dict:
    """Fetch one saved post by activity id or full LinkedIn URL, with media and links."""
    found = q.get_bookmark(post_id_or_url)
    if found is None:
        return {"error": "not found in archive", "query": post_id_or_url}
    return found


@mcp.tool()
def list_folders() -> list[dict]:
    """List imported labels with their post counts.

    LinkedIn itself has no saved-post folders; this is normally empty unless the
    official export carried labels.
    """
    return q.list_folders()


@mcp.tool()
def top_authors(limit: int = 25, min_bookmarks: int = 2) -> list[dict]:
    """Authors you save the most, ranked by saved-post count."""
    return q.authors(limit, min_bookmarks)


@mcp.tool()
def sql_query(sql: str, limit: int = 200) -> list[dict]:
    """Run a read-only SELECT against the archive for custom analysis.

    Tables: posts(post_id, url, urn, full_text, created_at, saved_at, status,
    post_type, is_repost, author_name, author_handle, author_headline, likes,
    comments, reposts, article_title, article_subtitle, article_url,
    reposted_full_text, reposted_from_handle, media_json, links_json, origin,
    first_seen_at, last_seen_at, content_updated_at, sort_index, ...),
    labels(id, name), post_labels(post_id, label_id), meta(key, value).
    `sort_index` is the newest-first saved position (0 = most recently saved).
    """
    return q.run_sql(sql, limit)


@mcp.tool()
def refresh_bookmarks(
    mode: str = "quick",
    limit: int | None = None,
    fetch_content: bool = True,
    engine: str = "http",
) -> dict:
    """Pull new saved posts from LinkedIn into the archive.

    Args:
        mode: "quick" stops at the first run of already-archived posts; "full"
            walks the entire saved-posts list.
        limit: Optional cap on how many saved posts to scan.
        fetch_content: Whether to fetch full post text for posts missing it.
        engine: "http" uses stored cookies; "browser" drives a real browser
            session (slower, but survives LinkedIn invalidating cookie sessions).
    """
    try:
        result = sync_mod.sync(
            mode=mode, limit=limit, fetch_content=fetch_content, engine=engine
        )
    except sync_mod.SyncError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": not result.errors,
        "mode": result.mode,
        "scanned": result.seen,
        "added": result.added,
        "updated": result.updated,
        "enriched": result.enriched,
        "unchanged": result.unchanged,
        "stopped": result.stopped_reason,
        "account": result.account,
        "errors": result.errors,
        "archive_total": db.total_posts(),
    }


@mcp.tool()
def check_session(engine: str = "http") -> dict:
    """Verify the stored LinkedIn session is still valid, without syncing.

    Args:
        engine: "http" checks the stored cookie session; "browser" checks the
            persistent browser session.
    """
    try:
        return {"ok": True, **sync_mod.verify_account(engine=engine)}
    except sync_mod.SyncError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    db.init().close()
    mcp.run()


if __name__ == "__main__":
    main()
