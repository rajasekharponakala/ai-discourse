#!/usr/bin/env python3
"""
Scrape high-engagement X posts from tracked AI/tech accounts using Firecrawl.

Pipeline (one run):
  1. Load ``accounts.json`` and the previous ``data/posts.json`` + ``data/scrape_state.json``.
  2. Pick the ``accounts_per_run`` least-recently-scraped accounts (rotation keeps
     credit usage flat no matter how long the account list gets).
  3. For each account scrape its explicit ``post_urls`` (cheapest, most reliable)
     and then its profile URL, requesting both ``markdown`` and structured ``json``.
  4. Normalise + validate every extracted post, merge with previously seen posts
     (engagement numbers are refreshed, first_seen_at is kept).
  5. Score + headline via ``score_and_headline.rank_posts`` and write the top N.

A single bad URL / account never fails the run: errors are logged, counted and
written to ``data/scrape_state.json``. The process only exits non-zero when the
API key is missing (misconfiguration you want to notice).

Usage:
  FIRECRAWL_API=fc-... python scripts/scrape_x.py
  python scripts/scrape_x.py --only karpathy,rasbt --max-posts 2
  python scripts/scrape_x.py --fixture scripts/fixtures/sample_extraction.json   # offline test
  python scripts/scrape_x.py --dry-run          # print what would be scraped
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_and_headline import iso, parse_count, parse_time, rank_posts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_PATH = ROOT / "accounts.json"
POSTS_PATH = ROOT / "data" / "posts.json"
STATE_PATH = ROOT / "data" / "scrape_state.json"

log = logging.getLogger("scrape_x")

DEFAULT_SETTINGS = {
    "max_posts_per_account": 3,
    "accounts_per_run": 6,
    "keep_top_posts": 60,
    "max_post_age_days": 14,
    "cache_max_age_minutes": 60,
    "scrape_timeout_ms": 120_000,
}

# ---------------------------------------------------------------------------
# Firecrawl extraction contract
# ---------------------------------------------------------------------------

POST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "post_url": {"type": "string", "description": "Canonical https://x.com/<handle>/status/<id> URL"},
                    "author_handle": {"type": "string", "description": "Handle without @"},
                    "author_name": {"type": "string"},
                    "post_text": {"type": "string", "description": "Full, verbatim post text"},
                    "posted_at": {"type": "string", "description": "ISO-8601 UTC timestamp"},
                    "is_repost": {"type": "boolean"},
                    "is_reply": {"type": "boolean"},
                    "engagement": {
                        "type": "object",
                        "properties": {
                            "likes": {"type": "number"},
                            "reposts": {"type": "number"},
                            "replies": {"type": "number"},
                            "views": {"type": "number"},
                            "bookmarks": {"type": "number"},
                        },
                        "required": ["likes", "reposts", "replies", "views", "bookmarks"],
                    },
                    "is_listicle": {"type": "boolean"},
                    "list_items": {"type": "array", "items": {"type": "string"}},
                    "media_urls": {"type": "array", "items": {"type": "string"}},
                    "sensational_headline": {"type": "string"},
                },
                "required": [
                    "post_url",
                    "author_handle",
                    "author_name",
                    "post_text",
                    "engagement",
                    "is_listicle",
                    "list_items",
                    "media_urls",
                    "sensational_headline",
                ],
            },
        }
    },
    "required": ["posts"],
}


def build_prompt(handle: str, max_posts: int, single_post: bool) -> str:
    """Tight, rule-based extraction prompt. Kept deterministic for reliability."""
    scope = (
        "Extract exactly the ONE main post at this URL (ignore replies and quoted context below it)."
        if single_post
        else (
            f"Extract up to {max_posts} ORIGINAL posts authored by @{handle} from this profile, "
            "choosing the ones with the HIGHEST engagement from roughly the last 7 days. "
            "Skip pure reposts/retweets and replies to other users. Skip pinned posts older than 7 days."
        )
    )
    return f"""{scope}

Return JSON matching the schema. Rules for every post:
- post_url: canonical https://x.com/<handle>/status/<numeric id>. Never invent an id; if unknown use "".
- author_handle: without "@". author_name: display name as shown.
- post_text: the COMPLETE verbatim text (expand "Show more"; include every line and list item). No summaries.
- posted_at: ISO-8601 UTC (e.g. 2026-01-31T14:05:00Z); "" if unknown.
- engagement: plain integers. Convert "1.2K" -> 1200, "3.4M" -> 3400000. Use 0 when a metric is not shown.
- is_listicle: true only if the post is structured as a list of 3+ items (numbered, bulleted, emoji-bullets or line-by-line tips/resources/lessons), or a thread opener promising "N things/lessons/tips".
- list_items: each list item as a short clean string, in order ([] if not a listicle).
- media_urls: direct image/video URLs attached to the post (pbs.twimg.com / video.twimg.com); [] if none.
- sensational_headline: ONE punchy, X-optimised headline (max 90 chars) that makes people click, written about the post in third person, naming the author (e.g. "Karpathy's 7 rules for training LLMs that nobody talks about").
  It MUST be faithful to the post: no invented facts, numbers or quotes; no hashtags, no emojis, no ALL CAPS, no trailing period.
If nothing matching is visible, return {{"posts": []}}."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATUS_URL_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)", re.I)


def canonical_status(url: str | None) -> tuple[str, str] | None:
    """Return (handle, status_id) for a valid X status URL."""
    if not url:
        return None
    m = STATUS_URL_RE.search(url)
    return (m.group(1), m.group(2)) if m else None


def profile_url(handle: str) -> str:
    return f"https://x.com/{handle}"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        log.warning("could not parse %s (%s); starting fresh", path, exc)
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic: never leave a half-written posts.json


def load_accounts(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = load_json(path, {})
    settings = {**DEFAULT_SETTINGS, **(raw.get("settings") or {})}
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw.get("accounts", []):
        # Allow plain strings ("karpathy") or objects ({"handle": "karpathy", ...}).
        acct = {"handle": entry} if isinstance(entry, str) else dict(entry)
        handle = str(acct.get("handle", "")).lstrip("@").strip()
        if not handle or handle.lower() in seen or acct.get("enabled") is False:
            continue
        seen.add(handle.lower())
        acct["handle"] = handle
        acct.setdefault("post_urls", [])
        accounts.append(acct)
    return settings, accounts


def pick_accounts(accounts: list[dict[str, Any]], state: dict[str, Any], n: int) -> list[dict[str, Any]]:
    """Least-recently-scraped first; accounts with explicit post_urls always included."""
    last = state.get("last_scraped", {})
    ordered = sorted(accounts, key=lambda a: last.get(a["handle"].lower(), ""))
    chosen = ordered[: max(0, n)]
    for acct in accounts:
        if acct.get("post_urls") and acct not in chosen:
            chosen.append(acct)
    return chosen


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(getattr(obj, "__dict__", {}))


def normalize_post(raw: dict[str, Any], acct: dict[str, Any], source_url: str, now: datetime) -> dict[str, Any] | None:
    """Validate + clean one extracted post. Returns None for junk."""
    text = str(raw.get("post_text") or "").strip()
    if len(text) < 3:
        return None

    handle = str(raw.get("author_handle") or acct["handle"]).lstrip("@").strip()
    status = canonical_status(raw.get("post_url")) or canonical_status(source_url)
    if status:
        handle = status[0] if status[0].lower() == handle.lower() else handle
        post_id = status[1]
        url = f"https://x.com/{status[0]}/status/{status[1]}"
    else:
        # No status id: keep the post but key it by content so re-scrapes merge.
        post_id = "h" + hashlib.sha1(f"{handle.lower()}|{text[:280]}".encode()).hexdigest()[:16]
        url = profile_url(handle)

    # Drop posts by someone else (reposts surfaced on the profile page).
    if handle.lower() != acct["handle"].lower() or raw.get("is_repost"):
        return None

    eng_raw = _as_dict(raw.get("engagement"))
    engagement = {k: parse_count(eng_raw.get(k)) for k in ("likes", "reposts", "replies", "views", "bookmarks")}

    list_items = [re.sub(r"^\s*(?:\d+[.)]|[-•*▪︎→])\s*", "", str(i)).strip() for i in raw.get("list_items") or []]
    list_items = [i for i in list_items if i]
    media = [
        str(u).strip()
        for u in raw.get("media_urls") or []
        if isinstance(u, str) and u.startswith("http") and "profile_images" not in u
    ]

    posted_at = parse_time(raw.get("posted_at"))
    if posted_at and posted_at > now:  # model hallucinated a future date
        posted_at = None

    return {
        "id": post_id,
        "url": url,
        "author_handle": handle,
        "author_name": str(raw.get("author_name") or acct.get("name") or handle).strip(),
        "post_text": text,
        "posted_at": iso(posted_at) if posted_at else None,
        "engagement": engagement,
        "is_listicle": bool(raw.get("is_listicle")) and len(list_items) >= 2,
        "list_items": list_items,
        "media_urls": list(dict.fromkeys(media))[:4],
        "sensational_headline": str(raw.get("sensational_headline") or "").strip(),
        "tags": acct.get("tags", []),
        "is_reply": bool(raw.get("is_reply")),
        "first_seen_at": iso(now),
        "last_scraped_at": iso(now),
    }


def merge_posts(existing: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fresh data wins for engagement/text; keep the earliest first_seen_at."""
    by_id: dict[str, dict[str, Any]] = {p["id"]: p for p in existing if p.get("id") and not p.get("sample")}
    for post in fresh:
        old = by_id.get(post["id"])
        if old:
            post["first_seen_at"] = old.get("first_seen_at") or post["first_seen_at"]
            post["posted_at"] = post.get("posted_at") or old.get("posted_at")
            # Engagement only goes up; guard against a partial / truncated extraction.
            post["engagement"] = {
                k: max(parse_count(post["engagement"].get(k)), parse_count((old.get("engagement") or {}).get(k)))
                for k in post["engagement"]
            }
            if not post["media_urls"]:
                post["media_urls"] = old.get("media_urls", [])
        by_id[post["id"]] = post
    return list(by_id.values())


# ---------------------------------------------------------------------------
# Firecrawl client
# ---------------------------------------------------------------------------


class Scraper:
    """Thin wrapper around the Firecrawl SDK with retries + fixture support."""

    def __init__(self, api_key: str | None, settings: dict[str, Any], fixture: dict[str, Any] | None = None):
        self.settings = settings
        self.fixture = fixture
        self.client = None
        if fixture is None:
            from firecrawl import Firecrawl  # imported lazily so --fixture works without the SDK

            self.client = Firecrawl(api_key=api_key)

    def scrape(self, url: str, prompt: str) -> tuple[list[dict[str, Any]], str]:
        """Return (raw posts, markdown). Raises on hard failure after retries."""
        if self.fixture is not None:
            entry = self.fixture.get(url) or self.fixture.get(url.rstrip("/").lower()) or {"posts": []}
            return list(entry.get("posts", [])), entry.get("markdown", "")

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                doc = self.client.scrape(
                    url,
                    formats=[
                        "markdown",
                        {"type": "json", "prompt": prompt, "schema": POST_SCHEMA},
                    ],
                    only_main_content=True,
                    timeout=int(self.settings["scrape_timeout_ms"]),
                    # Re-use Firecrawl's cache for recently scraped URLs -> saves credits.
                    max_age=int(self.settings["cache_max_age_minutes"]) * 60_000,
                )
                data = _as_dict(getattr(doc, "json", None))
                posts = data.get("posts") or []
                return [_as_dict(p) for p in posts if p], getattr(doc, "markdown", "") or ""
            except Exception as exc:  # SDK raises various error types; retry all of them
                last_exc = exc
                msg = str(exc)
                # Don't burn retries on auth / payment / bad-request problems.
                if any(code in msg for code in ("401", "402", "403", "400")):
                    break
                wait = 2**attempt
                log.warning("  attempt %d for %s failed: %s (retrying in %ss)", attempt, url, msg[:200], wait)
                time.sleep(wait)
        raise RuntimeError(f"scrape failed for {url}: {last_exc}")


def scrape_account(scraper: Scraper, acct: dict[str, Any], settings: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    handle = acct["handle"]
    max_posts = int(acct.get("max_posts") or settings["max_posts_per_account"])
    posts: list[dict[str, Any]] = []
    errors: list[str] = []

    targets = [(u, True) for u in acct.get("post_urls", []) if canonical_status(u)]
    if not acct.get("skip_profile"):
        targets.append((profile_url(handle), False))

    for url, single in targets:
        try:
            raw_posts, markdown = scraper.scrape(url, build_prompt(handle, max_posts, single))
            kept = [p for p in (normalize_post(r, acct, url if single else "", now) for r in raw_posts) if p]
            if not single:
                kept = kept[:max_posts]
            log.info("  %-45s -> %d raw, %d kept (%d chars md)", url, len(raw_posts), len(kept), len(markdown))
            posts.extend(kept)
        except Exception as exc:
            log.error("  %s: %s", url, exc)
            errors.append(f"{url}: {str(exc)[:300]}")
    return posts, errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated handles to scrape (ignores rotation)")
    parser.add_argument("--accounts-per-run", type=int, help="override settings.accounts_per_run")
    parser.add_argument("--max-posts", type=int, help="override settings.max_posts_per_account")
    parser.add_argument("--fixture", type=Path, help="JSON map of url -> {posts, markdown}; no API calls")
    parser.add_argument("--dry-run", action="store_true", help="show selected accounts and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    # The SDK logs at INFO; keep our output readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings, accounts = load_accounts(ACCOUNTS_PATH)
    if args.accounts_per_run is not None:
        settings["accounts_per_run"] = args.accounts_per_run
    if args.max_posts is not None:
        settings["max_posts_per_account"] = args.max_posts

    state = load_json(STATE_PATH, {"last_scraped": {}, "runs": []})
    state.setdefault("last_scraped", {})

    if args.only:
        wanted = {h.strip().lstrip("@").lower() for h in args.only.split(",") if h.strip()}
        selected = [a for a in accounts if a["handle"].lower() in wanted]
        known = {a["handle"].lower() for a in accounts}
        selected += [{"handle": h, "post_urls": []} for h in wanted - known]
    else:
        selected = pick_accounts(accounts, state, int(settings["accounts_per_run"]))

    log.info("tracking %d accounts; scraping %d this run: %s", len(accounts), len(selected), ", ".join(a["handle"] for a in selected))
    if args.dry_run:
        return 0

    fixture = load_json(args.fixture, {}) if args.fixture else None
    api_key = os.environ.get("FIRECRAWL_API") or os.environ.get("FIRECRAWL_API_KEY")
    if fixture is None and not api_key:
        log.error("FIRECRAWL_API env var is not set (GitHub secret 'FIRECRAWL_API').")
        return 2

    scraper = Scraper(api_key, settings, fixture)
    now = datetime.now(timezone.utc)
    fresh: list[dict[str, Any]] = []
    errors: list[str] = []
    ok_accounts = 0

    for acct in selected:
        log.info("@%s", acct["handle"])
        posts, errs = scrape_account(scraper, acct, settings, now)
        fresh.extend(posts)
        errors.extend(errs)
        if not errs or posts:
            ok_accounts += 1
            state["last_scraped"][acct["handle"].lower()] = iso(now)

    previous = load_json(POSTS_PATH, {"posts": []})
    merged = merge_posts(previous.get("posts", []), fresh)
    ranked = rank_posts(
        merged,
        now=now,
        keep_top=int(settings["keep_top_posts"]),
        max_age_days=float(settings["max_post_age_days"]),
    )

    if not fresh and not [p for p in previous.get("posts", []) if not p.get("sample")]:
        # Nothing real yet (e.g. every scrape failed on the very first run):
        # keep the sample file so the site still renders.
        log.warning("no posts scraped and no previous real data; leaving %s untouched", POSTS_PATH.name)
    else:
        write_json(
            POSTS_PATH,
            {
                "generated_at": iso(now),
                "source": "firecrawl",
                "tracked_accounts": [
                    {"handle": a["handle"], "name": a.get("name", a["handle"]), "tags": a.get("tags", [])} for a in accounts
                ],
                "posts": ranked,
            },
        )

    run_summary = {
        "at": iso(now),
        "accounts": [a["handle"] for a in selected],
        "ok_accounts": ok_accounts,
        "fresh_posts": len(fresh),
        "published_posts": len(ranked),
        "errors": errors[:20],
    }
    state["runs"] = ([run_summary] + state.get("runs", []))[:20]
    write_json(STATE_PATH, state)

    log.info("done: %d/%d accounts ok, %d fresh posts, %d published, %d errors", ok_accounts, len(selected), len(fresh), len(ranked), len(errors))
    for e in errors:
        # GitHub Actions annotation: visible in the run summary without failing it.
        print(f"::warning title=Firecrawl scrape failed::{e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
