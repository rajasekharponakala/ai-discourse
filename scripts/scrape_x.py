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
import threading
import math
import time
from concurrent.futures import ThreadPoolExecutor
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
    "max_concurrency": 4,
    "max_requests_per_minute": 10,
    # Credit budget: spread remaining Firecrawl credits evenly over the billing period.
    "credit_budget": True,
    "credit_reserve_pct": 10,
    "run_interval_hours": 2,
    "initial_credits_per_scrape": 10,
    "fallback_accounts_per_run": 6,
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


def parse_per_run(value: Any, total: int) -> int:
    """accounts_per_run may be a number, or "all" / 0 / null for every account."""
    if value in (None, "", 0, "0") or str(value).strip().lower() == "all":
        return total
    return max(1, int(value))


def scrape_targets(acct: dict[str, Any]) -> list[tuple[str, bool]]:
    """(url, is_single_post) pairs to scrape for an account."""
    targets = [(u, True) for u in acct.get("post_urls", []) if canonical_status(u)]
    if not acct.get("skip_profile"):
        targets.append((profile_url(acct["handle"]), False))
    return targets


def pick_accounts(
    accounts: list[dict[str, Any]],
    state: dict[str, Any],
    n: int,
    credit_budget: float | None = None,
    credits_per_scrape: float = 1.0,
) -> list[dict[str, Any]]:
    """Least-recently-scraped first, up to ``n`` accounts and (if given) ``credit_budget``.

    Never-scraped accounts sort first (""), so new additions are picked up
    immediately, and accounts skipped by an exhausted budget go first next run.
    """
    last = state.get("last_scraped", {})
    ordered = sorted(accounts, key=lambda a: last.get(a["handle"].lower(), ""))
    chosen: list[dict[str, Any]] = []
    spent = 0.0
    for acct in ordered:
        if len(chosen) >= n:
            break
        cost = len(scrape_targets(acct)) * credits_per_scrape
        if credit_budget is not None and spent + cost > credit_budget:
            break
        chosen.append(acct)
        spent += cost
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


class OutOfCredits(RuntimeError):
    """Firecrawl returned 402: nothing else in this run can succeed."""


class Scraper:
    """Thin wrapper around the Firecrawl SDK with pacing, retries + fixture support.

    Thread-safe: requests from all worker threads share one pacer so the run
    stays under ``max_requests_per_minute`` (Firecrawl rate-limits per plan).
    """

    MAX_ERROR_RETRIES = 3
    MAX_RATE_LIMIT_RETRIES = 6

    def __init__(self, api_key: str | None, settings: dict[str, Any], fixture: dict[str, Any] | None = None):
        self.settings = settings
        self.fixture = fixture
        self.client = None
        self.out_of_credits = False
        self.credits_spent = 0.0      # reported by Firecrawl per scrape (metadata.credits_used)
        self.successful_scrapes = 0
        self.metered_scrapes = 0      # scrapes that reported credits_used
        self._credit_lock = threading.Lock()
        self.run_budget: float | None = None  # set by main() in budget mode
        rpm = float(settings.get("max_requests_per_minute") or 0)
        self._min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._pace_lock = threading.Lock()
        self._next_slot = 0.0
        if fixture is None:
            from firecrawl import Firecrawl  # imported lazily so --fixture works without the SDK

            self.client = Firecrawl(api_key=api_key)

    def _record_cost(self, doc: Any) -> None:
        used = getattr(getattr(doc, "metadata", None), "credits_used", None)
        with self._credit_lock:
            self.successful_scrapes += 1
            if isinstance(used, (int, float)) and used >= 0:
                self.credits_spent += float(used)
                self.metered_scrapes += 1

    def credit_usage(self) -> dict[str, Any] | None:
        """Remaining credits + billing period from Firecrawl, or None if unavailable."""
        if self.client is None:
            return None
        try:
            usage = _as_dict(self.client.get_credit_usage())
        except Exception as exc:
            log.warning("could not read Firecrawl credit usage: %s", str(exc)[:200])
            return None
        if usage.get("remaining_credits") is None:
            return None
        return usage

    def _wait_for_slot(self) -> None:
        """Space request starts evenly across all threads."""
        if not self._min_interval:
            return
        with self._pace_lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._min_interval
        if slot > now:
            time.sleep(slot - now)

    def _push_back(self, seconds: float) -> None:
        """After a 429, delay every thread's next request, not just this one."""
        with self._pace_lock:
            self._next_slot = max(self._next_slot, time.monotonic() + seconds)

    def scrape(self, url: str, prompt: str) -> tuple[list[dict[str, Any]], str]:
        """Return (raw posts, markdown). Raises on hard failure after retries."""
        if self.fixture is not None:
            entry = self.fixture.get(url) or self.fixture.get(url.rstrip("/").lower()) or {"posts": []}
            return list(entry.get("posts", [])), entry.get("markdown", "")
        if self.out_of_credits:
            raise OutOfCredits("skipped: Firecrawl credits exhausted earlier in this run")

        errors = rate_limited = 0
        while True:
            self._wait_for_slot()
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
                self._record_cost(doc)
                data = _as_dict(getattr(doc, "json", None))
                posts = data.get("posts") or []
                return [_as_dict(p) for p in posts if p], getattr(doc, "markdown", "") or ""
            except Exception as exc:  # SDK raises various error types
                msg = str(exc)
                if "402" in msg or "Payment Required" in msg or "Insufficient credits" in msg:
                    self.out_of_credits = True
                    raise OutOfCredits(f"Firecrawl credits exhausted: {msg[:200]}") from exc
                # Don't burn retries on auth / bad-request problems.
                if any(code in msg for code in ("401", "403", "400")):
                    raise RuntimeError(f"scrape failed for {url}: {msg}") from exc
                if "429" in msg or "Rate limit" in msg or "Rate Limit" in msg:
                    rate_limited += 1
                    if rate_limited > self.MAX_RATE_LIMIT_RETRIES:
                        raise RuntimeError(f"scrape failed for {url}: still rate limited: {msg}") from exc
                    m = re.search(r"retry after (\d+)\s*s", msg)
                    wait = min(int(m.group(1)) + 2, 65) if m else 30
                    log.info("  rate limited on %s; waiting %ss", url, wait)
                    self._push_back(wait)
                    continue
                errors += 1
                if errors >= self.MAX_ERROR_RETRIES:
                    raise RuntimeError(f"scrape failed for {url}: {msg}") from exc
                wait = 2**errors
                log.warning("  attempt %d for %s failed: %s (retrying in %ss)", errors, url, msg[:200], wait)
                time.sleep(wait)


def scrape_account(scraper: Scraper, acct: dict[str, Any], settings: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    handle = acct["handle"]
    if scraper.out_of_credits:
        return [], [f"{profile_url(handle)}: skipped: Firecrawl credits exhausted"]
    if scraper.run_budget is not None and scraper.credits_spent >= scraper.run_budget:
        # Real costs came in above the estimate: stop at this run's budget.
        return [], [f"{profile_url(handle)}: skipped: run credit budget reached"]
    log.info("@%s", handle)
    max_posts = int(acct.get("max_posts") or settings["max_posts_per_account"])
    posts: list[dict[str, Any]] = []
    errors: list[str] = []

    for url, single in scrape_targets(acct):
        try:
            raw_posts, markdown = scraper.scrape(url, build_prompt(handle, max_posts, single))
            kept = [p for p in (normalize_post(r, acct, url if single else "", now) for r in raw_posts) if p]
            if not single:
                kept = kept[:max_posts]
            log.info("  [%s] %-40s -> %d raw, %d kept (%d chars md)", handle, url, len(raw_posts), len(kept), len(markdown))
            posts.extend(kept)
        except OutOfCredits as exc:
            errors.append(f"{url}: {exc}")
            break
        except Exception as exc:
            log.error("  %s: %s", url, exc)
            errors.append(f"{url}: {str(exc)[:300]}")
    return posts, errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Credit budget
# ---------------------------------------------------------------------------
#
# Each run gets an equal share of the credits left in the billing period:
#
#     spendable   = remaining_credits - reserve
#     runs_left   = hours until billing_period_end / run_interval_hours
#     allowance   = spendable / runs_left
#
# The allowance is added to a "bank" in data/scrape_state.json and the run
# spends at most what is in the bank. Small plans therefore scrape a few
# accounts every few runs instead of nothing at all, and a big top-up is
# spread out instead of burned in one run. The average cost of an X scrape
# is measured from Firecrawl's per-request credits_used and kept as a moving
# average, so the planner adapts to real prices.


def plan_budget(scraper: "Scraper | None", settings: dict[str, Any], state: dict[str, Any], now: datetime) -> dict[str, Any]:
    per_scrape = float(state.get("avg_credits_per_scrape") or settings["initial_credits_per_scrape"])
    usage = scraper.credit_usage() if scraper else None
    if usage is None:
        return {"mode": "fallback", "credits_per_scrape": per_scrape}

    remaining = float(usage.get("remaining_credits") or 0)
    plan_credits = float(usage.get("plan_credits") or 0)
    end = parse_time(str(usage.get("billing_period_end") or ""))
    hours_left = (end - now).total_seconds() / 3600 if end and end > now else 30 * 24
    interval = max(0.25, float(settings["run_interval_hours"]))
    runs_left = max(1, math.ceil(hours_left / interval))

    reserve = (plan_credits or remaining) * float(settings["credit_reserve_pct"]) / 100
    spendable = max(0.0, remaining - reserve)
    allowance = spendable / runs_left
    # Bank never exceeds what is actually spendable (e.g. after credits ran out).
    bank = min(float(state.get("credit_bank") or 0) + allowance, spendable)
    return {
        "mode": "budget",
        "remaining_credits": remaining,
        "plan_credits": plan_credits,
        "billing_period_end": iso(end) if end else None,
        "runs_left": runs_left,
        "reserve": reserve,
        "allowance": allowance,
        "bank": bank,
        "run_budget": bank,
        "credits_per_scrape": per_scrape,
    }


def settle_budget(scraper: "Scraper", budget: dict[str, Any], state: dict[str, Any]) -> None:
    """Deduct what this run spent from the bank and update the cost estimate."""
    per_scrape = budget["credits_per_scrape"]
    if not scraper.metered_scrapes and scraper.successful_scrapes:
        # No per-request cost reported: measure it from the account balance instead.
        after = scraper.credit_usage()
        if after is not None:
            diff = budget["remaining_credits"] - float(after.get("remaining_credits") or 0)
            if diff > 0:
                scraper.credits_spent = diff
                scraper.metered_scrapes = scraper.successful_scrapes
    if scraper.metered_scrapes:
        measured = scraper.credits_spent / scraper.metered_scrapes
        per_scrape = round(0.7 * per_scrape + 0.3 * measured, 3) if state.get("avg_credits_per_scrape") else measured
        spent = scraper.credits_spent + (scraper.successful_scrapes - scraper.metered_scrapes) * per_scrape
    else:
        spent = scraper.successful_scrapes * per_scrape
    state["avg_credits_per_scrape"] = per_scrape
    state["credit_bank"] = round(max(0.0, budget["bank"] - spent), 3)
    log.info("credits: spent ~%.1f this run, avg %.2f/scrape, bank now %.1f", spent, per_scrape, state["credit_bank"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated handles to scrape (ignores rotation)")
    parser.add_argument("--accounts-per-run", help='override settings.accounts_per_run (number or "all")')
    parser.add_argument("--max-posts", type=int, help="override settings.max_posts_per_account")
    parser.add_argument("--fixture", type=Path, help="JSON map of url -> {posts, markdown}; no API calls")
    parser.add_argument("--dry-run", action="store_true", help="show selected accounts and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    # The SDK logs at INFO; keep our output readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings, accounts = load_accounts(ACCOUNTS_PATH)
    if args.accounts_per_run:
        settings["accounts_per_run"] = args.accounts_per_run
    if args.max_posts is not None:
        settings["max_posts_per_account"] = args.max_posts

    state = load_json(STATE_PATH, {"last_scraped": {}, "runs": []})
    state.setdefault("last_scraped", {})
    now = datetime.now(timezone.utc)

    fixture = load_json(args.fixture, {}) if args.fixture else None
    api_key = os.environ.get("FIRECRAWL_API") or os.environ.get("FIRECRAWL_API_KEY")
    if fixture is None and not api_key and not args.dry_run:
        log.error("FIRECRAWL_API env var is not set (GitHub secret 'FIRECRAWL_API').")
        return 2
    scraper = Scraper(api_key, settings, fixture) if (fixture is not None or api_key) else None

    budget: dict[str, Any] = {"mode": "unlimited"}
    if args.only:
        wanted = {h.strip().lstrip("@").lower() for h in args.only.split(",") if h.strip()}
        selected = [a for a in accounts if a["handle"].lower() in wanted]
        known = {a["handle"].lower() for a in accounts}
        selected += [{"handle": h, "post_urls": []} for h in wanted - known]
    else:
        cap = parse_per_run(settings["accounts_per_run"], len(accounts))
        budget = plan_budget(scraper, settings, state, now) if settings.get("credit_budget") else budget
        if budget["mode"] == "fallback":
            cap = min(cap, int(settings["fallback_accounts_per_run"]))
        selected = pick_accounts(
            accounts,
            state,
            cap,
            credit_budget=budget.get("run_budget"),
            credits_per_scrape=budget.get("credits_per_scrape", 1.0),
        )

    log.info("budget: %s", json.dumps(budget))
    log.info("tracking %d accounts; scraping %d this run: %s", len(accounts), len(selected), ", ".join(a["handle"] for a in selected))
    if args.dry_run:
        return 0
    assert scraper is not None
    scraper.run_budget = budget.get("run_budget")

    fresh: list[dict[str, Any]] = []
    errors: list[str] = []
    ok_accounts = 0

    # Scrape accounts in parallel (keep max_concurrency within your Firecrawl
    # plan's concurrent-request limit). Results are consumed in input order.
    workers = max(1, int(settings.get("max_concurrency") or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda a: scrape_account(scraper, a, settings, now), selected))

    for acct, (posts, errs) in zip(selected, results):
        fresh.extend(posts)
        errors.extend(errs)
        if not errs or posts:
            ok_accounts += 1
            state["last_scraped"][acct["handle"].lower()] = iso(now)

    if budget["mode"] == "budget":
        settle_budget(scraper, budget, state)
    if not selected and budget.get("mode") == "budget":
        print(
            f"::notice title=Credit budget::Skipping this run to stay within Firecrawl credits "
            f"(bank {budget['bank']:.1f} < {budget['credits_per_scrape']:.1f} credits per scrape). "
            "Credits accrue each run; scraping resumes automatically."
        )

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
        "out_of_credits": scraper.out_of_credits,
        "credits_spent": round(scraper.credits_spent, 2),
        "budget": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in budget.items()},
        "error_count": len(errors),
        "errors": [e for e in errors if "skipped:" not in e][:20],
    }
    state["runs"] = ([run_summary] + state.get("runs", []))[:20]
    write_json(STATE_PATH, state)

    log.info("done: %d/%d accounts ok, %d fresh posts, %d published, %d errors", ok_accounts, len(selected), len(fresh), len(ranked), len(errors))
    if scraper.out_of_credits:
        skipped = sum("skipped:" in e for e in errors)
        # Not a failure of the run: data that was scraped is still published.
        print(
            f"::error title=Firecrawl credits exhausted::{skipped} accounts skipped. Top up credits or set "
            "accounts_per_run to a number in accounts.json to rotate through accounts."
        )
    for e in errors:
        if "skipped:" not in e:
            # GitHub Actions annotation: visible in the run summary without failing it.
            print(f"::warning title=Firecrawl scrape failed::{e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
