"use client";

// GK-467. Route-level error boundary. `global-error.tsx` only catches errors
// that escape the ROOT LAYOUT, so before this file existed any crash inside a
// page — most often the backend being unreachable mid-deploy — fell through to
// Next.js's unstyled 500. This one renders inside the root layout, so the
// member gets the portal's own styling and a retry that re-runs the render.
import * as Sentry from "@sentry/nextjs";
import { useEffect } from "react";

import { ReloadButton } from "@/components/reload-button";
import { ServiceUnavailable } from "@/components/service-unavailable";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <main className="container max-w-3xl py-16">
      <ServiceUnavailable
        title="Что-то пошло не так"
        description="Не удалось загрузить страницу. Мы уже получили уведомление — попробуйте обновить."
        action={<ReloadButton onClick={() => reset()} />}
      />
    </main>
  );
}
