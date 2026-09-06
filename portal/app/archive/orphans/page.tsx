import { redirect } from "next/navigation";

import { ShowcaseView } from "@/components/showcase-view";
import type { PortalModule } from "@/components/module-card";
import type { PortalVideo } from "@/components/video-card";
import { PortalUnavailable } from "@/components/portal-unavailable";
import { backendDown, backendGet } from "@/lib/backend";

export const dynamic = "force-dynamic";

type ModuleVideosResponse = { module: PortalModule; videos: PortalVideo[] };

/** Drill-down level 2 (GK-093): the "Остальные видео" page — visible videos that
 *  belong to no active showcase. */
export default async function OrphansPage() {
  const res = await backendGet<ModuleVideosResponse>("/portal/orphans");
  if (!res.ok) {
    if (backendDown(res)) return <PortalUnavailable active="archive" />;
    if (res.status === 403) redirect("/me");
    redirect("/?error=login");
  }

  return <ShowcaseView module={res.data.module} videos={res.data.videos} />;
}
