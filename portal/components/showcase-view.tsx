import Link from "next/link";
import { ChevronLeft } from "lucide-react";

import { PortalHeader } from "@/components/portal-header";
import { VideoCard, type PortalVideo } from "@/components/video-card";
import type { PortalModule } from "@/components/module-card";

/** Drill-down level 2 (GK-093): one showcase's own page — back link, title +
 *  count, then just this showcase's videos. Shared by the module and orphans
 *  pages so both render identically. */
export function ShowcaseView({
  module,
  videos,
}: {
  module: PortalModule;
  videos: PortalVideo[];
}) {
  return (
    <>
      <PortalHeader active="archive" />
      <main className="container max-w-5xl py-8">
        <Link
          href="/archive"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground transition hover:text-foreground"
        >
          <ChevronLeft className="h-4 w-4" />
          Все разделы
        </Link>

        <h1 className="mt-3 text-2xl font-semibold tracking-tight">{module.title}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {module.video_count} видео
          {module.description ? ` · ${module.description}` : ""}
        </p>

        {videos.length === 0 ? (
          <div className="mt-10 rounded-xl border border-dashed bg-card p-10 text-center text-sm text-muted-foreground">
            В этом разделе пока нет видео.
          </div>
        ) : (
          <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {videos.map((v) => (
              <VideoCard key={v.vimeo_id} video={v} />
            ))}
          </div>
        )}
      </main>
    </>
  );
}
