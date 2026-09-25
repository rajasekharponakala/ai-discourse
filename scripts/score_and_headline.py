#!/usr/bin/env python3
"""
Scoring + headline pass for tracked X posts.

This module is pure (no network) so it can be:
  * imported by ``scrape_x.py`` right after scraping, and
  * run standalone to re-score / re-headline an existing ``data/posts.json``:

      python scripts/score_and_headline.py            # rewrite data/posts.json in place
      python scripts/score_and_headline.py --check    # print ranking, don't write

Score = engagement velocity  x  listicle bonus  x  media bonus
where velocity is weighted engagement divided by a Hacker-News-style age
"gravity" term, so a fresh post with 5k likes can outrank a week-old post
with 20k likes.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
POSTS_PATH = ROOT / "data" / "posts.json"

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How much each interaction type is worth relative to one like.
ENGAGEMENT_WEIGHTS = {
    "likes": 1.0,
    "reposts": 2.5,   # amplification is worth more than a like
    "replies": 1.5,   # conversation / controversy signal
    "bookmarks": 2.0, # "save for later" = strong listicle signal
    "views": 0.01,    # 100 views ~ 1 like
}
AGE_GRAVITY = 0.8          # higher = old posts sink faster
AGE_OFFSET_HOURS = 2.0     # keeps brand-new posts from exploding to infinity
LISTICLE_BONUS = 1.35      # base multiplier for list-style posts
LISTICLE_PER_ITEM = 0.02   # +2% per list item ...
LISTICLE_ITEM_CAP = 10     # ... up to 10 items
MEDIA_BONUS = 1.12         # posts with images/video travel further

# Absolute thresholds for the "viral" / "hot" badges shown in the UI.
VIRAL_LIKES, VIRAL_VIEWS = 10_000, 1_000_000
HOT_LIKES, HOT_VIEWS = 2_000, 200_000

HEADLINE_MAX_CHARS = 110

# ---------------------------------------------------------------------------
# Number / time helpers
# ---------------------------------------------------------------------------

_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


def parse_count(value: Any) -> int:
    """Turn '1.2K', '3,405', '2M', 17, None ... into an int (0 on failure)."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value)) if math.isfinite(value) else 0
    text = str(value).strip().lower().replace(",", "").replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)([kmb])?", text)
    if not m:
        return 0
    return int(float(m.group(1)) * _SUFFIX.get(m.group(2) or "", 1))


def parse_time(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def weighted_engagement(engagement: dict[str, Any]) -> float:
    return sum(parse_count(engagement.get(k)) * w for k, w in ENGAGEMENT_WEIGHTS.items())


def post_age_hours(post: dict[str, Any], now: datetime) -> float:
    """Age from the post timestamp, falling back to when we first saw it."""
    ts = parse_time(post.get("posted_at")) or parse_time(post.get("first_seen_at")) or now
    return max(0.0, (now - ts).total_seconds() / 3600)


def score_post(post: dict[str, Any], now: datetime) -> float:
    eng = post.get("engagement") or {}
    age = post_age_hours(post, now)
    velocity = weighted_engagement(eng) / math.pow(age + AGE_OFFSET_HOURS, AGE_GRAVITY)

    multiplier = 1.0
    if post.get("is_listicle"):
        n_items = min(len(post.get("list_items") or []), LISTICLE_ITEM_CAP)
        multiplier *= LISTICLE_BONUS + LISTICLE_PER_ITEM * n_items
    if post.get("media_urls"):
        multiplier *= MEDIA_BONUS

    # log-compress so scores are human-readable (roughly 0..100)
    return round(20 * math.log10(1 + velocity * multiplier), 2)


def engagement_tier(engagement: dict[str, Any]) -> str:
    likes = parse_count(engagement.get("likes"))
    views = parse_count(engagement.get("views"))
    if likes >= VIRAL_LIKES or views >= VIRAL_VIEWS:
        return "viral"
    if likes >= HOT_LIKES or views >= HOT_VIEWS:
        return "hot"
    return "normal"


# ---------------------------------------------------------------------------
# Headlines
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+")
_HASHTAG_RE = re.compile(r"(?:^|\s)#[A-Za-z_]\w*")
_MENTION_LEAD_RE = re.compile(r"^(?:@\w+\s*)+")
# Broad emoji / pictograph ranges; headlines should read clean.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "️‍"
    "]+"
)
_CLICKBAIT_PREFIX_RE = re.compile(r"^(?:headline|title)\s*[:\-]\s*", re.I)


def _truncate(text: str, limit: int = HEADLINE_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-–— ")
    return cut + "…"


def clean_headline(raw: str | None, keep_case: Iterable[str] = ()) -> str:
    """Normalise a model-written headline into a clean one-liner.

    ``keep_case`` holds words (e.g. the author's name) whose capitalisation
    should survive de-shouting.
    """
    if not raw:
        return ""
    text = str(raw)
    text = _URL_RE.sub("", text)
    text = _HASHTAG_RE.sub(" ", text)
    text = _EMOJI_RE.sub("", text)
    text = _CLICKBAIT_PREFIX_RE.sub("", text.strip())
    text = re.sub(r"\s+", " ", text).strip().strip("*_ ")
    # Drop quotes only when they wrap the entire headline.
    if len(text) > 2 and text[0] in "\"'“‘" and text[-1] in "\"'”’" and text.count(text[0]) <= 2:
        text = text[1:-1].strip()
    text = text.rstrip(".")
    # De-shout: if the whole thing is caps, sentence-case it (keeping names).
    letters = [c for c in text if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        keep = {w.lower(): w for w in keep_case}
        words = text.lower().split(" ")
        words = [_restore_case(w, keep) for w in words]
        text = " ".join(words)
        text = text[:1].upper() + text[1:]
    return _truncate(text)


def _restore_case(word: str, keep: dict[str, str]) -> str:
    m = re.match(r"^(\W*)([\w-]+)(.*)$", word)
    if not m or m.group(2) not in keep:
        return word
    return m.group(1) + keep[m.group(2)] + m.group(3)


def _first_sentence(text: str) -> str:
    text = _MENTION_LEAD_RE.sub("", _URL_RE.sub("", text or "")).strip()
    text = _EMOJI_RE.sub("", text)
    first = re.split(r"(?<=[.!?])\s+|\n+", text, maxsplit=1)[0]
    return re.sub(r"\s+", " ", first).strip().rstrip(":")


def _display_name(post: dict[str, Any]) -> str:
    name = (post.get("author_name") or "").strip()
    return name or "@" + (post.get("author_handle") or "someone")


def fallback_headline(post: dict[str, Any]) -> str:
    """Deterministic headline when the extractor didn't give us a usable one."""
    name = _display_name(post)
    lead = _first_sentence(post.get("post_text") or "")
    items = post.get("list_items") or []
    tier = engagement_tier(post.get("engagement") or {})

    if post.get("is_listicle") and items:
        topic = _truncate(lead, 60) if lead else "the list everyone is saving"
        return clean_headline(f"{name} shares {len(items)} takeaways: {topic}")
    if not lead:
        return clean_headline(f"{name} just posted something the timeline can't ignore")
    if tier == "viral":
        return clean_headline(f"{name} goes viral: “{_truncate(lead, 70)}”")
    return clean_headline(f"{name}: “{_truncate(lead, 80)}”")


def ensure_headline(post: dict[str, Any]) -> str:
    names = (post.get("author_name") or "").split() + [post.get("author_handle") or ""]
    cleaned = clean_headline(post.get("sensational_headline"), keep_case=[n for n in names if n])
    # Reject empty or trivially short headlines.
    if len(cleaned) < 12:
        return fallback_headline(post)
    return cleaned


# ---------------------------------------------------------------------------
# Batch pass
# ---------------------------------------------------------------------------


def rank_posts(
    posts: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    keep_top: int = 60,
    max_age_days: float = 14,
) -> list[dict[str, Any]]:
    """Score, headline, filter and sort posts. Returns a new list."""
    now = now or datetime.now(timezone.utc)
    ranked: list[dict[str, Any]] = []
    for post in posts:
        if post_age_hours(post, now) > max_age_days * 24:
            continue
        if not (post.get("post_text") or "").strip():
            continue
        post = dict(post)
        post["sensational_headline"] = ensure_headline(post)
        post["engagement_tier"] = engagement_tier(post.get("engagement") or {})
        post["score"] = score_post(post, now)
        ranked.append(post)

    ranked.sort(key=lambda p: p["score"], reverse=True)
    return ranked[:keep_top]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=POSTS_PATH)
    parser.add_argument("--output", type=Path, default=None, help="defaults to --input")
    parser.add_argument("--keep-top", type=int, default=60)
    parser.add_argument("--max-age-days", type=float, default=14)
    parser.add_argument("--check", action="store_true", help="print ranking without writing")
    args = parser.parse_args(argv)

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    ranked = rank_posts(payload.get("posts", []), keep_top=args.keep_top, max_age_days=args.max_age_days)

    for i, p in enumerate(ranked, 1):
        flag = "L" if p.get("is_listicle") else " "
        print(f"{i:>3}. {p['score']:>6.2f} {flag} @{p.get('author_handle', '?'):<16} {p['sensational_headline']}")

    if not args.check:
        payload["posts"] = ranked
        payload["generated_at"] = iso(datetime.now(timezone.utc))
        out = args.output or args.input
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(ranked)} posts -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
