"""Command line interface: ``lbm <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db
from . import ingest
from . import query as q
from . import sync as sync_mod
from .voyager import parse_cookie_string


def _print_json(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _format_bookmark(item: dict, *, indent: str = "  ") -> str:
    lines = []
    stamp = (item.get("created_at") or item.get("saved_at") or item.get("first_seen_at") or "")[:10]
    handle = item.get("author_handle") or "?"
    name = item.get("author_name") or ""
    status = item.get("status") or "available"
    marker = "" if status == "available" else f"  [{status}]"
    kind = item.get("post_type") or "post"
    lines.append(f"{stamp}  @{handle} ({name})  {kind}{marker}")
    text = (item.get("full_text") or "").strip()
    if text:
        lines.append(f"{indent}{text}")
    if item.get("reposted_full_text"):
        lines.append(
            f"{indent}reposted @{item.get('reposted_from_handle') or '?'}: "
            f"{item['reposted_full_text']}"
        )
    if item.get("article_title"):
        lines.append(f"{indent}article: {item['article_title']}")
    metrics = [
        f"{k}={item[k]}"
        for k in ("likes", "comments", "reposts")
        if item.get(k) is not None
    ]
    if metrics:
        lines.append(f"{indent}{' '.join(metrics)}")
    if item.get("labels"):
        lines.append(f"{indent}labels: {', '.join(item['labels'])}")
    if item.get("url"):
        lines.append(f"{indent}{item['url']}")
    return "\n".join(lines)


def cmd_init(args) -> int:
    db.init().close()
    print(f"initialized {db.db_path()}")
    return 0


def _clean(value: str) -> str:
    return value.strip().strip('"').strip("'").strip()


def _has_session_cookies(text: str) -> bool:
    cookies = parse_cookie_string(text)
    return "li_at" in cookies and any(k in cookies for k in ("JSESSIONID", "jsessionid"))


def _build_cookie_string(li_at: str, jsessionid: str) -> str | None:
    parts = []
    for name, value in (("li_at", li_at), ("JSESSIONID", jsessionid)):
        cleaned = _clean(value)
        if not cleaned:
            print(f"{name} cannot be empty", file=sys.stderr)
            return None
        parts.append(f"{name}={cleaned}")
    return "; ".join(parts)


_CAPTURE_HELP = """\
To capture a session that LinkedIn will accept, use the full request header —
it avoids copying an individual cookie value wrong:

  1. Log in at https://www.linkedin.com in your browser and confirm you land on
     the feed (not a login or checkpoint page).
  2. DevTools (F12) -> Network -> reload -> click any request to linkedin.com.
  3. Under Request Headers, find `cookie:`, then Copy value (or select the whole
     value and copy).
  4. Run, pasting it (the tool picks out li_at and JSESSIONID automatically):

       pbpaste | uv run lbm login --stdin

     or:  uv run lbm login --cookie-file <file-with-the-header>
"""


def _interactive_login() -> int:
    """Open a real browser, let the user log in, then verify from that session."""
    from . import browser as browser_mod
    from .voyager import VoyagerClient

    print(
        "Opening a browser window. Log in to LinkedIn normally (2FA included).\n"
        "The window stays open once you reach the feed, then verifies the session.\n"
    )
    try:
        transport = browser_mod.BrowserTransport(headless=False)
        transport.login_interactive()
    except browser_mod.BrowserError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - user-facing cancellation/timeout
        print(f"login did not complete: {exc}", file=sys.stderr)
        return 1

    from .browser import harvest_saved_pages

    try:
        try:
            member = VoyagerClient(transport=transport).whoami().get("name")
            probe = {"me": {"ok": True, "status": 200, "member": member}}
        except Exception as exc:  # noqa: BLE001
            probe = {"me": {"ok": False, "error": f"{type(exc).__name__}: {exc}"}}
        try:
            pages = harvest_saved_pages(transport, max_scrolls=1)
            probe["saved_posts"] = {
                "ok": bool(pages),
                "status": 200,
                "saved_ids_seen": sum(len(p.activity_ids) for p in pages),
            }
        except Exception as exc:  # noqa: BLE001
            probe["saved_posts"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        transport.close()

    for name, result in probe.items():
        state = "PASS" if result.get("ok") else "FAIL"
        detail = result.get("error") or {
            k: v for k, v in result.items() if k not in ("ok", "status")
        }
        print(f"  {state:<4}  {name:<12} {detail}")

    me_ok = probe.get("me", {}).get("ok", False)
    listing_ok = probe.get("saved_posts", {}).get("ok", False)
    if me_ok and listing_ok:
        print("\nsession ready — run: lbm sync --mode full --engine browser")
        return 0
    if me_ok and not listing_ok:
        print(
            "\nlogged in, but no saved posts were harvested. If you do have saved "
            "posts, the page layout may have changed — send me this output.",
            file=sys.stderr,
        )
    return 1


def cmd_login(args) -> int:
    cookies: str | None = None

    if getattr(args, "interactive", False):
        return _interactive_login()

    if getattr(args, "from_browser", False):
        from . import browser as browser_mod

        try:
            found = browser_mod.cookies_from_browser(args.browser, args.profile)
        except browser_mod.BrowserCookieError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        cookies = "; ".join(f"{k}={v}" for k, v in found.items())
        print(f"read {len(found)} LinkedIn cookies from your browser")
    elif args.cookie_file:
        path = Path(args.cookie_file).expanduser()
        if not path.exists():
            print(f"cookie file not found: {path}", file=sys.stderr)
            return 2
        cookies = path.read_text(encoding="utf-8")
    elif args.cookie:
        cookies = args.cookie
    elif args.stdin:
        cookies = sys.stdin.read()
    elif args.li_at or args.jsessionid:
        if not (args.li_at and args.jsessionid):
            print("--li-at and --jsessionid must be provided together", file=sys.stderr)
            return 2
        cookies = _build_cookie_string(args.li_at, args.jsessionid)

    if cookies is not None and not _has_session_cookies(cookies):
        # Allow "li_at\nJSESSIONID" piped on two lines.
        values = [_clean(line) for line in cookies.splitlines() if line.strip()]
        if len(values) >= 2:
            cookies = _build_cookie_string(values[0], values[1])

    if cookies is None:
        print(
            "Paste your LinkedIn cookies. Easiest and least error-prone: the full\n"
            "request `cookie:` header from DevTools -> Network (any linkedin.com\n"
            "request) -> Request Headers, piped in with `pbpaste | lbm login`.\n"
            "Otherwise paste the li_at value, then the JSESSIONID value.\n"
            f"Values are stored only in {db.accounts_db_path()}\n"
        )
        try:
            li_at = _clean(input("li_at>       "))
            if _has_session_cookies(li_at):
                cookies = li_at
            else:
                jsessionid = input("JSESSIONID>  ")
                cookies = _build_cookie_string(li_at, jsessionid)
        except (EOFError, KeyboardInterrupt):
            print("\nlogin cancelled", file=sys.stderr)
            return 2

    if not cookies:
        print("no cookies provided", file=sys.stderr)
        return 2
    if not _has_session_cookies(cookies):
        print(
            "could not find both li_at and JSESSIONID in the input.\n"
            "Paste the cookie *values* (or a full 'li_at=...; JSESSIONID=...' "
            "string), not a bare token.",
            file=sys.stderr,
        )
        return 2

    try:
        label = sync_mod.add_cookie(args.label, cookies)
    except sync_mod.SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"saved LinkedIn session as '{label}' in {db.accounts_db_path()}")

    if not args.no_verify:
        try:
            info = sync_mod.verify_account(label)
        except sync_mod.SyncError as exc:
            print(
                f"\nLinkedIn REJECTED this session, so it will not sync:\n  {exc}\n",
                file=sys.stderr,
            )
            print(_CAPTURE_HELP, file=sys.stderr)
            return 1
        probe = info.get("probe") or {}
        if not probe.get("saved_posts", {}).get("ok", True):
            print(
                "the session works but the saved-posts listing failed; the "
                "GraphQL query id may have rotated (see README troubleshooting).",
                file=sys.stderr,
            )
            return 1
        print(f"session verified for {info.get('name') or 'unknown member'}")

    print("next: lbm sync --mode full")
    return 0


def cmd_accounts(args) -> int:
    accounts = sync_mod.list_accounts()
    if args.json:
        _print_json(accounts)
    elif not accounts:
        print("no accounts configured (run: lbm login)")
    else:
        for account in accounts:
            print(
                f"{account['label']}  created={account['created_at'] or '-'}  "
                f"last_used={account['last_used'] or 'never'}"
            )
    return 0


def cmd_verify(args) -> int:
    try:
        info = sync_mod.verify_account(
            args.label, engine=args.engine, headless=not args.show, allow_login=True
        )
    except sync_mod.SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    probe = info.get("probe") or {}
    listing_ok = probe.get("saved_posts", {}).get("ok", True)
    if args.json:
        _print_json(info)
    else:
        print(f"session OK: {info.get('name')} ({info.get('label')})")
        for name, result in probe.items():
            state = "PASS" if result.get("ok") else "FAIL"
            detail = result.get("error") or {
                k: v for k, v in result.items() if k not in ("ok", "status")
            }
            print(f"  {state:<4}  {name:<12} {detail}")
        if not listing_ok:
            print(
                "\nthe session works but the saved-posts listing failed; the "
                "GraphQL query id may have rotated (see README troubleshooting).",
                file=sys.stderr,
            )
    return 0 if listing_ok else 1


def cmd_logout(args) -> int:
    if sync_mod.remove_account(args.label):
        print(f"removed session '{args.label}'")
        return 0
    print(f"no session named '{args.label}'", file=sys.stderr)
    return 1


def cmd_sync(args) -> int:
    state = {"scanned": False}

    def progress(result) -> None:
        if not state["scanned"]:
            # Leave the harvest progress line behind before the scan line starts,
            # otherwise the two overwrite each other on one terminal row.
            print("", file=sys.stderr)
            state["scanned"] = True
        detail = (
            f"enriched={result.enriched}/{result.pending_content}"
            if result.pending_content
            else f"enriched={result.enriched}"
        )
        print(
            f"\rscanning... seen={result.seen} added={result.added} {detail}",
            end="",
            file=sys.stderr,
            flush=True,
        )

    def harvest_progress(captured: int) -> None:
        print(
            f"\rloading saved posts from LinkedIn... {captured} page(s) captured",
            end="",
            file=sys.stderr,
            flush=True,
        )

    try:
        result = sync_mod.sync(
            mode=args.mode,
            limit=args.limit,
            boundary=args.boundary,
            fetch_content=not args.no_content,
            account=args.account,
            progress=None if args.json else progress,
            engine=args.engine,
            headless=not args.show,
            allow_login=True,
            harvest_progress=None if args.json else harvest_progress,
        )
    except sync_mod.SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.json:
        print("", file=sys.stderr)
        for error in result.errors[:20]:
            print(f"error: {error}", file=sys.stderr)

    if args.json:
        _print_json(result.__dict__)
    else:
        print(result.summary())
        total = db.total_posts()
        print(f"archive now holds {total} saved posts")
    return 1 if result.errors else 0


def cmd_import(args) -> int:
    try:
        summary = ingest.import_export(args.path)
    except ingest.ImportError_ as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(summary)
    else:
        extras = f" ({len(summary['errors'])} skipped)" if summary["errors"] else ""
        print(
            f"imported from {summary['file']}: "
            f"{summary['added']} added, {summary['updated']} refreshed, "
            f"{summary['unchanged']} unchanged{extras}"
        )
        if summary.get("date_column"):
            print(
                f"saved dates: {summary.get('earliest_saved') or '?'} .. "
                f"{summary.get('latest_saved') or '?'}"
            )
        for error in summary["errors"][:10]:
            print(f"  skipped: {error}", file=sys.stderr)
    return 0


def cmd_search(args) -> int:
    try:
        results = q.search_bookmarks(
            args.query, args.limit, args.author, args.label, args.since, args.until
        )
    except ValueError as exc:
        print(f"invalid query: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(results)
    elif not results:
        print("no matches")
    else:
        print(f"{len(results)} match(es):\n")
        for item in results:
            print(_format_bookmark(item))
            print()
    return 0


def cmd_recent(args) -> int:
    results = q.recent_bookmarks(args.limit, args.days, args.author, args.label)
    if args.json:
        _print_json(results)
    elif not results:
        print("archive is empty")
    else:
        for item in results:
            print(_format_bookmark(item))
            print()
    return 0


def cmd_get(args) -> int:
    found = q.get_bookmark(args.post)
    if found is None:
        print("not found in archive", file=sys.stderr)
        return 1
    _print_json(found) if args.json else print(_format_bookmark(found))
    return 0


def cmd_folders(args) -> int:
    labels = q.list_folders()
    if args.json:
        _print_json(labels)
    elif not labels:
        print("no labels (LinkedIn has no saved-post folders; import the official export to get any)")
    else:
        for label in labels:
            print(f"{label['bookmarks']:>6}  {label['name']}")
    return 0


def cmd_authors(args) -> int:
    authors = q.authors(args.limit, args.min_bookmarks)
    if args.json:
        _print_json(authors)
    elif not authors:
        print("no authors yet")
    else:
        for author in authors:
            print(f"{author['bookmarks']:>6}  @{author['author_handle']} ({author['author_name']})")
    return 0


def cmd_stats(args) -> int:
    stats = q.stats()
    if args.json:
        _print_json(stats)
    else:
        print(
            f"saved posts: {stats['total']} ({stats['available']} available, "
            f"{stats['unavailable']} unavailable, {stats['with_content']} with text)"
        )
        print(f"labels     : {stats['labels']}")
        print(f"newest     : {stats['newest'] or 'n/a'}")
        print(f"last sync  : {stats['last_sync_at'] or 'never'} ({stats['last_sync_mode'] or '-'})")
        print(f"full sync  : {stats['last_full_sync_at'] or 'never'}")
        print(f"export     : {stats['last_export_import_at'] or 'never'}")
        if stats["by_origin"]:
            print("origins    : " + ", ".join(f"{r['origin']}={r['count']}" for r in stats["by_origin"]))
        if stats["top_authors"]:
            print("top authors:")
            for author in stats["top_authors"]:
                print(f"  {author['bookmarks']:>5}  @{author['author_handle']}")
    return 0


def cmd_sql(args) -> int:
    try:
        rows = q.run_sql(args.sql, args.limit)
    except Exception as exc:  # noqa: BLE001
        print(f"query failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(rows)
    else:
        for row in rows:
            print(row)
    return 0


def cmd_doctor(args) -> int:
    conn = db.init()
    try:
        info = db.counts(conn)
        fts = db.fts_counts(conn)
    finally:
        conn.close()

    failures = 0
    warnings = 0

    def report(label: str, state: str, detail: str) -> None:
        nonlocal failures, warnings
        if state == "fail":
            failures += 1
        elif state == "warn":
            warnings += 1
        print(f"{state.upper():<4}  {label:<13} {detail}")

    print(
        f"archive       {info['total']} saved posts "
        f"({info['available']} available, {info['with_content']} with text)"
    )
    report(
        "search index",
        "pass" if fts["in_sync"] else "fail",
        f"indexed={fts['indexed']} orphaned={fts['orphaned']} missing={fts['missing']}"
        + ("" if fts["in_sync"] else "  (run: lbm reindex)"),
    )

    if info["total"] and info["with_content"] < info["total"]:
        report(
            "content",
            "warn",
            f"{info['total'] - info['with_content']} post(s) have no text yet "
            "(run: lbm sync --mode full)",
        )
    else:
        report("content", "pass", f"{info['with_content']}/{info['total']} posts have text")

    test = q.self_test()
    report(
        "retrieval",
        "pass" if test.get("ok") else "fail",
        test.get("reason")
        or (
            f"token={test['token']!r} -> {test['hits']} hit(s), found={test['found']}, "
            f"author_filter={test['author_filter_ok']}"
        ),
    )

    try:
        session = sync_mod.verify_account()
        report("li session", "pass", session.get("name") or session.get("label") or "ok")
    except sync_mod.SyncError as exc:
        first_line = str(exc).splitlines()[0]
        report("li session", "warn", first_line)

    stats = q.stats()
    report(
        "freshness",
        "pass" if stats["last_sync_at"] else "warn",
        f"last sync {stats['last_sync_at'] or 'never'} "
        f"(mode {stats['last_sync_mode'] or '-'})",
    )

    if args.json:
        _print_json(
            {
                "counts": info,
                "fts": fts,
                "self_test": test,
                "last_sync_at": stats["last_sync_at"],
                "failures": failures,
                "warnings": warnings,
            }
        )
    else:
        print(f"\n{failures} failure(s), {warnings} warning(s)")
    return 1 if failures else 0


def cmd_reindex(args) -> int:
    conn = db.init()
    try:
        indexed = db.reindex(conn)
        fts = db.fts_counts(conn)
    finally:
        conn.close()
    if args.json:
        _print_json({"indexed": indexed, **fts})
    else:
        print(f"reindexed {indexed} saved posts; in_sync={fts['in_sync']}")
    return 0 if fts["in_sync"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lbm", description="Local, searchable archive of your LinkedIn saved posts"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the archive database")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("login", help="save a LinkedIn session cookie for scraping")
    p.add_argument("--label", default="main", help="local name for the session")
    p.add_argument("--from-browser", action="store_true",
                   help="read cookies from a local browser instead of pasting them")
    p.add_argument("--interactive", action="store_true",
                   help="log in by opening a real browser window (browser engine)")
    p.add_argument("--browser", help="browser to read from (default: most recent)")
    p.add_argument("--profile", help="browser profile directory (Chromium family)")
    p.add_argument("--stdin", action="store_true", help="read cookies from stdin")
    p.add_argument("--cookie", help="full cookie string (visible in shell history)")
    p.add_argument("--cookie-file", help="read the cookie string from a file")
    p.add_argument("--li-at", help="li_at value only")
    p.add_argument("--jsessionid", help="JSESSIONID value only")
    p.add_argument("--no-verify", action="store_true", help="skip the live session check")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("accounts", help="list configured LinkedIn sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("verify", help="check the stored session against LinkedIn")
    p.add_argument("--label")
    p.add_argument("--engine", choices=["http", "browser"], default="http")
    p.add_argument("--show", action="store_true",
                   help="show the browser window (hidden by default)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("logout", help="remove a stored session")
    p.add_argument("label")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("sync", help="pull saved posts from LinkedIn")
    p.add_argument("--mode", choices=["quick", "full"], default="quick")
    p.add_argument("--limit", type=int, default=None, help="cap how many to scan")
    p.add_argument("--boundary", type=int, default=sync_mod.DEFAULT_QUICK_BOUNDARY,
                   help="quick mode: stop after this many consecutive known posts")
    p.add_argument("--account", help="session label to use (default: first)")
    p.add_argument("--engine", choices=["http", "browser"], default="http",
                   help="http uses stored cookies; browser drives a real browser session")
    p.add_argument("--show", action="store_true",
                   help="browser engine: show the browser window (hidden by default)")
    p.add_argument("--no-content", action="store_true",
                   help="skip fetching full post text (only list saved ids)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("import-export", help="import the official Saved Items export")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("search", help="full-text search the archive")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--author")
    p.add_argument("--label")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("recent", help="newest saves first")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--days", type=int)
    p.add_argument("--author")
    p.add_argument("--label")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_recent)

    p = sub.add_parser("get", help="show one saved post by id or URL")
    p.add_argument("post")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("folders", help="list imported labels")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_folders)

    p = sub.add_parser("authors", help="authors ranked by saved posts")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--min-bookmarks", type=int, default=2)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_authors)

    p = sub.add_parser("stats", help="archive coverage and freshness")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("sql", help="run a read-only SQL query")
    p.add_argument("sql")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("doctor", help="check archive and retrieval health")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("reindex", help="rebuild the full-text search index")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_reindex)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
