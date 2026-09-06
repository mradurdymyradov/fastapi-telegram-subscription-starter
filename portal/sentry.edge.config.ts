// GK-041: edge-runtime Sentry for the member video portal. The portal ships a
// middleware.ts (session gate) that runs in the edge runtime, so this init
// captures middleware errors. Same DSN resolution as the server config; no-op
// when no DSN is configured.
import * as Sentry from "@sentry/nextjs";

const dsn = process.env.SENTRY_DSN || process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    environment:
      process.env.SENTRY_ENVIRONMENT ||
      process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT ||
      "development",
    tracesSampleRate: Number(
      process.env.SENTRY_TRACES_SAMPLE_RATE ??
        process.env.NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE ??
        0,
    ),
    sendDefaultPii: false,
  });
}
