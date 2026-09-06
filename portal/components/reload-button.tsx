"use client";

import { RefreshCw } from "lucide-react";

// GK-467. The retry control for the "сервис недоступен" panel. A client
// component so it can be dropped into server-rendered pages (where the only
// sensible retry is re-requesting the page) as well as into an error boundary
// (where `onClick` is wired to Next.js's `reset`).
export function ReloadButton({
  onClick,
  label = "Обновить",
}: {
  onClick?: () => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick ?? (() => window.location.reload())}
      className="inline-flex items-center gap-2 rounded-xl bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground transition hover:opacity-90"
    >
      <RefreshCw className="h-4 w-4" />
      {label}
    </button>
  );
}
