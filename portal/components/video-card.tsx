import Link from "next/link";
import { PlayCircle } from "lucide-react";

import { formatDuration } from "@/lib/utils";

export type PortalVideo = {
  vimeo_id: number;
  title: string;
  description: string | null;
  duration_seconds: number | null;
  thumbnail_url: string | null;
  module_id: number | null;
  sort_order: number;
};

export function VideoCard({ video }: { video: PortalVideo }) {
  const duration = formatDuration(video.duration_seconds);
  return (
    <Link
      href={`/archive/v/${video.vimeo_id}`}
      className="group flex flex-col overflow-hidden rounded-xl border bg-card transition hover:shadow-md hover:shadow-black/5"
    >
      <div className="relative aspect-video w-full bg-muted">
        {video.thumbnail_url ? (
          // Vimeo thumbnail CDN; <img> avoids next/image remote-domain config.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={video.thumbnail_url}
            alt=""
            className="h-full w-full object-cover transition group-hover:scale-[1.02]"
            loading="lazy"
          />
        ) : (
          <div className="grid h-full w-full place-items-center text-muted-foreground">
            <PlayCircle className="h-10 w-10" />
          </div>
        )}
        <div className="absolute inset-0 grid place-items-center bg-black/0 transition group-hover:bg-black/20">
          <PlayCircle className="h-12 w-12 text-white/0 transition group-hover:text-white/90" />
        </div>
        {duration && (
          <span className="absolute bottom-2 right-2 rounded bg-black/70 px-1.5 py-0.5 text-xs font-medium text-white">
            {duration}
          </span>
        )}
      </div>
      <div className="flex flex-1 flex-col p-3">
        <h3 className="line-clamp-2 text-sm font-medium leading-snug">{video.title}</h3>
        {video.description && (
          <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{video.description}</p>
        )}
      </div>
    </Link>
  );
}
