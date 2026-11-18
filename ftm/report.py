"""Report: a static HTML page and a Markdown summary built from the clusters table.

The report lists evidence. It never labels a cluster as astroturf, propaganda or similar.
"""

from __future__ import annotations

import html
import sqlite3
from collections import Counter
from datetime import timedelta
from pathlib import Path

from .collector import iso, parse_ts, utcnow
from .detector import DetectorParams

EXAMPLES_PER_CLUSTER = 8

METHOD_TEXT = (
    "Captions are lowercased and stripped of links, hashtags, mentions, emojis and "
    "punctuation. Captions under {min_tokens} words are ignored. Near-duplicates are found "
    "with MinHash on word 3-grams and confirmed with TF-IDF cosine similarity "
    "(threshold {similarity:.2f}). A cluster is listed when at least {min_posts} similar "
    "posts appear within {window_hours:g} hours. Ads are linked when their text matches the "
    "captions (cosine ≥ {ad_text_similarity:.2f}) or, more weakly, when they share "
    "{ad_keyword_min}+ of the cluster's key terms and ran at the same time. The same method "
    "is applied to every hashtag, topic and side."
)

CAVEATS = [
    "A cluster is not proof of coordination. Memes, lyrics, quotes, news headlines and "
    "copy-paste trends can all produce near-identical posts organically.",
    "Coverage is limited to the hashtags we track (30 unique per 7 days) and to what the "
    "Instagram Graph API returns. Hashtag endpoints usually omit the posting account.",
    "Instagram's 'Paid partnership' label is not available through these endpoints. "
    "Disclosure markers are only what posters wrote in the caption.",
    "Ad Library spend and impressions are published as ranges, shown here as given.",
    "An ad match means similar wording or topic at a similar time. It does not show that "
    "the advertiser paid for or organised the posts.",
]


def _fmt_range(lo, hi, currency: str | None = None) -> str:
    if lo is None and hi is None:
        return "not disclosed"
    cur = f" {currency}" if currency else ""
    if hi is None:
        return f"{lo:,}+{cur}"
    return f"{lo:,}–{hi:,}{cur}"


def _fmt_ts(value: str | None) -> str:
    if not value:
        return "—"
    return parse_ts(value).strftime("%Y-%m-%d %H:%M UTC")


def load_report_data(conn: sqlite3.Connection) -> dict:
    clusters = []
    for c in conn.execute("SELECT * FROM clusters ORDER BY window_posts DESC, mean_similarity DESC"):
        posts = conn.execute(
            """SELECT p.*, cp.similarity, cp.in_window,
                      (SELECT group_concat(DISTINCT hashtag) FROM post_hashtags h
                        WHERE h.media_id = p.media_id) AS tags
               FROM cluster_posts cp JOIN posts p ON p.media_id = cp.media_id
               WHERE cp.cluster_id = ? ORDER BY p.timestamp""",
            (c["cluster_id"],),
        ).fetchall()
        ads = conn.execute(
            """SELECT a.*, ca.match_type, ca.score, ca.overlaps_window
               FROM cluster_ads ca JOIN ads a ON a.ad_id = ca.ad_id
               WHERE ca.cluster_id = ?
               ORDER BY ca.match_type = 'text' DESC, ca.score DESC""",
            (c["cluster_id"],),
        ).fetchall()
        tag_counts: Counter = Counter()
        disclosure: Counter = Counter()
        for p in posts:
            tag_counts.update((p["tags"] or "").split(",") if p["tags"] else [])
            disclosure.update(p["disclosure"].split(",") if p["disclosure"] else [])
        clusters.append({"c": c, "posts": posts, "ads": ads, "tags": tag_counts,
                         "disclosure": disclosure,
                         "demo": any(p["source"] == "demo" for p in posts)})
    stats = conn.execute(
        "SELECT COUNT(*) n, MIN(timestamp) t0, MAX(timestamp) t1 FROM posts").fetchone()
    n_ads = conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
    tags = [r[0] for r in conn.execute(
        "SELECT DISTINCT hashtag FROM post_hashtags ORDER BY hashtag")]
    return {"clusters": clusters, "n_posts": stats["n"], "t0": stats["t0"], "t1": stats["t1"],
            "n_ads": n_ads, "hashtags": tags}


# ---------------------------------------------------------------- HTML

def _timeline_svg(posts, window_start: str, window_end: str,
                  period: tuple[str | None, str | None] = (None, None)) -> str:
    """Hourly histogram of a cluster's posts across the whole analysis period, so a burst
    can be seen against the rest of the week. Bars inside the densest window are highlighted."""
    times = [parse_ts(p["timestamp"]) for p in posts]
    t0 = min([*times, *([parse_ts(period[0])] if period[0] else [])])
    t0 = t0.replace(minute=0, second=0, microsecond=0)
    t1 = max([*times, *([parse_ts(period[1])] if period[1] else [])])
    n_bins = max(1, int((t1 - t0).total_seconds() // 3600) + 1)
    if n_bins > 24 * 14:  # beyond two weeks, bin by 6 hours
        step = timedelta(hours=6)
        n_bins = max(1, int((t1 - t0) / step) + 1)
    else:
        step = timedelta(hours=1)
    counts = [0] * n_bins
    for t in times:
        counts[min(n_bins - 1, int((t - t0) / step))] += 1
    ws, we = parse_ts(window_start), parse_ts(window_end)
    width, height, pad = 640, 90, 16
    bw = (width - 2) / n_bins
    peak = max(counts)
    bars = []
    for i, n in enumerate(counts):
        if not n:
            continue
        b0 = t0 + i * step
        inside = b0 + step > ws and b0 <= we
        h = (height - pad) * n / peak
        cls = "bar in" if inside else "bar"
        bars.append(
            f'<rect class="{cls}" x="{1 + i * bw:.1f}" y="{height - pad - h:.1f}" '
            f'width="{max(1.0, bw - 1):.1f}" height="{h:.1f}">'
            f"<title>{b0:%Y-%m-%d %H:%M} UTC: {n} post{'s' if n != 1 else ''}</title></rect>"
        )
    label = f"{t0:%b %d %H:%M} → {t1:%b %d %H:%M} UTC · peak {peak}/{'hour' if step.seconds == 3600 else '6h'}"
    return (
        f'<svg class="timeline" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Posts over time">{"".join(bars)}'
        f'<line class="axis" x1="0" x2="{width}" y1="{height - pad}" y2="{height - pad}"/>'
        f'<text class="axis-label" x="0" y="{height - 3}">{html.escape(label)}</text></svg>'
    )


CSS = """
:root{--bg:#fbfbf9;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--card:#fff;--accent:#2f5d8a;
--accent-soft:#c9d7e6;--warn:#8a5a00;--warn-bg:#fff4dc}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;--line:#33322f;
--card:#1f1f1d;--accent:#8db4dc;--accent-soft:#3a4b5e;--warn:#f0c46b;--warn-bg:#3a2f18}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:19px;margin:0}h3{font-size:14px;margin:18px 0 6px;
color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--muted);margin:0 0 24px}
.note{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:10px 14px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin:20px 0}
.stats{display:flex;flex-wrap:wrap;gap:8px 24px;margin:10px 0 4px;color:var(--muted);font-size:14px}
.stats b{color:var(--fg);font-variant-numeric:tabular-nums}
blockquote{margin:12px 0;padding:10px 14px;border-left:3px solid var(--accent);background:var(--bg);
border-radius:0 8px 8px 0;white-space:pre-wrap;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.cap{overflow-wrap:anywhere}.tablewrap{overflow-x:auto}
.timeline{width:100%;height:auto;display:block}.bar{fill:var(--accent-soft)}.bar.in{fill:var(--accent)}
.axis{stroke:var(--line)}.axis-label{fill:var(--muted);font-size:11px}
.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:1px 9px;
margin:2px 4px 2px 0;font-size:12.5px;color:var(--muted)}
.pill.text{border-color:var(--accent);color:var(--accent)}
a{color:var(--accent)}ul{padding-left:20px}footer{color:var(--muted);font-size:13px;margin-top:40px}
"""


def render_html(data: dict, p: DetectorParams) -> str:
    e = html.escape
    out = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Follow the Money</title>", f"<style>{CSS}</style></head><body><main>",
        "<h1>Follow the Money</h1>",
        f"<p class='sub'>Clusters of near-identical Instagram posts and related paid ads. "
        f"Generated {e(_fmt_ts(iso(utcnow())))}. Evidence only, not verdicts.</p>",
    ]
    if any(c["demo"] for c in data["clusters"]):
        out.append("<div class='note'><b>Demo data.</b> This report was built from synthetic "
                   "posts and ads. None of it describes real accounts, advertisers or campaigns."
                   "</div>")
    out.append("<div class='stats'>"
               f"<span><b>{data['n_posts']:,}</b> posts</span>"
               f"<span><b>{len(data['hashtags'])}</b> hashtags</span>"
               f"<span><b>{data['n_ads']:,}</b> ads checked</span>"
               f"<span><b>{len(data['clusters'])}</b> clusters</span>"
               f"<span>{e(_fmt_ts(data['t0']))} → {e(_fmt_ts(data['t1']))}</span></div>")
    out.append(f"<h3>Method</h3><p>{e(METHOD_TEXT.format(**p.__dict__))}</p>")
    out.append("<h3>Read with care</h3><ul>" +
               "".join(f"<li>{e(x)}</li>" for x in CAVEATS) + "</ul>")

    if not data["clusters"]:
        out.append("<div class='card'>No clusters met the thresholds in this period.</div>")

    for n, item in enumerate(data["clusters"], 1):
        c, posts, ads = item["c"], item["posts"], item["ads"]
        acct = (f"<span><b>{c['distinct_accounts']}</b> distinct accounts</span>"
                if c["distinct_accounts"] is not None else
                "<span>accounts: <b>not available</b></span>")
        out.append(f"<section class='card' id='cluster-{n}'><h2>Cluster {n}</h2>")
        out.append("<div class='stats'>"
                   f"<span><b>{c['size']}</b> posts</span>"
                   f"<span><b>{c['window_posts']}</b> within {c['window_hours']:g}h</span>"
                   f"<span>similarity <b>{c['mean_similarity']:.2f}</b></span>"
                   f"<span>exact same wording <b>{c['exact_share']:.0%}</b></span>"
                   f"{acct}</div>")
        out.append(f"<blockquote>{e(c['representative'] or '')}</blockquote>")
        out.append(_timeline_svg(posts, c["window_start"], c["window_end"],
                                 (data["t0"], data["t1"])))
        out.append(f"<p class='stats'><span>Densest window: <b>{e(_fmt_ts(c['window_start']))}"
                   f"</b> → <b>{e(_fmt_ts(c['window_end']))}</b></span></p>")
        out.append("<div>" + "".join(f"<span class='pill'>#{e(t)} · {k}</span>"
                                    for t, k in item["tags"].most_common(10)) + "</div>")
        if item["disclosure"]:
            out.append("<p>Disclosure markers in captions: " + ", ".join(
                f"<b>{e(k)}</b> ×{v}" for k, v in item["disclosure"].most_common()) + "</p>")
        else:
            out.append("<p>No disclosure markers (#ad, paid partnership, …) in captions.</p>")

        out.append("<h3>Paid ads with related wording</h3>")
        if ads:
            out.append("<div class='tablewrap'><table><tr><th>Match</th><th>Page / paid for by"
                       "</th><th>Spend</th><th>Impressions</th><th>Ran</th><th>Ad text</th></tr>")
            for a in ads:
                kind = ("<span class='pill text'>wording</span>" if a["match_type"] == "text"
                        else "<span class='pill'>topic</span>")
                by = e(a["bylines"] or "no byline")
                page = e(a["page_name"] or "?")
                link = (f" <a href='{e(a['snapshot_url'])}' rel='noopener'>ad</a>"
                        if a["snapshot_url"] and a["snapshot_url"].startswith("https://") else "")
                body = e((a["body"] or "")[:220]) + ("…" if a["body"] and len(a["body"]) > 220 else "")
                out.append(
                    f"<tr><td>{kind}<br><small>{a['score']:.2f}</small></td>"
                    f"<td>{page}<br><small>Paid for by: {by}</small>{link}</td>"
                    f"<td class='num'>{e(_fmt_range(a['spend_lower'], a['spend_upper'], a['currency']))}</td>"
                    f"<td class='num'>{e(_fmt_range(a['impressions_lower'], a['impressions_upper']))}</td>"
                    f"<td class='num'>{e(_fmt_ts(a['start_time'])[:10])} → "
                    f"{e(_fmt_ts(a['stop_time'])[:10] if a['stop_time'] else 'active')}</td>"
                    f"<td class='cap'>{body}</td></tr>")
            out.append("</table></div>")
        else:
            out.append("<p>No matching ads found in the Ad Library data we pulled.</p>")

        out.append(f"<h3>Example posts ({min(EXAMPLES_PER_CLUSTER, len(posts))} of {len(posts)})</h3>")
        out.append("<div class='tablewrap'><table><tr><th>Posted</th><th>Similarity</th>"
                   "<th>Likes / comments</th><th>Caption</th></tr>")
        for post in _examples(posts):
            link = post["permalink"]
            when = e(_fmt_ts(post["timestamp"]))
            if link and link.startswith("https://"):
                when = f"<a href='{e(link)}' rel='noopener'>{when}</a>"
            out.append(
                f"<tr><td class='num'>{when}</td><td class='num'>{post['similarity']:.2f}</td>"
                f"<td class='num'>{post['like_count'] if post['like_count'] is not None else '—'} / "
                f"{post['comments_count'] if post['comments_count'] is not None else '—'}</td>"
                f"<td class='cap'>{e(post['caption'] or '')}</td></tr>")
        out.append("</table></div></section>")

    out.append("<footer>Open-source method: anyone can rerun it on the same data and check "
               "the result. Report errors or organic explanations so clusters can be "
               "re-reviewed.</footer></main></body></html>")
    return "\n".join(out)


def _examples(posts, k: int = EXAMPLES_PER_CLUSTER):
    """Earliest posts plus the least similar ones, so readers see the range of wording."""
    if len(posts) <= k:
        return posts
    first = list(posts[: k // 2])
    rest = sorted(posts[k // 2:], key=lambda p: p["similarity"])[: k - len(first)]
    return sorted(first + rest, key=lambda p: p["timestamp"])


# ---------------------------------------------------------------- Markdown

def render_markdown(data: dict, p: DetectorParams) -> str:
    lines = ["# Follow the Money: weekly summary", "",
             f"Generated {_fmt_ts(iso(utcnow()))}. Evidence only, not verdicts.", ""]
    if any(c["demo"] for c in data["clusters"]):
        lines += ["> **Demo data.** Synthetic posts and ads; nothing here is real.", ""]
    lines += [f"- Posts analysed: {data['n_posts']:,} ({_fmt_ts(data['t0'])} → {_fmt_ts(data['t1'])})",
              f"- Hashtags: {', '.join('#' + t for t in data['hashtags']) or 'none'}",
              f"- Ads checked: {data['n_ads']:,}",
              f"- Clusters: {len(data['clusters'])}", "",
              "## Method", "", METHOD_TEXT.format(**p.__dict__), "",
              "## Read with care", ""] + [f"- {x}" for x in CAVEATS] + [""]
    for n, item in enumerate(data["clusters"], 1):
        c = item["c"]
        rep = (c["representative"] or "").replace("\n", " ")
        lines += [f"## Cluster {n}", "",
                  f"> {rep}", "",
                  f"- {c['size']} posts, {c['window_posts']} within {c['window_hours']:g}h "
                  f"({_fmt_ts(c['window_start'])} → {_fmt_ts(c['window_end'])})",
                  f"- Mean similarity {c['mean_similarity']:.2f}; "
                  f"{c['exact_share']:.0%} use the exact same wording",
                  f"- Hashtags: {', '.join(f'#{t} ({k})' for t, k in item['tags'].most_common(6))}",
                  "- Disclosure markers: " + (", ".join(f"{k} ×{v}" for k, v in
                                                       item["disclosure"].most_common()) or "none")]
        if item["ads"]:
            lines.append("- Related ads:")
            for a in item["ads"]:
                kind = "wording match" if a["match_type"] == "text" else "topic match"
                lines.append(
                    f"  - {kind} ({a['score']:.2f}): {a['page_name'] or '?'}, paid for by "
                    f"{a['bylines'] or 'no byline'}; spend "
                    f"{_fmt_range(a['spend_lower'], a['spend_upper'], a['currency'])}; "
                    f"{_fmt_ts(a['start_time'])[:10]} → "
                    f"{_fmt_ts(a['stop_time'])[:10] if a['stop_time'] else 'active'}")
        else:
            lines.append("- Related ads: none found")
        lines.append("")
    return "\n".join(lines)


def write_reports(conn: sqlite3.Connection, out_dir: Path, p: DetectorParams) -> tuple[Path, Path]:
    data = load_report_data(conn)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path, md_path = out_dir / "report.html", out_dir / "summary.md"
    html_path.write_text(render_html(data, p), encoding="utf-8")
    md_path.write_text(render_markdown(data, p), encoding="utf-8")
    return html_path, md_path
