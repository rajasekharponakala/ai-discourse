"use client";

import * as React from "react";
import { Info, ListOrdered, Search, X } from "lucide-react";

import { PostCard } from "@/components/post-card";
import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { timeAgo, type Post, type PostsFile } from "@/lib/posts";

type SortKey = "score" | "recent" | "likes" | "views";
const SORTS: { key: SortKey; label: string }[] = [
  { key: "score", label: "Top" },
  { key: "recent", label: "Newest" },
  { key: "likes", label: "Most liked" },
  { key: "views", label: "Most viewed" },
];

const postTime = (p: Post) => Date.parse(p.posted_at || p.first_seen_at || "") || 0;

function sortPosts(posts: Post[], key: SortKey) {
  const sorted = [...posts];
  switch (key) {
    case "recent":
      return sorted.sort((a, b) => postTime(b) - postTime(a));
    case "likes":
      return sorted.sort((a, b) => b.engagement.likes - a.engagement.likes);
    case "views":
      return sorted.sort((a, b) => b.engagement.views - a.engagement.views);
    default:
      return sorted.sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  }
}

/** Client clock, only after mount (avoids hydration mismatch on a static page). */
function useNow(intervalMs = 60_000) {
  const [now, setNow] = React.useState<number | null>(null);
  React.useEffect(() => {
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

function FilterSwitch({
  id,
  label,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label htmlFor={id} className="flex cursor-pointer items-center gap-2 text-sm select-none">
      <Switch id={id} checked={checked} onCheckedChange={onChange} />
      {label}
    </label>
  );
}

export function PostFeed({ data }: { data: PostsFile }) {
  const now = useNow();
  const [query, setQuery] = React.useState("");
  const [account, setAccount] = React.useState("all");
  const [listiclesOnly, setListiclesOnly] = React.useState(false);
  const [highOnly, setHighOnly] = React.useState(false);
  const [mediaOnly, setMediaOnly] = React.useState(false);
  const [sort, setSort] = React.useState<SortKey>("score");

  const posts = data.posts;

  // Authors that actually have posts, most posts first.
  const authors = React.useMemo(() => {
    const counts = new Map<string, { handle: string; name: string; count: number }>();
    for (const p of posts) {
      const key = p.author_handle.toLowerCase();
      const cur = counts.get(key) ?? { handle: p.author_handle, name: p.author_name, count: 0 };
      cur.count += 1;
      counts.set(key, cur);
    }
    return [...counts.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }, [posts]);

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    const out = posts.filter((p) => {
      if (account !== "all" && p.author_handle.toLowerCase() !== account) return false;
      if (listiclesOnly && !p.is_listicle) return false;
      if (highOnly && p.engagement_tier !== "viral" && p.engagement_tier !== "hot") return false;
      if (mediaOnly && p.media_urls.length === 0) return false;
      if (!q) return true;
      return [p.sensational_headline, p.post_text, p.author_name, p.author_handle, ...(p.list_items ?? [])]
        .join(" \n ")
        .toLowerCase()
        .includes(q);
    });
    return sortPosts(out, sort);
  }, [posts, query, account, listiclesOnly, highOnly, mediaOnly, sort]);

  // Rank reflects the global score ranking, not the filtered position.
  const rankById = React.useMemo(
    () => new Map(sortPosts(posts, "score").map((p, i) => [p.id, i + 1])),
    [posts],
  );

  const activeFilters = query || account !== "all" || listiclesOnly || highOnly || mediaOnly;
  const reset = () => {
    setQuery("");
    setAccount("all");
    setListiclesOnly(false);
    setHighOnly(false);
    setMediaOnly(false);
  };

  const listicleCount = posts.filter((p) => p.is_listicle).length;
  const trackedCount = data.tracked_accounts?.length ?? authors.length;

  return (
    <div className="mx-auto w-full max-w-7xl px-4 pb-16 sm:px-6 lg:px-8">
      {/* Header */}
      <header className="flex items-start justify-between gap-4 pt-8 pb-6 sm:pt-12">
        <div className="min-w-0">
          <p className="text-brand text-xs font-semibold tracking-widest uppercase">AI Discourse</p>
          <h1 className="mt-1 text-3xl font-extrabold tracking-tight text-balance sm:text-4xl">
            What AI&apos;s loudest voices are saying on X
          </h1>
          <p className="text-muted-foreground mt-2 max-w-2xl text-sm sm:text-base">
            The highest-engagement posts, listicles and hot takes from {trackedCount} tracked AI &amp; tech accounts,
            ranked by engagement velocity and rewritten as scroll-stopping headlines.
          </p>
          <div className="text-muted-foreground mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs">
            <span>
              <strong className="text-foreground">{posts.length}</strong> posts
            </span>
            <span>
              <strong className="text-foreground">{authors.length}</strong> authors
            </span>
            <span>
              <strong className="text-foreground">{listicleCount}</strong> listicles
            </span>
            <span title={data.generated_at}>
              Updated {now ? timeAgo(data.generated_at, now) : data.generated_at.slice(0, 10)}
            </span>
          </div>
        </div>
        <ThemeToggle />
      </header>

      {data.sample && (
        <div className="mb-6 flex gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm">
          <Info className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
          <p>
            <strong>Sample data.</strong> These demo posts use made-up accounts so the site renders before the first
            scrape. The GitHub Action replaces them with real posts on its next successful run.
          </p>
        </div>
      )}

      {/* Controls */}
      <section
        aria-label="Filters"
        className="bg-background/80 supports-[backdrop-filter]:bg-background/60 sticky top-0 z-10 -mx-4 mb-6 border-b px-4 py-3 backdrop-blur sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8"
      >
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="relative flex-1">
            <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
            <Input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search headlines, posts, authors…"
              className="pl-9"
              aria-label="Search posts"
            />
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <select
              value={account}
              onChange={(e) => setAccount(e.target.value)}
              aria-label="Filter by account"
              className="border-input bg-background focus-visible:ring-ring/50 h-9 max-w-[14rem] rounded-md border px-3 text-sm shadow-xs outline-none focus-visible:ring-[3px]"
            >
              <option value="all">All accounts ({posts.length})</option>
              {authors.map((a) => (
                <option key={a.handle} value={a.handle.toLowerCase()}>
                  {a.name} (@{a.handle}) · {a.count}
                </option>
              ))}
            </select>
            <div role="radiogroup" aria-label="Sort" className="bg-muted inline-flex rounded-md p-0.5">
              {SORTS.map((s) => (
                <button
                  key={s.key}
                  role="radio"
                  aria-checked={sort === s.key}
                  onClick={() => setSort(s.key)}
                  className={cn(
                    "rounded px-2.5 py-1 text-xs font-medium transition-colors",
                    sort === s.key
                      ? "bg-background text-foreground shadow-xs"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2">
          <FilterSwitch id="f-list" label="Listicles only" checked={listiclesOnly} onChange={setListiclesOnly} />
          <FilterSwitch id="f-high" label="High engagement" checked={highOnly} onChange={setHighOnly} />
          <FilterSwitch id="f-media" label="With media" checked={mediaOnly} onChange={setMediaOnly} />
          {activeFilters && (
            <Button variant="ghost" size="sm" onClick={reset} className="ml-auto">
              <X /> Clear filters
            </Button>
          )}
        </div>
      </section>

      {/* Author chips (quick filter) */}
      {authors.length > 1 && (
        <div className="-mx-1 mb-6 flex gap-2 overflow-x-auto px-1 pb-1">
          {authors.slice(0, 20).map((a) => {
            const active = account === a.handle.toLowerCase();
            return (
              <button
                key={a.handle}
                onClick={() => setAccount(active ? "all" : a.handle.toLowerCase())}
                className={cn(
                  "shrink-0 rounded-full border px-3 py-1 text-xs transition-colors",
                  active ? "bg-primary text-primary-foreground border-primary" : "hover:bg-accent",
                )}
                aria-pressed={active}
              >
                @{a.handle} <span className="opacity-60">{a.count}</span>
              </button>
            );
          })}
        </div>
      )}

      <p className="text-muted-foreground mb-4 text-sm" aria-live="polite">
        Showing {filtered.length} of {posts.length} posts
        {listiclesOnly && (
          <Badge variant="list" className="ml-2">
            <ListOrdered /> listicles
          </Badge>
        )}
      </p>

      {filtered.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((post) => (
            <PostCard
              key={post.id}
              post={post}
              rank={rankById.get(post.id) ?? 0}
              now={now}
              onAuthorClick={(h) => setAccount(h.toLowerCase())}
            />
          ))}
        </div>
      ) : (
        <div className="text-muted-foreground rounded-xl border border-dashed p-12 text-center">
          <p className="font-medium">No posts match these filters.</p>
          <Button variant="outline" size="sm" className="mt-4" onClick={reset}>
            Clear filters
          </Button>
        </div>
      )}

      <footer className="text-muted-foreground mt-16 border-t pt-6 text-xs">
        Data scraped from public X posts via Firecrawl every 2 hours. Headlines are auto-generated summaries, not quotes.
        Always check the original post.
      </footer>
    </div>
  );
}
