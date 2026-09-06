import { redirect } from "next/navigation";

import { PortalHeader } from "@/components/portal-header";
import { ModuleCard, type PortalModule } from "@/components/module-card";
import { PortalUnavailable } from "@/components/portal-unavailable";
import { backendDown, backendGet } from "@/lib/backend";

export const dynamic = "force-dynamic";

type ModulesResponse = { modules: PortalModule[] };

/** Archive index (GK-093): a grid of showcase tiles, like Vimeo's Showcases page.
 *  Pick a showcase → its own page lists the videos → pick one to play. The index
 *  no longer loads any videos, so it stays light regardless of library size. */
export default async function ArchivePage() {
  const res = await backendGet<ModulesResponse>("/portal/modules");
  if (!res.ok) {
    // GK-467: backend unreachable or 5xx → the member's session is fine, the
    // service isn't. Bouncing them to the landing page here would read as
    // "you've been logged out" on every deploy.
    if (backendDown(res)) return <PortalUnavailable active="archive" />;
    // 403 = valid session but lapsed subscription → /me (renew prompt).
    // anything else (401/expired) → landing page.
    if (res.status === 403) redirect("/me");
    redirect("/?error=login");
  }

  const { modules } = res.data;

  return (
    <>
      <PortalHeader active="archive" />
      <main className="container max-w-5xl py-8">
        <h1 className="text-2xl font-semibold tracking-tight">Видеоархив</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Разделы библиотеки сообщества. Откройте раздел, чтобы выбрать видео. Доступно,
          пока активна подписка.
        </p>

        {modules.length === 0 ? (
          <div className="mt-10 rounded-xl border border-dashed bg-card p-10 text-center text-sm text-muted-foreground">
            Разделы скоро появятся. Загляните позже.
          </div>
        ) : (
          <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {modules.map((m) => (
              <ModuleCard key={`${m.id}-${m.code}`} module={m} />
            ))}
          </div>
        )}
      </main>
    </>
  );
}
