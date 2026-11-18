"""Caption normalisation and disclosure-marker detection.

The same normalisation is applied to every post and every ad, whoever posted it.
"""

from __future__ import annotations

import re
import unicodedata

URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
HASHTAG_RE = re.compile(r"#[\w_]+", re.UNICODE)
MENTION_RE = re.compile(r"@[\w._]+", re.UNICODE)
HASHTAG_CAPTURE_RE = re.compile(r"#([\w_]+)", re.UNICODE)

# Markers that a post may be paid or sponsored. The Graph API hashtag endpoints do not
# expose Instagram's "Paid partnership" label, so we can only look for self-disclosure
# in the caption text. Absence of a marker proves nothing.
DISCLOSURE_PATTERNS = {
    "#ad": re.compile(r"(?<!\w)#ad(?!\w)", re.IGNORECASE),
    "#sponsored": re.compile(r"(?<!\w)#sponsored(?!\w)", re.IGNORECASE),
    "#partner": re.compile(r"(?<!\w)#(partner|paidpartner|paidpartnership)(?!\w)", re.IGNORECASE),
    "paid partnership": re.compile(r"paid partnership", re.IGNORECASE),
    "paid for by": re.compile(r"paid for by", re.IGNORECASE),
    "sponsored by": re.compile(r"sponsored by", re.IGNORECASE),
}


def normalize(text: str | None) -> str:
    """Lowercase; strip links, hashtags, mentions, emojis and punctuation; collapse spaces.

    Letters and digits from any script are kept, so non-English captions still compare.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = URL_RE.sub(" ", text)
    text = HASHTAG_RE.sub(" ", text)
    text = MENTION_RE.sub(" ", text)
    text = text.lower()
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            kept.append(ch)
        elif ch == "'" or ch == "’":
            continue  # "don't" -> "dont"
        else:
            kept.append(" ")  # emojis, punctuation, symbols, whitespace
    return " ".join("".join(kept).split())


def tokens(text_norm: str) -> list[str]:
    return text_norm.split()


def hashtags_in(text: str | None) -> list[str]:
    if not text:
        return []
    return sorted({h.lower() for h in HASHTAG_CAPTURE_RE.findall(text)})


def disclosure_markers(text: str | None) -> list[str]:
    if not text:
        return []
    return [name for name, pat in DISCLOSURE_PATTERNS.items() if pat.search(text)]
