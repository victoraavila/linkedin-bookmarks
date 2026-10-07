# linkedin-bookmarks

A local, cumulative, searchable archive of your LinkedIn saved posts
(*My Items → Saved posts*), exposed to OpenCode through an MCP server — with a
sync engine so recent saves stay fresh without any official API access.

This is the sibling of [`x-bookmarks`](../x-bookmarks): same shape, same MCP tool
surface, same guarantees. Point an agent at one and it already knows the other.

## Why this shape

- LinkedIn has **no official API for saved posts**. Its Marketing and Talent APIs
  simply do not expose them.
- The official **"Get a copy of your data"** export *does* include a `Saved
  Items` file — but it is a CSV of **URLs and save dates only**: no post text, no
  author, no media. It is a backup of links, not a readable archive.
- The LinkedIn website itself reads saved posts from an internal GraphQL service
  ("Voyager"). Talking to that with your own logged-in cookies — exactly what
  `twscrape` does for X — gives full post content, authors, metrics, media and
  articles, with no per-call cost.
- The official export is therefore **optional enrichment**: it contributes save
  dates and proves a post was saved even if LinkedIn later stops serving it.

```
        voyager (live, cookies)      Saved Items export (optional)
                   │                          │
                   └────────► lbm import ◄────┘
                                  │
                          data/bookmarks.db  (SQLite + FTS5)
                                  │
                     lbm.mcp_server  ──stdio──►  OpenCode
```

## Quick start

From this directory:

```bash
# 1. Save your LinkedIn session (needs li_at and JSESSIONID cookies)
uv run lbm login

# 2. Backfill everything you have saved so far
uv run lbm sync --mode full

# 3. Check what landed
uv run lbm stats
uv run lbm search "pricing"
```

### Getting the cookies

The most reliable way is to copy the **whole `cookie` request header** rather
than individual values, because it cannot be mistyped and it captures every
cookie LinkedIn sends:

1. Log in at linkedin.com and confirm you land on the **feed** (not a login or
   checkpoint page).
2. DevTools (F12) → **Network** → reload → click any request to `linkedin.com`.
3. Under **Request Headers**, find `cookie:`, select the whole value, copy it.
4. Paste it straight in:

```bash
pbpaste | uv run lbm login --stdin
```

**Or skip copying entirely** — read the current cookies straight from your
browser (approve the macOS keychain prompt the first time):

```bash
uv run lbm login --from-browser                  # newest browser/profile
uv run lbm login --from-browser --browser chrome --profile "Profile 2"
```

This is the most reliable option, because browsers can hold several cookies
named `li_at` scoped to different domains (`linkedin.com` vs
`www.linkedin.com`), and hand-copying the wrong one yields a session LinkedIn
rejects as invalid.

The two cookies that actually matter are:

| Cookie | Why |
| --- | --- |
| `li_at` | the session credential |
| `JSESSIONID` | reused as the `csrf-token` header on every Voyager request |

`li_at` and `JSESSIONID` are both required: `JSESSIONID` looks like
`"ajax:7454514581611861831542146"` and the quotes are stripped automatically.

You can also paste the two values by hand (DevTools → Application → Cookies →
`https://www.linkedin.com`):

```bash
uv run lbm login            # prompts for li_at, then JSESSIONID (input is hidden)
```

Other accepted inputs:

```bash
# two lines on stdin: li_at first, then JSESSIONID
printf '%s\n%s\n' "$LI_AT" "$JSESSIONID" | uv run lbm login --stdin

# explicit flags (visible in shell history)
uv run lbm login --li-at "$LI_AT" --jsessionid "$JSESSIONID"

# from a file
uv run lbm login --cookie-file ~/.secrets/linkedin-cookies.txt
```

The session is stored in `data/accounts.db` (mode `600`, gitignored). Cookies
are credentials — they are never written into your OpenCode config.

`lbm login` verifies the session against LinkedIn and **fails loudly** if
LinkedIn rejects it, rather than saving a session that will not sync. Skip that
with `--no-verify`, or check later with `lbm verify`.

## Keeping it fresh

```bash
uv run lbm sync --mode quick     # newest-save-first, stops at the first known run
uv run lbm sync --mode full      # walks the entire saved list
```

`quick` is the everyday command: the saved list is newest-first, so it stops
after `--boundary` (default 25) consecutive posts that are already archived. It
also only fetches full text for posts that are still missing it, so a refresh
usually touches just the handful of posts you saved since last time.

You can also refresh from inside OpenCode — the MCP server exposes
`refresh_bookmarks`:

> "refresh my LinkedIn bookmarks and then show me anything new about pricing"

### Optional: run it on a schedule (macOS)

`scripts/com.linkedinbookmarks.sync.plist` is a launchd template that runs a
quick sync every 6 hours. Install it only if you want unattended syncing:

```bash
sed "s|__PROJECT__|$PWD|g" scripts/com.linkedinbookmarks.sync.plist \
  > ~/Library/LaunchAgents/com.linkedinbookmarks.sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.linkedinbookmarks.sync.plist
```

Remove it with `launchctl bootout gui/$(id -u)/com.linkedinbookmarks.sync`.

Scraping runs against LinkedIn's rate limits and its User Agreement, which
prohibits automated access. The schedule is conservative (one quick sync per 6
hours) and entirely opt-in. See **Account safety** below.

## Two sync engines

The default engine sends the stored cookies with `requests`. It is fast and
light, but LinkedIn sometimes invalidates sessions that make API calls from a
non-browser client — in which case you will see the "session rejected" error
even though you are logged in in your browser.

The **browser engine** drives a real, persistent browser and lets the saved-posts
page fetch its own list, harvesting the responses from the network. It does not
reconstruct LinkedIn's request at all, so it is immune to both the rotating
GraphQL query id and any change to the request shape, and its session is never
rejected the way a non-browser client's is. It is slower and needs a browser, but
it is the durable option.

```bash
# one-time: log in inside the automation browser (a window opens)
uv run lbm login --interactive

# verify from that browser session (checks /me and the saved-posts listing)
uv run lbm verify --engine browser

# sync through the browser — runs hidden by default
uv run lbm sync --mode full --engine browser
uv run lbm sync --mode quick --engine browser    # everyday refresh

# watch it work, if you want to
uv run lbm sync --engine browser --show
```

The browser runs **hidden by default**, so a sync in the background never
intrudes. A window is opened **only when it is actually needed**: `lbm login
--interactive` (once), or a sync when the stored session has expired — it will
open just long enough for you to log in, then go back to hidden. When there is
no terminal to log in from (e.g. called from an agent), it fails fast with "run
lbm login --interactive" instead of hanging.

`--mode full` scrolls the saved-posts page until it stops loading new pages
(up to a safety cap) and harvests everything it loads; `--mode quick` reads only
the top few screens. Progress is printed while it harvests. `--limit N` caps how
many posts are then processed. For a first backfill, `full` is the one to run.

The MCP tools take the same option:
`refresh_bookmarks(mode="quick", engine="browser")` and
`check_session(engine="browser")`.

## Adding the official export (save dates + deleted-post retention)

1. On linkedin.com: **Settings & Privacy → Data privacy → Get a copy of your
   data**. Tick **Saved items** (or take the full archive). LinkedIn emails a
   download link within ~24 hours.
2. Unzip it and import the Saved Items CSV:

```bash
uv run lbm import-export ~/Downloads/Basic_LinkedInDataExport_*/Saved\ Items.csv
uv run lbm stats
```

The importer sniffs the URL and date columns, so it tolerates the export's
changing headers, and accepts JSON too. Importing is **additive**: it fills in
`saved_at`, and because upsert never blanks existing fields, a post whose
content the live sync already captured stays intact.

## MCP tools

Registered in OpenCode as `linkedin-bookmarks`. Tools become
`linkedin-bookmarks_<tool>`, and in Code Mode they are grouped under
`tools["linkedin-bookmarks"]` (bracket notation, because the name contains a
hyphen):

| Tool | Purpose |
| --- | --- |
| `archive_status` | counts, content coverage, freshness, top authors |
| `search_bookmarks` | full-text search, filter by author/label/date |
| `recent_bookmarks` | newest saves first, optional `days` window |
| `get_bookmark` | one saved post by id or URL, with media and links |
| `list_folders` | imported labels and counts (usually empty) |
| `top_authors` | authors you save most |
| `sql_query` | read-only SQL for custom analysis |
| `refresh_bookmarks` | pull new saved posts from LinkedIn (quick/full) |
| `check_session` | verify the stored session without syncing |

## CLI reference

```
lbm init                       create the database
lbm login [--label NAME]       save a LinkedIn session cookie
lbm accounts                   list configured sessions
lbm verify [--label NAME]      check the session against LinkedIn
lbm logout LABEL               remove a stored session
lbm sync --mode quick|full     pull saved posts from LinkedIn
lbm import-export PATH         import the official Saved Items export
lbm search QUERY [--author A] [--label L] [--since D] [--until D]
lbm recent [--days N] [--author A]
lbm get ID_OR_URL
lbm folders | authors | stats
lbm sql "SELECT ..."
lbm doctor                     check archive + retrieval health
lbm reindex                    rebuild the full-text index
```

`sync --mode full` has no page cap and walks until LinkedIn stops returning
pages. `--limit N` sets an approximate cap. Add `--no-content` to record saved
ids without fetching post text. Add `--json` to any read command.

## How retrieval works

Search is **keyword-based**, using SQLite **FTS5** — not semantic/vector search.
It is deliberately identical to `x-bookmarks`.

- **Tokenizer**: `porter unicode61` — case-insensitive, Unicode-aware, with
  English stemming, so `launches` and `launch` are the same term.
- **Matching**: quoted **prefix** terms, so `vector` also matches `vectorize`.
- **Multi-word queries** are precise-first: all terms must match (implicit AND).
  If that returns nothing, the same terms are OR'd and BM25 puts documents
  matching more of them on top.
- **Ranking**: BM25 relevance (`ORDER BY rank`) — *not* recency. For "what's
  new", use `recent_bookmarks` / `lbm recent`.
- **Indexed fields**: post text, author name/handle/headline, expanded links,
  reposted text, and article title/subtitle. Metrics and media are stored but
  not indexed.

**Saved order vs post date.** `recent_bookmarks` orders by the observed
newest-first save position (`sort_index`, 0 = most recently saved), falling back
to the export's save date and then to when the archive first saw the post. This
is *not* the same as when the post was published — LinkedIn lets you save an old
post today. `created_at` is the post's own publication time, decoded exactly
from LinkedIn's snowflake activity id.

**What this is good and bad at**

| Good | Bad |
| --- | --- |
| Exact product/model names, identifiers, handles, URLs | Paraphrase with no shared vocabulary |
| Fast, offline, no API cost | Fuzzy/conceptual similarity |
| Articles and reposted text included | Private/connection-only content the account can't see |

The agent supplies the semantic layer: a question like *"posts about making
agents cheaper"* gets expanded into concrete terms (cost, pricing, KV cache,
quantization) and searched as several keyword queries.

## Checking that retrieval works

```bash
uv run lbm doctor      # health check, exits non-zero on failure
uv run lbm reindex     # rebuild the full-text index if it ever drifts
```

`doctor` verifies the one failure mode that degrades search silently — the FTS
index drifting out of sync with the `posts` table — plus a randomized end-to-end
round-trip: it picks a real post, finds a token unique to it, and confirms the
normal query path retrieves that same post (and the author filter). It also
reports how many saved posts have no text yet, the session state, and freshness.

## How content is preserved

The archive is cumulative and deliberately lossy-proof:

- An incoming record with an empty field **never** overwrites stored content.
- A post that disappears from LinkedIn is **retained**, not deleted.
- A tombstone (unavailable) updates the status but keeps the text, media URLs
  and author you already had.
- A saved post is recorded even when its content fetch fails: the sync writes a
  stub first, so listing and content fetching are independent failure domains.
- `first_seen_at` / `last_seen_at` / `content_updated_at` track observation
  history independently.

## Data, privacy, and layout

```
data/bookmarks.db   archive (SQLite + FTS5)
data/accounts.db    LinkedIn session cookies (li_at + JSESSIONID)  — gitignored, mode 600
```

Everything stays on this machine. Nothing is uploaded. Override locations with
`LB_DATA_DIR`, `LB_DB`, or `LB_ACCOUNTS_DB`. `LB_DATA_DIR` is what the OpenCode
MCP entry sets.

## OpenCode MCP entry

Already added to `~/.config/opencode/opencode.json`:

```jsonc
"mcp": {
  "servers": {
    "linkedin-bookmarks": {
      "type": "local",
      "command": ["/absolute/path/to/linkedin-bookmarks/.venv/bin/python",
                  "-m", "lbm.mcp_server"],
      "cwd": "/absolute/path/to/linkedin-bookmarks",
      "environment": { "LB_DATA_DIR": "/absolute/path/to/linkedin-bookmarks/data" }
    }
  }
}
```

Verify with `opencode mcp list` and look for `linkedin-bookmarks  connected`.

## Account safety

Cookie scraping uses your own session against LinkedIn's internal endpoints. It
does not exploit a vulnerability, but **LinkedIn's User Agreement prohibits
automated access**, and accounts that trip its rate limits can be temporarily
restricted. This tool is built to be conservative:

- a minimum interval between requests (`min_interval` in `lbm/voyager.py`);
- `quick` syncs page only until they hit known posts;
- content is fetched only for posts that are missing it;
- HTTP 429 is backed off with `Retry-After`, and repeated 429s abort rather than
  hammering.

Use it for your own archive, at your own risk, and don't run it unattended at
high frequency.

## Troubleshooting

- **`refresh_bookmarks` says no session configured** → run `lbm login`.
- **`check_session` / sync says the session expired (HTTP 401/403)** → the
  `li_at` cookie ages out after weeks. Run `lbm login --from-browser` again.
- **Session rejected right after a successful login ("redirected the request",
  or LinkedIn sends `li_at=delete me`)** → LinkedIn is invalidating the cookie
  session for non-browser calls. Switch to the browser engine:
  `uv run lbm login --interactive`, then `uv run lbm sync --engine browser`.
- **Listing fails with HTTP 400 from GraphQL (HTTP engine only)** → the
  saved-posts query id rotated. Do not chase it: use the browser engine, which
  harvests the page's own responses (`lbm login --interactive`, then
  `lbm sync --engine browser`). To fix the HTTP engine too, capture the current
  id from DevTools → Network while loading `linkedin.com/my-items/saved-posts/`
  and update `SAVED_POSTS_QUERY_ID` in `lbm/voyager.py`.
- **Rate limited (HTTP 429)** → `quick` mode with a smaller `--boundary`, or wait.
- **Many saved posts show no text** → run `lbm sync --mode full`; content is
  fetched for any post missing it.
- **Search matches nothing that clearly exists** → the FTS index uses prefix
  matching on whole terms; try fewer, more distinctive words.

## Development

```bash
uv run pytest                          # unit + integration tests (no network)
uv run python scripts/mcp_smoke.py     # end-to-end MCP protocol check
```

The tests lock down the parsing contract with synthetic Voyager responses and
cover the cumulative-merge guarantees, pagination, boundary logic and CSV
sniffing. Nothing in the test suite touches the network.

### Reference projects

This was built by adapting ideas from the working ecosystem around LinkedIn
saved posts:

- [`tlmaloney/li-scraper`](https://github.com/tlmaloney/li-scraper) — the
  cookie-based Voyager listing + token pagination recipe this sync engine follows.
- [`mguttmann/linkedin-internal-api`](https://github.com/mguttmann/linkedin-internal-api)
  — documented Voyager auth, endpoints and MCP design.
- [`stickerdaniel/linkedin-mcp-server`](https://github.com/stickerdaniel/linkedin-mcp-server)
  — the browser-session alternative, and a reference for MCP packaging.
- [`kanwia-ai/LinkedIn-Saved-Posts-`](https://github.com/kanwia-ai/LinkedIn-Saved-Posts-)
  and [`BjornMelin/linkedin-saved-posts-ai`](https://github.com/BjornMelin/linkedin-saved-posts-ai)
  — Playwright scraping patterns for the same page.
