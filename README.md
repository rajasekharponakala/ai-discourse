# AI Discourse: X tracker with viral headlines

Tracks popular AI/tech people on X, pulls their highest-engagement posts (especially listicles) with **Firecrawl**, ranks them and writes a punchy headline for each one. A static **Next.js** site on GitHub Pages shows the results.

```
accounts.json ──► scripts/scrape_x.py ──► Firecrawl (markdown + JSON extraction)
                        │
                        ▼
              scripts/score_and_headline.py  (velocity score, listicle/media bonus, headline cleanup)
                        │
                        ▼
                 data/posts.json  ──►  Next.js static export  ──►  GitHub Pages
```

Every 2 hours, `.github/workflows/track-x.yml` scrapes, commits `data/posts.json` and `.github/workflows/pages.yml` rebuilds the site.

## Repository layout

| Path | What it is |
| --- | --- |
| `accounts.json` | Handles to track, plus scrape settings. Edit this file to change who is tracked. |
| `scripts/scrape_x.py` | Main scraper. Uses the Firecrawl Python SDK, merges results with earlier runs and ranks them. |
| `scripts/score_and_headline.py` | Scoring and headline pass with no network calls. The scraper imports it, and you can also run it on its own. |
| `scripts/fixtures/sample_extraction.json` | Offline fixture so you can test the pipeline without using credits. |
| `data/posts.json` | Latest ranked posts. The workflow commits it. It starts as **sample data** (made-up demo accounts) so the site renders immediately. |
| `data/scrape_state.json` | Rotation state (when each account was last scraped) plus a log of the last 20 runs. The first scrape creates it. |
| `app/`, `components/`, `lib/` | Next.js App Router frontend (Tailwind v4 + shadcn/ui). |
| `.github/workflows/track-x.yml` | Scheduled scraper. Runs every 2 hours and can be started manually. |
| `.github/workflows/pages.yml` | Builds and deploys the static site to GitHub Pages. |

## One-time setup

1. **Secret**: add a repository secret named `FIRECRAWL_API` under *Settings → Secrets and variables → Actions*, with your `fc-...` key.
2. **GitHub Pages**: go to *Settings → Pages → Build and deployment → Source* and choose **GitHub Actions**. Then re-run the *Deploy site to GitHub Pages* workflow, or push any commit. The site is served at `https://<user>.github.io/<repo>/`.
3. **First scrape**: go to *Actions → Track X posts → Run workflow*. You can type a few handles in `only` to test cheaply, for example `karpathy,rasbt`.

## Adding or removing accounts

Edit `accounts.json`:

```jsonc
{
  "handle": "karpathy",          // required, without the @
  "name": "Andrej Karpathy",     // display name used as a fallback
  "tags": ["research"],          // optional, informational
  "post_urls": [                 // optional: specific posts to always (re)scrape
    "https://x.com/karpathy/status/1234567890"
  ],
  "max_posts": 2,                // optional per-account override
  "skip_profile": false,         // true = scrape only post_urls (cheapest)
  "enabled": true                // false = keep in the list but don't scrape
}
```

A plain string (`"karpathy"`) also works. You don't need to change any code. The next run picks up the new list, and new accounts go first because they have never been scraped.

### Settings (`accounts.json → settings`)

| Key | Default | Meaning |
| --- | --- | --- |
| `max_posts_per_account` | 3 | Posts kept per profile scrape |
| `accounts_per_run` | 6 | Accounts scraped per run. The least recently scraped go first, so the whole list is covered over time. |
| `keep_top_posts` | 60 | Size of the published leaderboard |
| `max_post_age_days` | 14 | Posts older than this drop off |
| `cache_max_age_minutes` | 60 | Firecrawl `maxAge`: reuses a cached scrape if one is this fresh, which saves credits |
| `scrape_timeout_ms` | 120000 | Timeout for each scrape |

## How the scraper works

1. **Rotation**: it picks the `accounts_per_run` least recently scraped accounts. Accounts with explicit `post_urls` are always included.
2. **Targets**: it scrapes explicit post URLs first, since they are the cheapest and most reliable, then the profile page `https://x.com/<handle>`.
3. **Firecrawl call**: each URL is requested with both `markdown` and a `json` format that has a strict JSON schema and a rule-based prompt (`build_prompt` in `scrape_x.py`). For each post the extraction returns `author_handle`, `author_name`, `post_text` (verbatim), `posted_at`, `engagement{likes,reposts,replies,views,bookmarks}`, `is_listicle`, `list_items`, `media_urls` and `sensational_headline`.
4. **Validation**: `1.2K`/`3.4M` counts are normalised, reposts and other authors' posts are dropped, status URLs are made canonical, and future-dated timestamps are rejected. List bullets are cleaned and non-media URLs are filtered out.
5. **Merge**: posts are deduplicated by status ID across runs. Engagement never goes down between runs, and `first_seen_at` is preserved.
6. **Score and headline** (`score_and_headline.py`):
   - `weighted = likes + 2.5·reposts + 1.5·replies + 2·bookmarks + 0.01·views`
   - `velocity = weighted / (age_hours + 2)^0.8`, a Hacker News-style gravity that favours fresh posts
   - × `1.35 + 0.02·items` for listicles, × `1.12` for posts with media, then log-scaled to about 0–100
   - Headlines are cleaned: hashtags, emojis, URLs and shouting are removed, and length is capped at 110 characters. If the model's headline is missing or too short, a deterministic template is used instead.
   - Each post is tagged `viral` (≥10k likes or ≥1M views) or `hot` (≥2k likes or ≥200k views) for the UI filter.
7. **Robustness**: each URL gets 3 retries with backoff, and auth, credit and bad-request errors are not retried. One failing URL or account never fails the run; failures show up as workflow warnings and in `data/scrape_state.json`. If a run produces nothing and no real data exists yet, the sample file is left alone so the site keeps rendering. Files are written atomically.

## Running locally

```bash
# Python side
python3 -m venv .venv && source .venv/bin/activate
pip install -r scripts/requirements.txt

python scripts/scrape_x.py --dry-run                           # which accounts would be scraped
export FIRECRAWL_API=fc-...                                    # your key
python scripts/scrape_x.py --only karpathy --max-posts 2       # cheap real test
python scripts/scrape_x.py                                     # normal rotation run

# offline, no credits (writes the demo account into data/posts.json; `git checkout data/` afterwards)
python scripts/scrape_x.py --only demo_ml --fixture scripts/fixtures/sample_extraction.json

# re-score / re-headline existing data after tweaking weights
python scripts/score_and_headline.py --check

# Frontend
npm install
npm run dev        # http://localhost:3000
npm run build      # static export in ./out
```

## Firecrawl credit considerations

- **X pages cost more than normal pages.** Firecrawl routes x.com / twitter.com URLs through its Grok-backed tooling, and JSON extraction adds its own cost on top of a plain scrape. Expect each X URL to cost several times a normal scrape. Check the current rates on your Firecrawl dashboard. This is expected.
- **Budget math**: `runs_per_day (12) × accounts_per_run (6) × URLs per account (1 profile + post_urls)` gives about **72 X scrapes per day** with the defaults. With 52 accounts, each one is refreshed roughly every 8–9 hours.
- **Ways to spend less**:
  - Lower `accounts_per_run` or run less often by editing the cron in `track-x.yml`.
  - Keep `max_posts_per_account` small. This mainly reduces extraction output, not page count.
  - Raise `cache_max_age_minutes` so repeat requests are served from Firecrawl's cache.
  - For accounts you only care about for specific posts, use `post_urls` + `skip_profile: true`.
  - Set `"enabled": false` on accounts you want to pause.
- A missing key fails the workflow immediately with a clear error. An exhausted or invalid key (402/401) fails fast without retrying, is logged as a warning, and the previous data is kept.

## Frontend

- Next.js App Router, static export (`output: "export"`), Tailwind v4 and shadcn/ui primitives (`components/ui/*`, configured by `components.json`, so `npx shadcn add <component>` works).
- `app/page.tsx` imports `data/posts.json` at build time, and `components/post-feed.tsx` handles search, the account filter, the *listicles only* / *high engagement* / *with media* toggles and sorting (top, newest, most liked, most viewed).
- Cards show the generated headline, the original text (expandable, with a numbered list for listicles), author, engagement metrics, a **View on X** link and a copy-headline button.
- Dark and light themes follow the system and can be toggled. The layout is responsive from 360px up.

> Headlines are machine-generated summaries meant to be clickable. They are not quotes. The extraction prompt forbids invented facts, but always check the original post before sharing.
