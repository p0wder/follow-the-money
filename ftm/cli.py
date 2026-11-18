"""Command-line entry point: python -m ftm <command>."""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

from . import adlibrary, collector, db, demo, detector, report
from .collector import utcnow
from .config import load_settings
from .graph import GraphClient, GraphError


def _params(args) -> detector.DetectorParams:
    return detector.DetectorParams(
        min_tokens=args.min_tokens, similarity=args.similarity,
        window_hours=args.window_hours, min_posts=args.min_posts,
    )


def _add_detector_args(sp: argparse.ArgumentParser) -> None:
    d = detector.DetectorParams()
    sp.add_argument("--days", type=float, default=7, help="analyse posts from the last N days "
                    "(0 = all posts in the database)")
    sp.add_argument("--similarity", type=float, default=d.similarity)
    sp.add_argument("--window-hours", type=float, default=d.window_hours)
    sp.add_argument("--min-posts", type=int, default=d.min_posts)
    sp.add_argument("--min-tokens", type=int, default=d.min_tokens)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ftm", description=__doc__)
    ap.add_argument("--db", type=Path, help="SQLite path (default: $FTM_DB_PATH or data/ftm.sqlite3)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create the database schema")
    c = sub.add_parser("collect", help="pull recent/top media for the configured hashtags")
    c.add_argument("--no-top", action="store_true", help="skip top_media, only recent_media")
    c.add_argument("--max-pages", type=int, default=5)
    sub.add_parser("quota", help="show hashtag slots used in the rolling 7-day window")
    a = sub.add_parser("ads", help="query the Ad Library for the configured keywords")
    a.add_argument("--days", type=int, default=30)
    a.add_argument("--max-pages", type=int, default=5)
    a.add_argument("--all-platforms", action="store_true",
                   help="include ads not shown on Instagram")
    d = sub.add_parser("detect", help="find clusters of near-identical posts")
    _add_detector_args(d)
    r = sub.add_parser("report", help="write report.html and summary.md")
    r.add_argument("--out", type=Path, default=Path("reports"))
    _add_detector_args(r)
    dm = sub.add_parser("demo", help="load synthetic data, detect and report (no API needed)")
    dm.add_argument("--out", type=Path, default=Path("reports/demo"))
    _add_detector_args(dm)

    args = ap.parse_args(argv)
    settings = load_settings()
    db_path = args.db or settings.db_path

    if args.cmd == "demo":
        db_path = args.db or Path("data/demo.sqlite3")
        if db_path.exists():
            db_path.unlink()
    conn = db.connect(db_path)

    try:
        if args.cmd == "init-db":
            print(f"schema ready at {db_path}")
        elif args.cmd == "quota":
            used = sorted(collector.hashtags_used_in_window(conn))
            print(f"{len(used)}/{collector.HASHTAG_CAP} hashtag slots used in the last 7 days")
            for t in used:
                print(f"  #{t}")
        elif args.cmd == "collect":
            client = GraphClient(settings.access_token or "", settings.graph_api_version)
            if not settings.hashtags:
                print(f"no hashtags in {settings.hashtags_file}", file=sys.stderr)
                return 2
            stats = collector.collect(conn, client, settings.ig_user_id or "", settings.hashtags,
                                      include_top=not args.no_top, max_pages=args.max_pages)
            print(f"done: {stats['posts']} posts from {stats['hashtags']} hashtags, "
                  f"{len(stats['skipped'])} skipped, {len(stats['errors'])} errors")
            return 1 if stats["errors"] and not stats["hashtags"] else 0
        elif args.cmd == "ads":
            client = GraphClient(settings.access_token or "", settings.graph_api_version)
            if not settings.ad_keywords:
                print(f"no keywords in {settings.keywords_file}", file=sys.stderr)
                return 2
            stats = adlibrary.fetch_ads(conn, client, settings.ad_keywords, settings.ad_countries,
                                        days=args.days, max_pages=args.max_pages,
                                        platform=None if args.all_platforms else "INSTAGRAM")
            print(f"done: {stats['ads']} ads for {stats['keywords']} keywords, "
                  f"{len(stats['errors'])} errors")
            return 1 if stats["errors"] and not stats["keywords"] else 0
        elif args.cmd in ("detect", "report", "demo"):
            p = _params(args)
            if args.cmd == "demo":
                n_posts, n_ads = demo.load(conn)
                print(f"loaded {n_posts} synthetic posts and {n_ads} synthetic ads")
                since = None
            else:
                since = utcnow() - timedelta(days=args.days) if args.days else None
            clusters = detector.run(conn, p, since=since)
            print(f"{len(clusters)} clusters")
            for i, cl in enumerate(clusters, 1):
                rep = cl.representative.replace("\n", " ")
                print(f"  {i}. {len(cl.posts)} posts, {cl.window_posts} in "
                      f"{p.window_hours:g}h, sim {cl.mean_similarity:.2f}: {rep[:70]}")
            if args.cmd in ("report", "demo"):
                html_path, md_path = report.write_reports(conn, args.out, p)
                print(f"wrote {html_path} and {md_path}")
    except GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
