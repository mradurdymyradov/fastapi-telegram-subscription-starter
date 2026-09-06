// GK-041: server-side (Node runtime) Sentry for the member video portal.
// Runs inside `node server.js`; reads runtime env from docker-compose. Prefers
// the non-public SENTRY_DSN, falling back to NEXT_PUBLIC_SENTRY_DSN. No-op when
// no DSN is configured.
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
