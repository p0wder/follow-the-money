"""Coordination detector.

Method (identical for every topic, hashtag and side):

1. Normalise captions (see normalize.py). Drop captions shorter than `min_tokens` words,
   because short phrases ("vote today", "so true") collide organically.
2. Collapse exact duplicates of the normalised caption.
3. Candidate pairs: MinHash LSH over word 3-shingles of the unique captions.
4. Verify each candidate pair with TF-IDF (unigrams + bigrams) cosine similarity;
   keep pairs with cosine >= `similarity`.
5. Connect verified pairs into clusters (union-find).
6. For each cluster, find the densest `window_hours` window of posting times. Flag the
   cluster if at least `min_posts` posts fall inside that window.

The output is a list of clusters with the evidence needed to judge them. A flag means
"look at this", not "this is coordinated": memes, song lyrics, news headlines and
quotes can all produce similar clusters organically.
"""

from __future__ import annotations

import hashlib
import math
import random
import sqlite3
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .collector import iso, parse_ts, utcnow
from .normalize import tokens

# A small stopword list so that keywords and TF-IDF weights are about content.
STOPWORDS = frozenset("""
a an and are as at be been but by can do for from has have he her his i if in into is it its
just me my no not of on or our out so that the their them they this to up us was we were what
when which who will with you your all about more than then there these those would could should
""".split())

_MERSENNE = (1 << 61) - 1


@dataclass
class DetectorParams:
    min_tokens: int = 6
    similarity: float = 0.70
    window_hours: float = 24.0
    min_posts: int = 5
    num_perm: int = 64
    bands: int = 16
    shingle: int = 3
    seed: int = 1729
    ad_text_similarity: float = 0.50
    ad_keyword_min: int = 3


@dataclass
class Post:
    media_id: str
    caption: str
    norm: str
    ts: datetime
    permalink: str | None = None
    username: str | None = None
    disclosure: str | None = None
    likes: int | None = None
    comments: int | None = None
    hashtags: list[str] = field(default_factory=list)


@dataclass
class Cluster:
    posts: list[Post]
    similarity_to_rep: dict[str, float]
    mean_similarity: float
    window_start: datetime
    window_end: datetime
    window_ids: set[str]
    representative: str
    keywords: list[str]
    distinct_accounts: int | None
    exact_share: float

    @property
    def window_posts(self) -> int:
        return len(self.window_ids)


# ---------------------------------------------------------------- text vectors

def _stable_hash(s: str) -> int:
    return struct.unpack("<Q", hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest())[0]


def terms(norm: str) -> list[str]:
    toks = [t for t in tokens(norm) if t not in STOPWORDS]
    return toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]


class TfidfModel:
    """TF-IDF with smoothed idf and L2-normalised sparse vectors (dicts)."""

    def __init__(self, docs: list[str]):
        self.n = len(docs)
        df: Counter = Counter()
        for d in docs:
            df.update(set(terms(d)))
        self.idf = {t: math.log((1 + self.n) / (1 + c)) + 1.0 for t, c in df.items()}

    def vector(self, norm: str) -> dict[str, float]:
        tf = Counter(terms(norm))
        default = math.log(1 + self.n) + 1.0
        vec = {t: (1 + math.log(c)) * self.idf.get(t, default) for t, c in tf.items()}
        length = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / length for t, v in vec.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


# ---------------------------------------------------------------- MinHash LSH

class MinHasher:
    def __init__(self, num_perm: int, seed: int):
        rng = random.Random(seed)
        self.params = [(rng.randrange(1, _MERSENNE), rng.randrange(0, _MERSENNE))
                       for _ in range(num_perm)]

    def signature(self, shingles: set[int]) -> list[int]:
        if not shingles:
            return [_MERSENNE] * len(self.params)
        return [min((a * s + b) % _MERSENNE for s in shingles) for a, b in self.params]


def shingles(norm: str, k: int) -> set[int]:
    toks = tokens(norm)
    if len(toks) < k:
        return {_stable_hash(" ".join(toks))} if toks else set()
    return {_stable_hash(" ".join(toks[i:i + k])) for i in range(len(toks) - k + 1)}


def lsh_candidates(texts: list[str], p: DetectorParams) -> set[tuple[int, int]]:
    if p.num_perm % p.bands:
        raise ValueError("num_perm must be divisible by bands")
    rows = p.num_perm // p.bands
    hasher = MinHasher(p.num_perm, p.seed)
    buckets: dict[tuple, list[int]] = defaultdict(list)
    for idx, text in enumerate(texts):
        sig = hasher.signature(shingles(text, p.shingle))
        for b in range(p.bands):
            buckets[(b, tuple(sig[b * rows:(b + 1) * rows]))].append(idx)
    pairs: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((members[i], members[j]))
    return pairs


# ---------------------------------------------------------------- clustering

class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def densest_window(times: list[datetime], hours: float) -> tuple[int, int]:
    """Return (i, j) indices into sorted `times` of the window with the most posts."""
    width = timedelta(hours=hours)
    best = (0, 0)
    j = 0
    for i in range(len(times)):
        if j < i:
            j = i
        while j + 1 < len(times) and times[j + 1] - times[i] <= width:
            j += 1
        if j - i > best[1] - best[0]:
            best = (i, j)
    return best


def load_posts(conn: sqlite3.Connection, since: datetime | None = None,
               until: datetime | None = None) -> list[Post]:
    q = "SELECT * FROM posts WHERE caption_norm IS NOT NULL"
    args: list = []
    if since:
        q += " AND timestamp >= ?"
        args.append(iso(since))
    if until:
        q += " AND timestamp <= ?"
        args.append(iso(until))
    tags: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT media_id, hashtag FROM post_hashtags"):
        tags[r["media_id"]].append(r["hashtag"])
    return [
        Post(r["media_id"], r["caption"] or "", r["caption_norm"], parse_ts(r["timestamp"]),
             r["permalink"], r["username"], r["disclosure"], r["like_count"],
             r["comments_count"], sorted(set(tags.get(r["media_id"], []))))
        for r in conn.execute(q + " ORDER BY timestamp", args)
    ]


def detect(posts: list[Post], p: DetectorParams | None = None) -> list[Cluster]:
    p = p or DetectorParams()
    eligible = [x for x in posts if len(tokens(x.norm)) >= p.min_tokens]

    # Collapse exact normalised duplicates.
    groups: dict[str, list[Post]] = defaultdict(list)
    for post in eligible:
        groups[post.norm].append(post)
    uniq = list(groups)
    if not uniq:
        return []

    model = TfidfModel(uniq)
    vecs = [model.vector(t) for t in uniq]
    uf = _UnionFind(len(uniq))
    for i, j in lsh_candidates(uniq, p):
        if cosine(vecs[i], vecs[j]) >= p.similarity:
            uf.union(i, j)

    comps: dict[int, list[int]] = defaultdict(list)
    for i in range(len(uniq)):
        comps[uf.find(i)].append(i)

    clusters: list[Cluster] = []
    for members in comps.values():
        cposts = [post for i in members for post in groups[uniq[i]]]
        if len(cposts) < p.min_posts:
            continue
        cposts.sort(key=lambda x: x.ts)
        i0, i1 = densest_window([x.ts for x in cposts], p.window_hours)
        window = cposts[i0:i1 + 1]
        if len(window) < p.min_posts:
            continue

        # Representative: the most-posted wording, ties broken by earliest appearance.
        rep_idx = max(members, key=lambda i: (len(groups[uniq[i]]),
                                              -min(x.ts for x in groups[uniq[i]]).timestamp()))
        rep_vec = vecs[rep_idx]
        sim_by_text = {uniq[i]: cosine(rep_vec, vecs[i]) for i in members}
        similarity_to_rep = {x.media_id: sim_by_text[x.norm] for x in cposts}

        clusters.append(Cluster(
            posts=cposts,
            similarity_to_rep=similarity_to_rep,
            mean_similarity=_mean_pairwise(members, vecs, groups, uniq),
            window_start=window[0].ts,
            window_end=window[-1].ts,
            window_ids={x.media_id for x in window},
            representative=groups[uniq[rep_idx]][0].caption,
            keywords=_keywords(members, vecs, groups, uniq),
            distinct_accounts=_distinct_accounts(cposts),
            exact_share=len(groups[uniq[rep_idx]]) / len(cposts),
        ))

    clusters.sort(key=lambda c: (c.window_posts, c.mean_similarity), reverse=True)
    return clusters


def _mean_pairwise(members, vecs, groups, uniq, cap: int = 150) -> float:
    """Mean cosine over all post pairs (exact duplicates count as 1.0), sampled if large."""
    weights = [len(groups[uniq[i]]) for i in members]
    idx = members
    if len(idx) > cap:
        rng = random.Random(0)
        pick = sorted(rng.sample(range(len(idx)), cap))
        idx = [members[k] for k in pick]
        weights = [weights[k] for k in pick]
    total = pairs = 0.0
    for a in range(len(idx)):
        w = weights[a]
        total += w * (w - 1) / 2
        pairs += w * (w - 1) / 2
        for b in range(a + 1, len(idx)):
            n = w * weights[b]
            total += n * cosine(vecs[idx[a]], vecs[idx[b]])
            pairs += n
    return total / pairs if pairs else 1.0


def _keywords(members, vecs, groups, uniq, k: int = 8) -> list[str]:
    score: Counter = Counter()
    for i in members:
        for t, v in vecs[i].items():
            if " " not in t:
                score[t] += v * len(groups[uniq[i]])
    return [t for t, _ in score.most_common(k)]


def _distinct_accounts(posts: list[Post]) -> int | None:
    names = {x.username for x in posts if x.username}
    return len(names) if names else None


# ---------------------------------------------------------------- ad matching

@dataclass
class AdMatch:
    ad_id: str
    match_type: str
    score: float
    overlaps_window: bool


def match_ads(cluster: Cluster, ads: list[sqlite3.Row], p: DetectorParams) -> list[AdMatch]:
    """Link a cluster to ads whose wording matches it, or that share its topic terms.

    'text' matches compare the ad's creative text with the cluster's captions.
    'keyword' matches only mean the ad uses several of the same distinctive words,
    which is weaker evidence and shown as such.
    """
    from .adlibrary import ad_active_during

    if not ads:
        return []
    captions = list(dict.fromkeys(x.norm for x in cluster.posts))
    ad_texts = [a["body_norm"] or "" for a in ads]
    model = TfidfModel(captions + ad_texts)
    cvecs = [model.vector(c) for c in captions[:50]]
    kw = set(cluster.keywords)
    out: list[AdMatch] = []
    for ad, text in zip(ads, ad_texts):
        if not text:
            continue
        avec = model.vector(text)
        best = max(cosine(avec, c) for c in cvecs)
        overlaps = ad_active_during(ad["start_time"], ad["stop_time"],
                                    cluster.window_start, cluster.window_end)
        if best >= p.ad_text_similarity:
            out.append(AdMatch(ad["ad_id"], "text", round(best, 3), overlaps))
            continue
        shared = kw & set(tokens(text))
        if len(shared) >= p.ad_keyword_min and overlaps:
            out.append(AdMatch(ad["ad_id"], "keyword", round(len(shared) / len(kw), 3), overlaps))
    out.sort(key=lambda m: (m.match_type != "text", -m.score))
    return out[:10]


# ---------------------------------------------------------------- persistence

def run(conn: sqlite3.Connection, p: DetectorParams | None = None,
        since: datetime | None = None, until: datetime | None = None) -> list[Cluster]:
    p = p or DetectorParams()
    clusters = detect(load_posts(conn, since, until), p)
    ads = conn.execute("SELECT * FROM ads").fetchall()
    run_at = iso(utcnow())
    with conn:
        conn.execute("DELETE FROM clusters")
        for c in clusters:
            cur = conn.execute(
                """INSERT INTO clusters (run_at, size, window_posts, window_start, window_end,
                       window_hours, mean_similarity, distinct_accounts, keywords, representative,
                       exact_share)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (run_at, len(c.posts), c.window_posts, iso(c.window_start), iso(c.window_end),
                 p.window_hours, round(c.mean_similarity, 4), c.distinct_accounts,
                 ",".join(c.keywords), c.representative, round(c.exact_share, 4)),
            )
            cid = cur.lastrowid
            conn.executemany(
                "INSERT INTO cluster_posts (cluster_id, media_id, similarity, in_window) "
                "VALUES (?,?,?,?)",
                [(cid, x.media_id, round(c.similarity_to_rep[x.media_id], 4),
                  int(x.media_id in c.window_ids)) for x in c.posts],
            )
            conn.executemany(
                "INSERT INTO cluster_ads (cluster_id, ad_id, match_type, score, overlaps_window) "
                "VALUES (?,?,?,?,?)",
                [(cid, m.ad_id, m.match_type, m.score, int(m.overlaps_window))
                 for m in match_ads(c, ads, p)],
            )
    return clusters
