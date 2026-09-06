import { redirect } from "next/navigation";
import { CheckCircle2, AlertTriangle, Send, LogOut } from "lucide-react";

import { PortalHeader } from "@/components/portal-header";
import { PortalUnavailable } from "@/components/portal-unavailable";
import { backendDown, backendGet, botDeepLink } from "@/lib/backend";
import { formatDate } from "@/lib/utils";

export const dynamic = "force-dynamic";

type MeResponse = {
  tg_id: number;
  username: string | null;
  first_name: string | null;
  has_access: boolean;
  subscription: {
    status: string;
    plan_code: string | null;
    plan_name: string | null;
    expires_at: string | null;
  } | null;
};

export default async function MePage() {
  const res = await backendGet<MeResponse>("/portal/me");
  if (!res.ok) {
    // GK-467: don't turn a mid-deploy blip into "your session ended".
    if (backendDown(res)) return <PortalUnavailable active="me" />;
    redirect("/?error=login");
  }

  const me = res.data;
  const expires = formatDate(me.subscription?.expires_at);
  const name = me.first_name || (me.username ? `@${me.username}` : "участник");

  return (
    <>
      <PortalHeader active="me" />
      <main className="container max-w-xl py-8">
        <h1 className="text-2xl font-semibold tracking-tight">Профиль</h1>
        <p className="mt-1 text-sm text-muted-foreground">Привет, {name}!</p>

        <div className="mt-6 rounded-xl border bg-card p-5">
          {me.has_access ? (
            <div className="flex items-start gap-3">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
              <div>
                <p className="font-medium">Подписка активна</p>
                <p className="mt-0.5 text-sm text-muted-foreground">
                  {me.subscription?.plan_name ? `Тариф: ${me.subscription.plan_name}. ` : ""}
                  {expires ? `Доступ до ${expires}.` : "Доступ к архиву открыт."}
                </p>
              </div>
            </div>
          ) : (
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" />
              <div>
                <p className="font-medium">Подписка неактивна</p>
                <p className="mt-0.5 text-sm text-muted-foreground">
                  Доступ к видео закрыт. Продлите подписку в Telegram-боте, и архив снова откроется.
                </p>
              </div>
            </div>
          )}
        </div>

        <div className="mt-5 flex flex-col gap-3 sm:flex-row">
          <a
            href={botDeepLink(me.has_access ? "portal" : "subscribe")}
            className="inline-flex flex-1 items-center justify-center gap-2 rounded-xl bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition hover:opacity-90"
          >
            <Send className="h-4 w-4" />
            {me.has_access ? "Открыть бота" : "Продлить в Telegram"}
          </a>
          <a
            href="/auth/logout"
            className="inline-flex items-center justify-center gap-2 rounded-xl border px-4 py-2.5 text-sm font-medium text-muted-foreground transition hover:bg-accent hover:text-foreground"
          >
            <LogOut className="h-4 w-4" />
            Выйти
          </a>
        </div>

        <p className="mt-6 text-xs text-muted-foreground">
          Доступ к архиву привязан к вашей подписке в нашей системе. Это не DRM —
          видео остаётся на Vimeo; мы управляем доступом на стороне сообщества.
        </p>
      </main>
    </>
  );
}
