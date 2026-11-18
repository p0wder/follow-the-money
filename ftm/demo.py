"""Synthetic demo data so the pipeline can be tried without API access.

Everything here is invented: a fictional local ballot measure ("Measure R", a transit levy
in the fictional town of Riverside), fictional advertisers, and generated posts. The demo
deliberately includes:

  * a copy-paste campaign FOR the measure, with a matching paid ad;
  * a copy-paste campaign AGAINST the measure, with a matching paid ad
    (same method, both sides);
  * an organic copy-paste trend (a viral quote) with no ad behind it, to show why every
    cluster needs human review;
  * background posts that should not cluster.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone

from .adlibrary import upsert_ad
from .collector import iso, upsert_post

BASE_TIME = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)

FOR_TEMPLATE = [
    "Measure R means {more} buses, shorter commutes and cleaner air for Riverside families. "
    "Vote YES on R this Tuesday!",
    "Measure R means {more} buses, shorter commutes and cleaner air for every Riverside family. "
    "Vote yes on R on Tuesday",
]
AGAINST_TEMPLATE = [
    "Measure R is a blank cheque. {higher} taxes, no audit, no guarantee of a single new bus. "
    "Vote NO on R.",
    "Measure R is a blank check: {higher} taxes, no audits and no guarantee of one new bus. "
    "Vote no on R!",
]
VIRAL_QUOTE = ("The best time to plant a tree was twenty years ago. The second best time "
               "is now.")
BACKGROUND_OPENERS = [
    "Sunrise run along the river", "Ramen near the old train station", "Community garden harvest",
    "Town hall meeting about the transit plan", "Farmers market haul", "Throwback to the lake",
    "New mural on Fifth Street", "Rainy Sunday with coffee", "Bus was late again",
    "Library book sale", "High school football under the lights", "First snow on the hills",
    "Dog park chaos", "Bakery on Main finally reopened", "Volunteer cleanup at the creek",
]
BACKGROUND_DETAILS = [
    "and honestly it made my week", "with the whole crew", "legs are toast",
    "still undecided about everything", "far too much cheese", "forty minutes in the rain",
    "who else was there", "ten out of ten would recommend", "photos do not do it justice",
    "already planning the next one", "thanks to everyone who showed up", "not my best idea",
    "can someone explain what Measure R changes for drivers", "the light was unreal",
]
EMOJIS = ["", " 🚌", " 💚", " 🙌", " 👇", " 🗳️", " !!", ""]
FOR_TAGS = ["riversidetransit", "measurer", "riverside"]
AGAINST_TAGS = ["measurer", "riverside", "riversidevotes"]


def _jitter(rng: random.Random, start: datetime, hours: float) -> datetime:
    return start + timedelta(seconds=rng.uniform(0, hours * 3600))


def _post(rng, i, caption, ts, tags):
    return {
        "id": f"demo_{i:05d}", "caption": caption, "timestamp": iso(ts),
        "media_type": rng.choice(["IMAGE", "CAROUSEL_ALBUM", "VIDEO"]),
        "like_count": int(rng.lognormvariate(3, 1)), "comments_count": int(rng.expovariate(0.3)),
        "permalink": None, "tags": tags,
    }


def generate(seed: int = 7) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    posts: list[dict] = []
    i = 0

    def add(caption, ts, tags):
        nonlocal i
        posts.append(_post(rng, i, caption, ts, tags))
        i += 1

    # Campaign FOR: 34 posts in ~8 hours on day 2.
    start = BASE_TIME + timedelta(days=1, hours=8)
    for _ in range(34):
        cap = rng.choice(FOR_TEMPLATE).format(more=rng.choice(["more", "more frequent"]))
        cap += rng.choice(EMOJIS) + " #" + " #".join(rng.sample(FOR_TAGS, 2))
        add(cap, _jitter(rng, start, 8), rng.sample(FOR_TAGS, 2))

    # Campaign AGAINST: 27 posts in ~6 hours on day 4.
    start = BASE_TIME + timedelta(days=3, hours=14)
    for _ in range(27):
        cap = rng.choice(AGAINST_TEMPLATE).format(higher=rng.choice(["Higher", "Higher property"]))
        cap += rng.choice(EMOJIS) + " #" + " #".join(rng.sample(AGAINST_TAGS, 2))
        add(cap, _jitter(rng, start, 6), rng.sample(AGAINST_TAGS, 2))

    # Organic viral quote: 12 posts spread over ~20 hours, under an unrelated tag.
    start = BASE_TIME + timedelta(days=5)
    for _ in range(12):
        add(VIRAL_QUOTE + rng.choice(EMOJIS) + " #riverside", _jitter(rng, start, 20), ["riverside"])

    # Background noise across the week.
    for _ in range(120):
        tag = rng.choice(["riverside", "riversidevotes", "measurer"])
        cap = f"{rng.choice(BACKGROUND_OPENERS)}, {rng.choice(BACKGROUND_DETAILS)}"
        add(cap + rng.choice(EMOJIS), _jitter(rng, BASE_TIME, 24 * 7), [tag])

    ads = [
        {
            "id": "demo_ad_001", "page_id": "demo_page_1",
            "page_name": "Riverside Moves Forward (fictional)",
            "bylines": "Citizens for Riverside Transit (fictional)",
            "spend": {"lower_bound": "5000", "upper_bound": "9999"},
            "impressions": {"lower_bound": "200000", "upper_bound": "249999"},
            "currency": "USD",
            "ad_delivery_start_time": iso(BASE_TIME + timedelta(days=1)),
            "ad_delivery_stop_time": iso(BASE_TIME + timedelta(days=6)),
            "ad_creative_bodies": ["Measure R means more buses, shorter commutes and cleaner air "
                                   "for Riverside families. Vote YES on R."],
            "ad_snapshot_url": None, "publisher_platforms": ["instagram", "facebook"],
        },
        {
            "id": "demo_ad_002", "page_id": "demo_page_2",
            "page_name": "Riverside Taxpayers Alliance (fictional)",
            "bylines": "Riverside Taxpayers Alliance (fictional)",
            "spend": {"lower_bound": "10000", "upper_bound": "14999"},
            "impressions": {"lower_bound": "300000", "upper_bound": "349999"},
            "currency": "USD",
            "ad_delivery_start_time": iso(BASE_TIME + timedelta(days=3)),
            "ad_delivery_stop_time": None,
            "ad_creative_bodies": ["Measure R is a blank cheque: higher taxes, no audit, no "
                                   "guarantee of a single new bus. Vote NO on R."],
            "ad_snapshot_url": None, "publisher_platforms": ["instagram"],
        },
        {
            "id": "demo_ad_003", "page_id": "demo_page_3",
            "page_name": "Riverside Library Friends (fictional)",
            "bylines": "Friends of Riverside Library (fictional)",
            "spend": {"lower_bound": "0", "upper_bound": "99"},
            "impressions": {"lower_bound": "1000", "upper_bound": "4999"},
            "currency": "USD",
            "ad_delivery_start_time": iso(BASE_TIME),
            "ad_delivery_stop_time": iso(BASE_TIME + timedelta(days=7)),
            "ad_creative_bodies": ["Story time every Saturday morning at the Riverside library. "
                                   "Free for all ages."],
            "ad_snapshot_url": None, "publisher_platforms": ["instagram"],
        },
    ]
    return posts, ads


def load(conn: sqlite3.Connection, seed: int = 7) -> tuple[int, int]:
    posts, ads = generate(seed)
    with conn:
        for p in posts:
            for tag in p["tags"]:
                upsert_post(conn, p, tag, "demo", source="demo")
        for a in ads:
            upsert_ad(conn, a, "measure r", source="demo")
    return len(posts), len(ads)
