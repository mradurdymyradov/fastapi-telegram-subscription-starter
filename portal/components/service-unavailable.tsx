import { CloudOff } from "lucide-react";
import type { ReactNode } from "react";

import { ReloadButton } from "@/components/reload-button";

// GK-467. The member-facing failure state, shared by the pages (when the
// backend is unreachable or 5xx-ing) and by the error boundaries. Deliberately
// says nothing about status codes, containers or deploys — from where the
// member sits the only true and useful statements are "not right now" and
// "try again in a minute". Presentational only, so it renders in both a server
// component and a "use client" boundary.
export function ServiceUnavailable({
  title = "Сервис временно недоступен",
  description = "Не удалось загрузить данные. Обычно это занимает меньше минуты — попробуйте обновить страницу.",
  action,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mx-auto mt-6 flex max-w-md flex-col items-center rounded-xl border bg-card px-6 py-10 text-center">
      <div className="mb-4 grid h-12 w-12 place-items-center rounded-full bg-muted text-muted-foreground">
        <CloudOff className="h-6 w-6" />
      </div>
      <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
      <p className="mt-2 text-sm text-muted-foreground">{description}</p>
      <div className="mt-6">{action ?? <ReloadButton />}</div>
    </div>
  );
}
