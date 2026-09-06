import Link from "next/link";
import { redirect } from "next/navigation";
import { ChevronLeft } from "lucide-react";

import { PortalHeader } from "@/components/portal-header";
import { PortalUnavailable } from "@/components/portal-unavailable";
import { backendDown, backendGet } from "@/lib/backend";
import { formatDuration } from "@/lib/utils";

export const dynamic = "force-dynamic";

type VideoDetail = {
  vimeo_id: number;
  title: string;
  description: string | null;
  duration_seconds: number | null;
  player_embed_url: string;
  module_id: number | null;
};

export default async function PlayerPage({ params }: { params: { vimeoId: string } }) {
  const res = await backendGet<VideoDetail>(`/portal/videos/${params.vimeoId}`);
  if (!res.ok) {
    if (backendDown(res)) return <PortalUnavailable active="archive" />;
    if (res.status === 403) redirect("/me");
    if (res.status === 404) {
      return (
        <>
          <PortalHeader active="archive" />
          <main className="container max-w-3xl py-16 text-center">
            <p className="text-muted-foreground">Видео не найдено или скрыто.</p>
            <Link href="/archive" className="mt-4 inline-block text-sm text-primary hover:underline">
              ← Назад к архиву
            </Link>
          </main>
        </>
      );
    }
    redirect("/?error=login");
  }

  const video = res.data;
  const duration = formatDuration(video.duration_seconds);
  // Build the embed from the id so the iframe is always loaded under OUR origin
  // — Vimeo's domain-level privacy reads the Referer header from this page.
  // strict-origin-when-cross-origin (set in Caddy) sends only the origin.
  const src = `https://player.vimeo.com/video/${video.vimeo_id}?title=0&byline=0&portrait=0&dnt=1`;

  return (
    <>
      <PortalHeader active="archive" />
      <main className="container max-w-4xl py-6">
        <Link
          href="/archive"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground transition hover:text-foreground"
        >
          <ChevronLeft className="h-4 w-4" />
          Назад к архиву
        </Link>

        <div className="mt-4 overflow-hidden rounded-xl border bg-black">
          <div className="relative aspect-video w-full">
            <iframe
              src={src}
              title={video.title}
              className="absolute inset-0 h-full w-full"
              allow="autoplay; fullscreen; picture-in-picture"
              allowFullScreen
            />
          </div>
        </div>

        <div className="mt-5">
          <h1 className="text-xl font-semibold tracking-tight">{video.title}</h1>
          {duration && <p className="mt-1 text-sm text-muted-foreground">{duration}</p>}
          {video.description && (
            <p className="mt-4 whitespace-pre-line text-sm leading-relaxed text-foreground/90">
              {video.description}
            </p>
          )}
        </div>
      </main>
    </>
  );
}
