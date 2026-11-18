"""Collector: pulls recent and top media for a curated hashtag list via the Instagram Graph API.

Uses only documented endpoints available to a business/creator account:
  GET /ig_hashtag_search?user_id=...&q=...
  GET /{hashtag-id}/recent_media?user_id=...&fields=...
  GET /{hashtag-id}/top_media?user_id=...&fields=...

Instagram allows 30 unique hashtags per account per rolling 7 days. We record every
lookup locally and refuse to exceed the cap, rather than letting the API reject us.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .graph import GraphClient, GraphError
from .normalize import disclosure_markers, normalize

HASHTAG_CAP = 30
CAP_WINDOW = timedelta(days=7)
MEDIA_FIELDS = "id,caption,media_type,comments_count,like_count,permalink,timestamp"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    """Parse Graph API timestamps such as 2026-11-18T10:15:00+0000 or ...Z."""
    v = value.strip().replace("Z", "+00:00")
    if len(v) >= 5 and (v[-5] in "+-") and v[-3] != ":":
        v = v[:-2] + ":" + v[-2:]
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hashtags_used_in_window(conn: sqlite3.Connection, now: datetime | None = None) -> set[str]:
    now = now or utcnow()
    rows = conn.execute(
        "SELECT DISTINCT hashtag FROM hashtag_queries WHERE queried_at >= ?",
        (iso(now - CAP_WINDOW),),
    ).fetchall()
    return {r["hashtag"] for r in rows}


def plan_hashtags(conn: sqlite3.Connection, wanted: list[str], now: datetime | None = None
                  ) -> tuple[list[str], list[str]]:
    """Split wanted hashtags into (allowed, skipped) under the rolling 7-day cap.

    Hashtags already queried in the window are free to query again; new ones use up slots.
    """
    used = hashtags_used_in_window(conn, now)
    allowed, skipped = [], []
    slots = HASHTAG_CAP - len(used)
    for tag in dict.fromkeys(t.lower().lstrip("#") for t in wanted):
        if tag in used:
            allowed.append(tag)
        elif slots > 0:
            allowed.append(tag)
            slots -= 1
        else:
            skipped.append(tag)
    return allowed, skipped


def upsert_post(conn: sqlite3.Connection, media: dict, hashtag: str, edge: str,
                source: str = "graph_api", seen_at: datetime | None = None) -> None:
    seen = iso(seen_at or utcnow())
    caption = media.get("caption")
    ts = iso(parse_ts(media["timestamp"]))
    conn.execute(
        """
        INSERT INTO posts (media_id, caption, caption_norm, timestamp, permalink, media_type,
                           like_count, comments_count, username, disclosure, source,
                           first_seen_at, last_seen_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(media_id) DO UPDATE SET
            like_count = excluded.like_count,
            comments_count = excluded.comments_count,
            caption = COALESCE(excluded.caption, posts.caption),
            caption_norm = COALESCE(excluded.caption_norm, posts.caption_norm),
            username = COALESCE(excluded.username, posts.username),
            last_seen_at = excluded.last_seen_at
        """,
        (
            str(media["id"]), caption, normalize(caption) if caption else None, ts,
            media.get("permalink"), media.get("media_type"), media.get("like_count"),
            media.get("comments_count"), media.get("username"),
            ",".join(disclosure_markers(caption)) or None, source, seen, seen,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO post_hashtags (media_id, hashtag, edge) VALUES (?,?,?)",
        (str(media["id"]), hashtag, edge),
    )


def collect(conn: sqlite3.Connection, client: GraphClient, ig_user_id: str,
            hashtags: list[str], *, include_top: bool = True, max_pages: int = 5,
            log=print) -> dict:
    if not ig_user_id:
        raise GraphError("IG_USER_ID is not set")
    allowed, skipped = plan_hashtags(conn, hashtags)
    for tag in skipped:
        log(f"skip #{tag}: would exceed {HASHTAG_CAP} unique hashtags in 7 days")

    stats = {"hashtags": 0, "posts": 0, "skipped": skipped, "errors": []}
    edges = ["recent_media"] + (["top_media"] if include_top else [])
    for tag in allowed:
        try:
            res = client.get("ig_hashtag_search", user_id=ig_user_id, q=tag)
            ids = res.get("data") or []
            if not ids:
                log(f"#{tag}: no hashtag id returned")
                continue
            hashtag_id = ids[0]["id"]
            conn.execute(
                "INSERT INTO hashtag_queries (hashtag, hashtag_id, queried_at) VALUES (?,?,?)",
                (tag, hashtag_id, iso(utcnow())),
            )
            count = 0
            for edge in edges:
                for media in client.paginate(f"{hashtag_id}/{edge}", max_pages=max_pages,
                                             user_id=ig_user_id, fields=MEDIA_FIELDS, limit=50):
                    if "timestamp" not in media:
                        continue
                    upsert_post(conn, media, tag, edge)
                    count += 1
            conn.commit()
            stats["hashtags"] += 1
            stats["posts"] += count
            log(f"#{tag}: {count} posts")
        except GraphError as exc:
            conn.rollback()
            stats["errors"].append(f"#{tag}: {exc}")
            log(f"#{tag}: ERROR {exc}")
    return stats
