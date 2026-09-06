import Link from "next/link";
import { redirect } from "next/navigation";

import { PortalHeader } from "@/components/portal-header";
import { ShowcaseView } from "@/components/showcase-view";
import type { PortalModule } from "@/components/module-card";
import type { PortalVideo } from "@/components/video-card";
import { PortalUnavailable } from "@/components/portal-unavailable";
import { backendDown, backendGet } from "@/lib/backend";

export const dynamic = "force-dynamic";

type ModuleVideosResponse = { module: PortalModule; videos: PortalVideo[] };

function NotFound() {
  return (
    <>
      <PortalHeader active="archive" />
      <main className="container max-w-3xl py-16 text-center">
        <p className="text-muted-foreground">Раздел не найден или скрыт.</p>
        <Link href="/archive" className="mt-4 inline-block text-sm text-primary hover:underline">
          ← Все разделы
        </Link>
      </main>
    </>
  );
}

/** Drill-down level 2 (GK-093): one showcase's videos. */
export default async function ModulePage({ params }: { params: { moduleId: string } }) {
  // Guard non-numeric ids so a typo'd URL shows "not found" instead of a 422→login.
  if (!/^\d+$/.test(params.moduleId)) return <NotFound />;

  const res = await backendGet<ModuleVideosResponse>(`/portal/modules/${params.moduleId}`);
  if (!res.ok) {
    if (backendDown(res)) return <PortalUnavailable active="archive" />;
    if (res.status === 403) redirect("/me");
    if (res.status === 404) return <NotFound />;
    redirect("/?error=login");
  }

  return <ShowcaseView module={res.data.module} videos={res.data.videos} />;
}
