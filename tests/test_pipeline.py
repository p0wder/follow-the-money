import io
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from ftm import adlibrary, collector, db, demo, detector, report
from ftm.config import read_list_file
from ftm.graph import GraphClient, GraphError
from ftm.normalize import disclosure_markers, hashtags_in, normalize

T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


# ---------------------------------------------------------------- normalize

def test_normalize_strips_noise_and_keeps_other_scripts():
    raw = "Vote YES on R!! 🚌🚌 https://x.co/abc @someone #MeasureR Don’t wait. Ça va 東京"
    assert normalize(raw) == "vote yes on r dont wait ça va 東京"


def test_disclosure_markers():
    assert disclosure_markers("Love this #ad #Sponsored") == ["#ad", "#sponsored"]
    assert disclosure_markers("#adventure time") == []
    assert disclosure_markers("Paid partnership with X") == ["paid partnership"]


def test_hashtags_in():
    assert hashtags_in("a #Foo b #bar #foo") == ["bar", "foo"]


def test_list_file_keeps_hashtag_entries(tmp_path):
    f = tmp_path / "tags.txt"
    f.write_text("# comment\n#climate\n\nelection\n#\n")
    assert read_list_file(f) == ["#climate", "election"]


# ---------------------------------------------------------------- collector

def test_parse_ts_formats():
    assert collector.parse_ts("2026-11-18T10:15:00+0000") == datetime(2026, 11, 18, 10, 15, tzinfo=timezone.utc)
    assert collector.parse_ts("2026-11-18T10:15:00Z").hour == 10


def test_hashtag_cap(conn):
    now = T0
    for i in range(29):
        conn.execute("INSERT INTO hashtag_queries (hashtag, queried_at) VALUES (?,?)",
                     (f"t{i}", collector.iso(now - timedelta(days=1))))
    # An old query outside the 7-day window does not count.
    conn.execute("INSERT INTO hashtag_queries (hashtag, queried_at) VALUES ('old', ?)",
                 (collector.iso(now - timedelta(days=8)),))
    allowed, skipped = collector.plan_hashtags(conn, ["t0", "new1", "new2", "#T1"], now)
    assert allowed == ["t0", "new1", "t1"]
    assert skipped == ["new2"]


class FakeOpener:
    """Stands in for urllib's urlopen, routing by URL path."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        path = urllib.parse.urlsplit(url).path.split("/", 2)[2]
        body = self.routes[path]
        if callable(body):
            body = body(url)
        return io.BytesIO(json.dumps(body).encode())


def test_collect_stores_posts_and_queries(conn):
    media = [
        {"id": "1", "caption": "Hello world #ad", "timestamp": "2026-11-01T10:00:00+0000",
         "permalink": "https://www.instagram.com/p/1/", "media_type": "IMAGE",
         "like_count": 3, "comments_count": 1},
        {"id": "2", "caption": None, "timestamp": "2026-11-01T11:00:00+0000"},
    ]
    opener = FakeOpener({
        "ig_hashtag_search": {"data": [{"id": "h1"}]},
        "h1/recent_media": {"data": media, "paging": {}},
        "h1/top_media": {"data": media[:1]},
    })
    client = GraphClient("SECRET", opener=opener)
    stats = collector.collect(conn, client, "ig1", ["Foo"], log=lambda *_: None)
    assert stats["posts"] == 3 and stats["hashtags"] == 1
    rows = conn.execute("SELECT * FROM posts ORDER BY media_id").fetchall()
    assert [r["media_id"] for r in rows] == ["1", "2"]
    assert rows[0]["disclosure"] == "#ad"
    assert rows[0]["caption_norm"] == "hello world"
    edges = {tuple(r) for r in conn.execute("SELECT media_id, edge FROM post_hashtags")}
    assert edges == {("1", "recent_media"), ("1", "top_media"), ("2", "recent_media")}
    assert collector.hashtags_used_in_window(conn) == {"foo"}


def test_graph_error_redacts_token():
    import urllib.error

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {},
                                     io.BytesIO(b'{"error":{"message":"nope"}}'))

    client = GraphClient("SECRET", opener=opener)
    with pytest.raises(GraphError) as exc:
        client.get("x", a=1)
    assert "SECRET" not in str(exc.value) and "nope" in str(exc.value)


def test_paginate_follows_next():
    pages = {
        "ads_archive": lambda url: (
            {"data": [{"id": "b"}]} if "after=" in url else
            {"data": [{"id": "a"}], "paging": {"next": url + "&after=x"}}
        ),
    }
    client = GraphClient("t", opener=FakeOpener(pages))
    assert [x["id"] for x in client.paginate("ads_archive")] == ["a", "b"]


# ---------------------------------------------------------------- ad library

def test_upsert_ad_ranges(conn):
    adlibrary.upsert_ad(conn, {
        "id": "9", "page_name": "P", "bylines": "Funder",
        "spend": {"lower_bound": "100", "upper_bound": "199"},
        "impressions": {"lower_bound": "1000"},
        "ad_delivery_start_time": "2026-11-01", "ad_creative_bodies": ["Hi there", "Second"],
    }, "kw")
    row = conn.execute("SELECT * FROM ads").fetchone()
    assert (row["spend_lower"], row["spend_upper"]) == (100, 199)
    assert (row["impressions_lower"], row["impressions_upper"]) == (1000, None)
    assert row["start_time"] == "2026-11-01T00:00:00Z"
    assert row["body_norm"] == "hi there second"


def test_ad_active_during():
    s, e = T0, T0 + timedelta(hours=5)
    assert adlibrary.ad_active_during("2026-10-30T00:00:00Z", None, s, e)
    assert not adlibrary.ad_active_during("2026-10-01T00:00:00Z", "2026-10-10T00:00:00Z", s, e)


# ---------------------------------------------------------------- detector

def _posts(texts_and_hours):
    return [detector.Post(f"m{i}", t, normalize(t), T0 + timedelta(hours=h))
            for i, (t, h) in enumerate(texts_and_hours)]


def test_detects_near_duplicates_in_window():
    base = "Measure Q will raise your rent and nobody voted for the hidden fees inside it"
    variants = [base, base + " !!", base.replace("nobody", "no one"),
                base.replace("your rent", "rents"), base + " share this"]
    noise = [f"completely different caption number {i} about the weather and lunch plans"
             for i in range(5)]
    posts = _posts([(v, i * 0.5) for i, v in enumerate(variants)] +
                   [(n, 30 + i) for i, n in enumerate(noise)])
    clusters = detector.detect(posts, detector.DetectorParams(min_posts=5))
    assert len(clusters) == 1
    assert {p.media_id for p in clusters[0].posts} == {f"m{i}" for i in range(5)}
    assert clusters[0].window_posts == 5
    assert clusters[0].mean_similarity > 0.7


def test_spread_out_posts_are_not_flagged():
    base = "Measure Q will raise your rent and nobody voted for the hidden fees inside it"
    posts = _posts([(base, i * 48) for i in range(6)])  # one every two days
    assert detector.detect(posts, detector.DetectorParams(min_posts=5, window_hours=24)) == []


def test_short_captions_ignored():
    posts = _posts([("so true", i * 0.1) for i in range(10)])
    assert detector.detect(posts) == []


def test_densest_window():
    times = [T0 + timedelta(hours=h) for h in [0, 10, 11, 12, 13, 40]]
    assert detector.densest_window(times, 5) == (1, 4)


def test_method_is_symmetric():
    """Swapping which side says what must not change the result."""
    a = "Vote yes on measure twelve to fund the new library and keep our parks open"
    b = "Vote no on measure twelve because it raises taxes and wastes our money again"
    posts1 = _posts([(a, i * 0.2) for i in range(6)] + [(b, 50 + i * 0.2) for i in range(6)])
    posts2 = _posts([(b, i * 0.2) for i in range(6)] + [(a, 50 + i * 0.2) for i in range(6)])
    c1, c2 = detector.detect(posts1), detector.detect(posts2)
    assert sorted(len(c.posts) for c in c1) == sorted(len(c.posts) for c in c2) == [6, 6]


# ---------------------------------------------------------------- end to end

def test_demo_end_to_end(conn, tmp_path):
    demo.load(conn)
    clusters = detector.run(conn)
    assert len(clusters) >= 3
    linked = conn.execute(
        "SELECT DISTINCT a.bylines FROM cluster_ads ca JOIN ads a USING (ad_id) "
        "WHERE ca.match_type = 'text'").fetchall()
    funders = {r[0] for r in linked}
    # Both sides' campaigns are found and linked to their own ads.
    assert "Citizens for Riverside Transit (fictional)" in funders
    assert "Riverside Taxpayers Alliance (fictional)" in funders
    # The unrelated library ad is never linked.
    assert not conn.execute("SELECT 1 FROM cluster_ads WHERE ad_id = 'demo_ad_003'").fetchone()
    # The organic viral quote is a cluster with no ad.
    quote = [c for c in clusters if "plant a tree" in c.representative]
    assert quote and quote[0].exact_share == 1.0

    html_path, md_path = report.write_reports(conn, tmp_path, detector.DetectorParams())
    html = html_path.read_text()
    assert "Demo data" in html and "<script" not in html
    assert "Cluster 1" in md_path.read_text()


def test_report_escapes_captions(conn, tmp_path):
    evil = "<script>alert(1)</script> this caption is long enough to be compared fully"
    for i in range(5):
        collector.upsert_post(conn, {"id": str(i), "caption": evil,
                                     "timestamp": collector.iso(T0 + timedelta(minutes=i))},
                              "x", "recent_media")
    detector.run(conn)
    html_path, _ = report.write_reports(conn, tmp_path, detector.DetectorParams())
    html = html_path.read_text()
    assert "<script>" not in html and "&lt;script&gt;" in html
