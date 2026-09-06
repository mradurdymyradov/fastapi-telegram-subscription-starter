"use client";

// GK-468. Route-level error boundary. `global-error.tsx` only catches what
// escapes the ROOT LAYOUT; anything thrown while rendering a page fell through
// to Next.js's unstyled 500 page, which drops the operator out of the panel
// entirely — no sidebar, no way back except the browser's back button.
// This one renders inside the root layout, keeps the admin styling, and offers
// both a retry and a way back to the dashboard.
import * as Sentry from "@sentry/nextjs";
import { AlertTriangle, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useEffect } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

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
    <main className="min-h-screen grid place-items-center bg-muted/30 p-6">
      <Card className="w-full max-w-md">
        <CardContent className="pt-6 text-center">
          <div className="mx-auto w-12 h-12 rounded-xl bg-destructive/10 text-destructive grid place-items-center">
            <AlertTriangle className="w-6 h-6" />
          </div>
          <h1 className="mt-4 text-lg font-semibold tracking-tight">Страница не открылась</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            Произошла непредвиденная ошибка. Мы уже получили уведомление — попробуйте ещё раз.
          </p>
          {/* The digest is the only handle Sentry and the operator share; without
              it a bug report is "что-то сломалось" and nobody can find the event. */}
          {error.digest && (
            <p className="mt-2 text-xs text-muted-foreground">
              Код для поддержки: <code>{error.digest}</code>
            </p>
          )}
          <div className="mt-6 flex items-center justify-center gap-2">
            <Button onClick={() => reset()}>
              <RefreshCw className="w-4 h-4" />
              Попробовать снова
            </Button>
            <Button variant="outline" asChild>
              <Link href="/">На дашборд</Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    </main>
  );
}
