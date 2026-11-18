# Follow the Money

A public-interest tool that surfaces **coordinated messaging on Instagram** and links it to
**who is paying for it**. It shows the evidence (matching captions, timing, disclosure
markers, ad spend) and lets people judge for themselves, whichever party, company or group
is behind a campaign.

> **Status:** prototype. Pure Python 3.10+ standard library, no third-party dependencies.

## Try it without API access

```bash
python -m ftm demo
open reports/demo/report.html    # or summary.md
```

The demo uses **entirely synthetic** data about a fictional ballot measure. It includes a
copy-paste campaign on each side (each with a matching paid ad), an organic viral quote
with no ad behind it, and background posts that should not cluster. It shows the method
catching both sides in the same way, and why every cluster still needs human review.

## How it works

```
 hashtags.txt ──► collect ──► posts ─┐
                                     ├─► detect ──► clusters ──► report.html / summary.md
 ad_keywords.txt ──► ads ──► ads ────┘
```

| Step | Module | What it does |
|---|---|---|
| Collector | `ftm/collector.py` | For each tracked hashtag: `ig_hashtag_search`, then `recent_media` and `top_media`. Stores caption, timestamp, permalink, media type, likes and comments in SQLite. Tracks every lookup and **refuses to exceed 30 unique hashtags per rolling 7 days**. |
| Money check | `ftm/adlibrary.py` | Queries the Meta Ad Library (`ads_archive`, `POLITICAL_AND_ISSUE_ADS`) for each keyword. Stores page, "paid for by" byline, spend and impression **ranges**, delivery dates, platforms and creative text. |
| Detector | `ftm/detector.py` | Normalises captions, finds near-duplicates (MinHash LSH on word 3-grams, confirmed with TF-IDF cosine), joins them into clusters, and flags clusters with many posts in a short window. Links clusters to ads by wording (strong) or shared key terms during the same period (weak). |
| Report | `ftm/report.py` | Static HTML page and Markdown summary: each cluster's representative caption, timeline, similarity, exact-copy share, hashtags, disclosure markers, related ads with funder and spend, and example posts. |

### Detection method

The same method, with the same thresholds, runs on every hashtag, topic and side.

1. **Normalise**: lowercase; strip links, hashtags, @mentions, emojis and punctuation.
   Letters from any script are kept. Captions under 6 words are ignored, because short
   phrases collide by chance.
2. **Collapse** exact duplicates of the normalised text.
3. **Candidates**: MinHash (64 permutations, 16 bands) over word 3-shingles.
4. **Verify**: TF-IDF (unigrams + bigrams, smoothed idf) cosine ≥ 0.70.
5. **Cluster**: union-find over verified pairs.
6. **Timing**: find the densest 24-hour window; list the cluster if ≥ 5 posts fall in it.
7. **Money**: an ad is a *wording* match if its text has cosine ≥ 0.50 with any caption
   in the cluster, or a *topic* match if it shares ≥ 3 of the cluster's top terms **and**
   was running within 2 days of the window.

All thresholds are command-line options (`--similarity`, `--window-hours`, `--min-posts`,
`--min-tokens`), and the report prints the values used.

### What the report does **not** say

- A cluster is **not** proof of coordination. Memes, lyrics, quotes, news headlines and
  copy-paste trends produce near-identical posts organically.
- An ad match means similar wording or topic at a similar time. It does **not** show that
  the advertiser paid for or organised the posts.
- Instagram's "Paid partnership" label is not exposed by the hashtag endpoints. The tool
  only notes self-disclosure written in captions (`#ad`, `#sponsored`, "paid partnership",
  "paid for by"…). Absence of a marker proves nothing.

## Running it on real data

### 1. Access

- A **Meta developer app** with an **Instagram business or creator account** connected to a
  Facebook Page. Hashtag search needs the `instagram_basic` permission and the Instagram
  Public Content Access feature (granted through App Review).
- **Ad Library API** access: confirm your identity at facebook.com/ID and accept the Ad
  Library API terms. The same user token can then call `ads_archive`.

### 2. Configure

```bash
cp .env.example .env            # add META_ACCESS_TOKEN and IG_USER_ID
$EDITOR config/hashtags.txt     # up to 30 hashtags; cover every side of each issue
$EDITOR config/ad_keywords.txt  # neutral search terms for the same topics
```

The token is read only from the environment. It is never written to the database, logs or
reports, and is redacted from error messages.

### 3. Run

```bash
python -m ftm init-db
python -m ftm quota             # hashtag slots used in the rolling 7-day window
python -m ftm collect           # run daily: recent_media only covers the last 24 hours
python -m ftm ads --days 30
python -m ftm report --days 7 --out reports/2026-W47
```

A daily cron entry is enough for the collector, for example:

```cron
15 6 * * * cd /path/to/follow-the-money && python -m ftm collect && python -m ftm ads --days 7
```

Data lives in `data/ftm.sqlite3` (schema in `ftm/db.py`). Reports go to `reports/`. Both are
git-ignored.

### Tests

```bash
pip install pytest
python -m pytest
```

Tests cover normalisation, the hashtag cap, API paging and token redaction (with a fake
HTTP opener, no network), detection, a symmetry check (swapping which side says what gives
the same result), HTML escaping, and the full demo pipeline.

## Principles

- **Show the evidence, not a verdict.**
- **Same method for every side and topic.** No per-topic tuning, no lists of "bad" actors.
- **Respect platform terms.** Only official Graph API and Ad Library endpoints; no scraping.
- **Open source**, so anyone can rerun the method on the same data and check it.

## Milestones

1. [ ] Set up the Graph API app and business account access.
2. [x] Build the collector and the database schema.
3. [x] Add the Ad Library money check.
4. [ ] Build the coordination detector and **test it on a week of real data**
       (detector built and tested on synthetic data).
5. [ ] Publish the first report (report generator built).
6. [ ] Approach research partners (e.g. Meta Content Library via ICPSR) for wider data access.

## Known limitations

- Hashtag-only coverage and the 30-hashtag weekly cap. Posts without a tracked hashtag are
  invisible.
- `recent_media` only returns posts from the last 24 hours, so missed collection days
  cannot be backfilled.
- Hashtag endpoints usually omit the posting account, so "distinct accounts" is shown as
  *not available* and account-level coordination cannot be measured yet.
- Captions only: images, video and audio are not compared.
- Similar wording can be organic, so every cluster needs human review.
- Ad Library coverage of Instagram is limited to ads Meta classifies as political or issue
  ads in the chosen countries.
