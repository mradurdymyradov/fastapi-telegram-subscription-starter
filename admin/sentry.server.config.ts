// GK-041: server-side (Node runtime) Sentry for the admin UI.
//
// Runs inside `node server.js` (Next.js standalone), so it reads runtime env
// passed by docker-compose. Prefer the non-public SENTRY_DSN; fall back to the
// build-time NEXT_PUBLIC_SENTRY_DSN so a single configured value covers both
// sides. Empty DSN => no-op.
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
