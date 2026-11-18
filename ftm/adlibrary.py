"""Money check: query the Meta Ad Library API for political and issue ads.

  GET /ads_archive?search_terms=...&ad_type=POLITICAL_AND_ISSUE_ADS
                  &ad_reached_countries=["US"]&fields=...

Spend and impressions are only published as ranges; we store the lower and upper bounds
as given and never present a single figure as exact.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from .collector import iso, parse_ts, utcnow
from .graph import GraphClient, GraphError
from .normalize import normalize

AD_FIELDS = ",".join([
    "id", "page_id", "page_name", "bylines", "spend", "impressions", "currency",
    "ad_delivery_start_time", "ad_delivery_stop_time", "ad_creative_bodies",
    "ad_snapshot_url", "publisher_platforms",
])


def _range(value) -> tuple[int | None, int | None]:
    if not isinstance(value, dict):
        return None, None
    def to_int(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None
    return to_int(value.get("lower_bound")), to_int(value.get("upper_bound"))


def _date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return iso(parse_ts(value if "T" in value else value + "T00:00:00+00:00"))
    except ValueError:
        return value


def upsert_ad(conn: sqlite3.Connection, ad: dict, search_term: str,
              source: str = "ad_library") -> None:
    bodies = ad.get("ad_creative_bodies") or []
    body = "\n---\n".join(b for b in bodies if b)
    s_lo, s_hi = _range(ad.get("spend"))
    i_lo, i_hi = _range(ad.get("impressions"))
    conn.execute(
        """
        INSERT INTO ads (ad_id, page_id, page_name, bylines, spend_lower, spend_upper,
                         impressions_lower, impressions_upper, currency, start_time, stop_time,
                         platforms, body, body_norm, snapshot_url, search_term, source, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ad_id) DO UPDATE SET
            spend_lower = excluded.spend_lower, spend_upper = excluded.spend_upper,
            impressions_lower = excluded.impressions_lower,
            impressions_upper = excluded.impressions_upper,
            stop_time = excluded.stop_time, fetched_at = excluded.fetched_at
        """,
        (
            str(ad["id"]), ad.get("page_id"), ad.get("page_name"), ad.get("bylines"),
            s_lo, s_hi, i_lo, i_hi, ad.get("currency"),
            _date(ad.get("ad_delivery_start_time")), _date(ad.get("ad_delivery_stop_time")),
            ",".join(ad.get("publisher_platforms") or []) or None,
            body or None, normalize(body) or None, ad.get("ad_snapshot_url"),
            search_term, source, iso(utcnow()),
        ),
    )


def fetch_ads(conn: sqlite3.Connection, client: GraphClient, keywords: list[str],
              countries: list[str], *, days: int = 30, max_pages: int = 5,
              platform: str | None = "INSTAGRAM", log=print) -> dict:
    since = (utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    stats = {"keywords": 0, "ads": 0, "errors": []}
    for kw in keywords:
        try:
            count = 0
            for ad in client.paginate(
                "ads_archive", max_pages=max_pages,
                search_terms=kw,
                ad_type="POLITICAL_AND_ISSUE_ADS",
                ad_reached_countries=json.dumps(countries),
                ad_delivery_date_min=since,
                ad_active_status="ALL",
                publisher_platforms=json.dumps([platform]) if platform else None,
                fields=AD_FIELDS,
                limit=100,
            ):
                upsert_ad(conn, ad, kw)
                count += 1
            conn.commit()
            stats["keywords"] += 1
            stats["ads"] += count
            log(f"'{kw}': {count} ads")
        except GraphError as exc:
            conn.rollback()
            stats["errors"].append(f"'{kw}': {exc}")
            log(f"'{kw}': ERROR {exc}")
    return stats


def ad_active_during(ad_start: str | None, ad_stop: str | None,
                     start: datetime, end: datetime, slack: timedelta = timedelta(days=2)) -> bool:
    """True if the ad's delivery period overlaps [start - slack, end + slack]."""
    if not ad_start:
        return False
    a0 = parse_ts(ad_start)
    a1 = parse_ts(ad_stop) if ad_stop else end + slack  # still running
    return a0 <= end + slack and a1 >= start - slack
