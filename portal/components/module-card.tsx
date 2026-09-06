import Link from "next/link";
import { Layers } from "lucide-react";

export type PortalModule = {
  id: number;
  code: string;
  title: string;
  description: string | null;
  sort_order: number;
  video_count: number;
  cover_url: string | null;
  is_orphans: boolean;
};

/** One Vimeo-style showcase tile on the archive index. Clicking it drills into
 *  that showcase's own page (GK-093 two-level navigation).
 *  ("видео" is invariant in Russian, so the count needs no pluralization.) */
export function ModuleCard({ module }: { module: PortalModule }) {
  const href = module.is_orphans ? "/archive/orphans" : `/archive/m/${module.id}`;
  return (
    <Link
      href={href}
      className="group flex flex-col overflow-hidden rounded-xl border bg-card transition hover:-translate-y-0.5 hover:shadow-md hover:shadow-black/5"
    >
      <div className="relative aspect-video w-full bg-muted">
        {module.cover_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={module.cover_url}
            alt=""
            className="h-full w-full object-cover transition group-hover:scale-[1.03]"
            loading="lazy"
          />
        ) : (
          <div className="grid h-full w-full place-items-center text-muted-foreground">
            <Layers className="h-10 w-10" />
          </div>
        )}
        {/* darken on hover + count badge, like a Vimeo collection tile */}
        <div className="absolute inset-0 bg-gradient-to-t from-black/55 via-black/0 to-black/0" />
        <span className="absolute bottom-2 left-2 inline-flex items-center gap-1 rounded-md bg-black/70 px-2 py-0.5 text-xs font-medium text-white">
          <Layers className="h-3 w-3" />
          {module.video_count} видео
        </span>
      </div>
      <div className="flex flex-1 flex-col p-3">
        <h3 className="line-clamp-2 text-sm font-semibold leading-snug">{module.title}</h3>
        {module.description && (
          <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{module.description}</p>
        )}
      </div>
    </Link>
  );
}
