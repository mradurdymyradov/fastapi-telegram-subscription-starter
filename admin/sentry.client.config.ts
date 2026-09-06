// GK-041: browser-side Sentry for the admin UI.
//
// This file is bundled into the client and runs in the visitor's browser, so it
// can only see env vars that Next.js inlines at build time (the NEXT_PUBLIC_*
// prefix). The DSN is therefore passed as a Docker build ARG (see Dockerfile /
// docker-compose.yml), not a runtime env. A DSN is a write-only ingestion key,
// so exposing it to the browser is expected and safe.
//
// Empty DSN => init is a no-op, so local/demo builds with no Sentry configured
// still run without errors (mirrors the backend's "empty SENTRY_DSN disables").
import * as Sentry from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT || "development",
    // Release is injected at build time by withSentryConfig (release.name).
    // Performance tracing defaults off; raise via SENTRY_TRACES_SAMPLE_RATE.
    tracesSampleRate: Number(
      process.env.NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE ?? 0,
    ),
    // Privacy: never attach IPs / cookies / request bodies by default (GK-041).
    sendDefaultPii: false,
  });
}
