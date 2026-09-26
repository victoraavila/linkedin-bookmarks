"""Normalize saved-post data from the two supported sources into one record shape.

Sources:
  * voyager          - live cookie-based scraping of LinkedIn's internal API
                       (backfill + incremental refresh)
  * linkedin_export  - the official "Get a copy of your data" Saved Items CSV
                       (URLs + save dates only; enrichment for deleted posts)

Every normalized record is a flat dict whose keys match ``lbm.db.ALL_FIELDS``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

# Provenance markers. A post seen through both sources records "linkedin_export+voyager".
ORIGIN_VOYAGER = "voyager"
ORIGIN_EXPORT = "linkedin_export"

# LinkedIn mints activity ids as snowflake-style 64-bit values whose top 41 bits
# are a Unix millisecond timestamp; ``id >> 22`` recovers it. Verified against
# LinkedIn's own 2018 example (urn:li:activity:6422861848709726208 -> 2018-07-11).
_SNOWFLAKE_SHIFT = 22
# Only trust a decoded timestamp inside a plausible window for LinkedIn content.
_MIN_DECODED_MS = int(datetime(2003, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

_ACTIVITY_RE = re.compile(r"urn:li:(?:activity|share|ugcPost):(\d+)")
_ACTIVITY_SLUG_RE = re.compile(r"-activity-(\d+)")
_URN_RE = re.compile(r"urn:li:[A-Za-z_]+:[^\s\"',)]+")


def _iso(value: Any) -> str | None:
    """Best-effort ISO-8601 with timezone, accepting datetimes, epochs and strings."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if isinstance(value, (int, float)):
        return _epoch_to_iso(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        normalized = cleaned.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%m/%d/%Y, %I:%M %p",
                "%m/%d/%Y %H:%M",
                "%B %d, %Y",
                "%b %d, %Y",
                "%Y-%m-%d",
            ):
                try:
                    dt = datetime.strptime(cleaned, fmt)
                    break
                except ValueError:
                    continue
            else:
                # An unrecognised date string is dropped rather than stored raw,
                # because a non-ISO value would break lexical since/until
                # comparisons and by_month grouping downstream.
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return None


def _epoch_to_iso(value: int | float) -> str | None:
    """Accept seconds, milliseconds or microseconds and normalize to ISO."""
    magnitude = abs(float(value))
    if magnitude > 1e14:  # microseconds
        seconds = value / 1_000_000
    elif magnitude > 1e11:  # milliseconds
        seconds = value / 1_000
    else:  # seconds
        seconds = value
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def snowflake_to_iso(post_id: str | int | None) -> str | None:
    """Decode a LinkedIn activity id into the post's creation time, if plausible."""
    if post_id is None:
        return None
    try:
        raw = int(str(post_id).strip())
    except (TypeError, ValueError):
        return None
    ms = raw >> _SNOWFLAKE_SHIFT
    if ms < _MIN_DECODED_MS:
        return None
    return _epoch_to_iso(ms)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False) if value else None
    return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        match = re.match(r"^([\d.]+)\s*([KkMm])?\+?$", cleaned)
        if match:
            number = float(match.group(1))
            suffix = (match.group(2) or "").lower()
            if suffix == "k":
                number *= 1_000
            elif suffix == "m":
                number *= 1_000_000
            return int(number)
        cleaned = re.sub(r"[^\d-]", "", cleaned)
        if not cleaned:
            return None
        value = cleaned
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def activity_id_from_urn(urn: str | None) -> str | None:
    match = _ACTIVITY_RE.search(urn or "")
    return match.group(1) if match else None


def activity_id_from_url(url: str | None) -> str | None:
    text = url or ""
    match = _ACTIVITY_RE.search(text) or _ACTIVITY_SLUG_RE.search(text)
    return match.group(1) if match else None


def urn_from_url(url: str | None) -> str | None:
    match = _URN_RE.search(url or "")
    return match.group(0) if match else None


def post_url(urn: str | None, post_id: str | None, handle: str | None = None) -> str:
    if urn:
        return f"https://www.linkedin.com/feed/update/{urn}/"
    if post_id and handle:
        return f"https://www.linkedin.com/posts/{handle}-activity-{post_id}"
    if post_id:
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id}/"
    return ""


def _fallback_id(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"url:{digest}"


# --------------------------------------------------------------------------- #
# voyager
# --------------------------------------------------------------------------- #

def from_voyager(parsed: dict, observed_at: str | None = None) -> dict:
    """Normalize a flat dict produced by ``lbm.voyager`` into an archive record."""
    urn = _str_or_none(parsed.get("urn"))
    post_id = activity_id_from_urn(urn) or _str_or_none(parsed.get("post_id"))
    if not post_id:
        raise ValueError("voyager record is missing an activity id")

    handle = parsed.get("author_handle")
    created_at = _iso(parsed.get("created_at")) or snowflake_to_iso(post_id)
    links = parsed.get("links") or []
    hashtags = parsed.get("hashtags") or []
    mentions = parsed.get("mentions") or []

    return {
        "post_id": post_id,
        "url": parsed.get("url") or post_url(urn, post_id, handle),
        "urn": urn or f"urn:li:activity:{post_id}",
        "full_text": parsed.get("full_text") or None,
        "lang": parsed.get("lang"),
        "created_at": created_at,
        "status": "available",
        "unavailable_reason": None,
        "post_type": parsed.get("post_type") or "post",
        "is_repost": _int(parsed.get("is_repost")),
        "reposted_from_name": parsed.get("reposted_from_name"),
        "reposted_from_handle": parsed.get("reposted_from_handle"),
        "reposted_full_text": parsed.get("reposted_full_text") or None,
        "author_id": _str_or_none(parsed.get("author_id")),
        "author_name": parsed.get("author_name"),
        "author_handle": handle,
        "author_headline": parsed.get("author_headline"),
        "author_followers": _int(parsed.get("author_followers")),
        "author_profile_image": parsed.get("author_profile_image"),
        "likes": _int(parsed.get("likes")),
        "comments": _int(parsed.get("comments")),
        "reposts": _int(parsed.get("reposts")),
        "article_title": parsed.get("article_title"),
        "article_subtitle": parsed.get("article_subtitle"),
        "article_url": parsed.get("article_url"),
        "media_json": _json(parsed.get("media")),
        "entities_json": _json(
            {"hashtags": hashtags, "mentions": mentions}
        ),
        "hashtags_json": _json(hashtags),
        "mentions_json": _json(mentions),
        "links_json": _json(links),
        "raw_json": _json(parsed.get("raw")),
        "saved_at": _iso(parsed.get("saved_at")),
        "sort_index": _int(parsed.get("sort_index")),
        "origin": ORIGIN_VOYAGER,
        "last_seen_at": observed_at,
    }


def from_voyager_stub(post_id: str, sort_index: int | None = None, observed_at: str | None = None) -> dict:
    """Minimal record for a saved post discovered before its content is fetched.

    The post is real and its position in the saved list is known; the content is
    filled in later by ``sync``. This guarantees a saved post is never lost just
    because content scraping failed.
    """
    if not post_id:
        raise ValueError("stub record is missing a post_id")
    return {
        "post_id": post_id,
        "urn": f"urn:li:activity:{post_id}",
        "url": post_url(f"urn:li:activity:{post_id}", post_id),
        "created_at": snowflake_to_iso(post_id),
        "status": "available",
        "post_type": "post",
        "sort_index": sort_index,
        "origin": ORIGIN_VOYAGER,
        "last_seen_at": observed_at,
    }


def from_tombstone(
    post_id: str,
    reason: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """A saved post that LinkedIn no longer serves. Keeps any stored content."""
    return {
        "post_id": post_id,
        "status": "unavailable",
        "unavailable_reason": reason or "post unavailable",
        "origin": ORIGIN_VOYAGER,
        "last_seen_at": observed_at,
    }


# --------------------------------------------------------------------------- #
# official export
# --------------------------------------------------------------------------- #

def from_export_row(
    url: str,
    saved_at: Any = None,
    kind: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Normalize one row of LinkedIn's official Saved Items export.

    The export carries only a URL and a save date, so the record is sparse by
    design: ``upsert_post`` merges it into whatever the live sync already has
    (or will later fill in). This is the analogue of xarchive for X.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("export row is missing a URL")

    post_id = activity_id_from_url(url) or _fallback_id(url)
    urn = urn_from_url(url) or (f"urn:li:activity:{post_id}" if post_id.isdigit() else None)

    return {
        "post_id": post_id,
        "url": url,
        "urn": urn,
        "created_at": snowflake_to_iso(post_id) if post_id.isdigit() else None,
        "status": "available",
        "post_type": kind or "post",
        "saved_at": _iso(saved_at),
        "origin": ORIGIN_EXPORT,
        "last_seen_at": observed_at,
    }
