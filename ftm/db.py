"""SQLite storage. One file, no server, easy to share for independent checking."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- Every hashtag lookup we make, so the 30-unique-hashtags-per-7-days cap can be enforced.
CREATE TABLE IF NOT EXISTS hashtag_queries (
    id            INTEGER PRIMARY KEY,
    hashtag       TEXT NOT NULL,
    hashtag_id    TEXT,
    queried_at    TEXT NOT NULL            -- ISO 8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_hashtag_queries_time ON hashtag_queries(queried_at);

-- Posts as returned by the Graph API hashtag endpoints.
-- The hashtag endpoints usually do not return the posting account, so `username` is often NULL.
CREATE TABLE IF NOT EXISTS posts (
    media_id        TEXT PRIMARY KEY,
    caption         TEXT,
    caption_norm    TEXT,
    timestamp       TEXT NOT NULL,         -- ISO 8601 UTC, when the post was published
    permalink       TEXT,
    media_type      TEXT,
    like_count      INTEGER,
    comments_count  INTEGER,
    username        TEXT,
    disclosure      TEXT,                  -- comma-separated disclosure markers found in caption
    source          TEXT NOT NULL DEFAULT 'graph_api',   -- 'graph_api' | 'demo'
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(timestamp);

-- Which hashtag(s) surfaced a post (a post can appear under several).
CREATE TABLE IF NOT EXISTS post_hashtags (
    media_id   TEXT NOT NULL REFERENCES posts(media_id) ON DELETE CASCADE,
    hashtag    TEXT NOT NULL,
    edge       TEXT NOT NULL,              -- 'recent_media' | 'top_media' | 'demo'
    PRIMARY KEY (media_id, hashtag, edge)
);

-- Ads from the Meta Ad Library (political and issue ads).
CREATE TABLE IF NOT EXISTS ads (
    ad_id             TEXT PRIMARY KEY,
    page_id           TEXT,
    page_name         TEXT,
    bylines           TEXT,                -- "Paid for by" funding entity
    spend_lower       INTEGER,
    spend_upper       INTEGER,
    impressions_lower INTEGER,
    impressions_upper INTEGER,
    currency          TEXT,
    start_time        TEXT,
    stop_time         TEXT,
    platforms         TEXT,                -- comma-separated publisher platforms
    body              TEXT,                -- creative body text (joined if several)
    body_norm         TEXT,
    snapshot_url      TEXT,
    search_term       TEXT,                -- the keyword that found it
    source            TEXT NOT NULL DEFAULT 'ad_library',
    fetched_at        TEXT NOT NULL
);

-- Output of the coordination detector. Re-created on each run.
CREATE TABLE IF NOT EXISTS clusters (
    cluster_id        INTEGER PRIMARY KEY,
    run_at            TEXT NOT NULL,
    size              INTEGER NOT NULL,
    window_posts      INTEGER NOT NULL,    -- posts inside the densest time window
    window_start      TEXT NOT NULL,
    window_end        TEXT NOT NULL,
    window_hours      REAL NOT NULL,
    mean_similarity   REAL NOT NULL,
    distinct_accounts INTEGER,             -- NULL when account data is unavailable
    keywords          TEXT,
    representative    TEXT,
    exact_share       REAL                 -- share of posts using the most common exact wording
);

CREATE TABLE IF NOT EXISTS cluster_posts (
    cluster_id  INTEGER NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
    media_id    TEXT NOT NULL REFERENCES posts(media_id) ON DELETE CASCADE,
    similarity  REAL NOT NULL,             -- similarity to the cluster's representative post
    in_window   INTEGER NOT NULL,
    PRIMARY KEY (cluster_id, media_id)
);

CREATE TABLE IF NOT EXISTS cluster_ads (
    cluster_id  INTEGER NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
    ad_id       TEXT NOT NULL REFERENCES ads(ad_id) ON DELETE CASCADE,
    match_type  TEXT NOT NULL,             -- 'text' (wording matches) | 'keyword' (shared topic terms)
    score       REAL NOT NULL,
    overlaps_window INTEGER NOT NULL,      -- ad was running during the cluster's window
    PRIMARY KEY (cluster_id, ad_id)
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
