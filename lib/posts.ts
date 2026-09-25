// Types + helpers for data/posts.json (written by scripts/scrape_x.py).

export type Engagement = {
  likes: number;
  reposts: number;
  replies: number;
  views: number;
  bookmarks: number;
};

export type Post = {
  id: string;
  url: string;
  author_handle: string;
  author_name: string;
  post_text: string;
  posted_at: string | null;
  engagement: Engagement;
  is_listicle: boolean;
  list_items: string[];
  media_urls: string[];
  sensational_headline: string;
  tags?: string[];
  engagement_tier?: "viral" | "hot" | "normal";
  score?: number;
  first_seen_at?: string;
  sample?: boolean;
};

export type TrackedAccount = { handle: string; name: string; tags?: string[] };

export type PostsFile = {
  generated_at: string;
  source?: string;
  sample?: boolean;
  tracked_accounts?: TrackedAccount[];
  posts: Post[];
};

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });

export function formatCount(n: number | undefined | null): string {
  return compact.format(Math.max(0, Number(n) || 0));
}

export function timeAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export function totalEngagement(e: Engagement): number {
  return (e.likes || 0) + (e.reposts || 0) + (e.replies || 0) + (e.bookmarks || 0);
}
