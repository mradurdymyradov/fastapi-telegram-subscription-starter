"use client";

// GK-041: top-level error boundary for the admin UI. Next.js renders this when
// an error escapes the root layout (a React render crash) — the class of errors
// the passive browser SDK can't catch on its own. It must declare its own
// <html>/<body> because it replaces the root layout, and styles are inline so it
// renders even if globals.css failed to load. We report to the admin Sentry
// project here (no-op when Sentry is unconfigured).
import * as Sentry from "@sentry/nextjs";
import { useEffect } from "react";

export default function GlobalError({
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
    <html lang="ru">
      <body
        style={{
          fontFamily: "system-ui, -apple-system, sans-serif",
          display: "flex",
          minHeight: "100vh",
          alignItems: "center",
          justifyContent: "center",
          margin: 0,
        }}
      >
        <div style={{ textAlign: "center", maxWidth: 420, padding: "2rem" }}>
          <h2 style={{ marginBottom: "0.5rem" }}>Что-то пошло не так</h2>
          <p style={{ color: "#666", marginBottom: "1.5rem" }}>
            Произошла непредвиденная ошибка. Мы уже получили уведомление.
          </p>
          <button
            onClick={() => reset()}
            style={{
              padding: "0.5rem 1.25rem",
              borderRadius: 8,
              border: "1px solid #ccc",
              cursor: "pointer",
            }}
          >
            Попробовать снова
          </button>
        </div>
      </body>
    </html>
  );
}
