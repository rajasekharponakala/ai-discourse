"use client";

import * as React from "react";
import {
  Bookmark,
  Check,
  ChevronDown,
  Copy,
  Eye,
  Flame,
  Heart,
  ListOrdered,
  MessageCircle,
  Repeat2,
  TrendingUp,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { formatCount, timeAgo, type Post } from "@/lib/posts";

// Deterministic avatar colour per handle (we don't hot-link X avatars).
const AVATAR_COLORS = [
  "bg-violet-500",
  "bg-sky-500",
  "bg-emerald-500",
  "bg-amber-500",
  "bg-rose-500",
  "bg-indigo-500",
  "bg-teal-500",
  "bg-fuchsia-500",
];
function avatarColor(handle: string) {
  let h = 0;
  for (const c of handle) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase())
    .join("");
}

function XLogo(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor" {...props}>
      <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
    </svg>
  );
}

function Metric({ icon: Icon, value, label }: { icon: React.ElementType; value: number; label: string }) {
  return (
    <span className="inline-flex items-center gap-1 tabular-nums" title={`${value.toLocaleString()} ${label}`}>
      <Icon className="size-3.5" aria-hidden="true" />
      <span className="sr-only">{label}:</span>
      {formatCount(value)}
    </span>
  );
}

export function PostCard({
  post,
  rank,
  now,
  onAuthorClick,
}: {
  post: Post;
  rank: number;
  now: number | null;
  onAuthorClick?: (handle: string) => void;
}) {
  const [expanded, setExpanded] = React.useState(false);
  const [copied, setCopied] = React.useState(false);
  const [mediaFailed, setMediaFailed] = React.useState(false);
  const e = post.engagement;
  const isLong = post.post_text.length > 280 || post.post_text.split("\n").length > 6;
  const image = post.media_urls.find((u) => /\.(jpe?g|png|webp|gif)(\?|$)|pbs\.twimg\.com\/media/i.test(u));

  const copyHeadline = async () => {
    try {
      await navigator.clipboard.writeText(`${post.sensational_headline}\n\n${post.url}`);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked: ignore */
    }
  };

  return (
    <Card className="group relative h-full overflow-hidden transition-all hover:-translate-y-0.5 hover:shadow-lg hover:border-brand/40">
      <CardHeader>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => onAuthorClick?.(post.author_handle)}
            className={cn(
              "flex size-10 shrink-0 items-center justify-center rounded-full text-sm font-semibold text-white",
              avatarColor(post.author_handle),
            )}
            aria-label={`Show only posts by @${post.author_handle}`}
          >
            {initials(post.author_name || post.author_handle)}
          </button>
          <div className="min-w-0 flex-1">
            <button
              type="button"
              onClick={() => onAuthorClick?.(post.author_handle)}
              className="block max-w-full truncate text-left text-sm font-semibold hover:underline"
            >
              {post.author_name}
            </button>
            <div className="text-muted-foreground truncate text-xs">
              @{post.author_handle}
              {post.posted_at && (
                <>
                  {" · "}
                  <time dateTime={post.posted_at} title={new Date(post.posted_at).toUTCString()}>
                    {now ? timeAgo(post.posted_at, now) : post.posted_at.slice(0, 10)}
                  </time>
                </>
              )}
            </div>
          </div>
          <span className="text-muted-foreground/60 text-xs font-semibold tabular-nums">#{rank}</span>
        </div>

        <div className="flex flex-wrap gap-1.5">
          {post.engagement_tier === "viral" && (
            <Badge variant="viral">
              <Flame /> Viral
            </Badge>
          )}
          {post.engagement_tier === "hot" && (
            <Badge variant="hot">
              <TrendingUp /> Hot
            </Badge>
          )}
          {post.is_listicle && (
            <Badge variant="list">
              <ListOrdered /> {post.list_items.length}-item list
            </Badge>
          )}
          {post.sample && <Badge variant="outline">Sample</Badge>}
        </div>

        <h2 className="text-xl leading-tight font-bold tracking-tight text-balance sm:text-[1.35rem]">
          {post.sensational_headline}
        </h2>
      </CardHeader>

      <CardContent className="flex flex-1 flex-col gap-3">
        <div className="bg-muted/50 rounded-lg border p-3 text-sm">
          <p className={cn("text-muted-foreground whitespace-pre-line break-words", !expanded && "line-clamp-5")}>
            {post.post_text}
          </p>
          {(isLong || post.list_items.length > 0) && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="text-brand mt-1 inline-flex items-center gap-0.5 text-xs font-medium hover:underline"
              aria-expanded={expanded}
            >
              {expanded ? "Show less" : post.is_listicle ? "Show the full list" : "Show more"}
              <ChevronDown className={cn("size-3 transition-transform", expanded && "rotate-180")} />
            </button>
          )}
          {expanded && post.is_listicle && post.list_items.length > 0 && (
            <ol className="mt-3 space-y-1.5 border-t pt-3">
              {post.list_items.map((item, i) => (
                <li key={i} className="flex gap-2">
                  <span className="bg-brand/15 text-brand flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold">
                    {i + 1}
                  </span>
                  <span>{item}</span>
                </li>
              ))}
            </ol>
          )}
        </div>

        {image && !mediaFailed && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={image}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
            onError={() => setMediaFailed(true)}
            className="max-h-64 w-full rounded-lg border object-cover"
          />
        )}

        <div className="text-muted-foreground mt-auto flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
          <Metric icon={Heart} value={e.likes} label="likes" />
          <Metric icon={Repeat2} value={e.reposts} label="reposts" />
          <Metric icon={MessageCircle} value={e.replies} label="replies" />
          <Metric icon={Eye} value={e.views} label="views" />
          <Metric icon={Bookmark} value={e.bookmarks} label="bookmarks" />
        </div>
      </CardContent>

      <CardFooter className="gap-2 border-t pt-4">
        <Button asChild size="sm" className="flex-1">
          <a href={post.url} target="_blank" rel="noopener noreferrer">
            <XLogo className="size-3.5" /> View on X
          </a>
        </Button>
        <Button size="sm" variant="outline" onClick={copyHeadline} aria-label="Copy headline and link">
          {copied ? <Check /> : <Copy />}
          <span className="hidden sm:inline">{copied ? "Copied" : "Copy"}</span>
        </Button>
        {typeof post.score === "number" && (
          <span className="text-muted-foreground ml-auto text-xs tabular-nums" title="Ranking score">
            {post.score.toFixed(0)} pts
          </span>
        )}
      </CardFooter>
    </Card>
  );
}
