"use client";

// GK-041: top-level error boundary for the member video portal. Next.js renders
// this when an error escapes the root layout (a React render crash). It declares
// its own <html>/<body> (it replaces the root layout) with inline styles so it
// renders even if globals.css failed to load. Reports to the portal Sentry
// project (no-op when Sentry is unconfigured).
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
            Не удалось загрузить страницу. Мы уже получили уведомление —
            попробуйте обновить.
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
            Обновить
          </button>
        </div>
      </body>
    </html>
  );
}
