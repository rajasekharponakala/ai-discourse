import { PostFeed } from "@/components/post-feed";
import type { PostsFile } from "@/lib/posts";
// Read at build time: the site is a static export, rebuilt whenever
// the GitHub Action commits a new data/posts.json.
import rawData from "@/data/posts.json";

const data = rawData as unknown as PostsFile;

export default function Home() {
  return (
    <main>
      <PostFeed data={data} />
    </main>
  );
}
