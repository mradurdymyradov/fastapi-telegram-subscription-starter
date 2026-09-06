// GK-041: browser-side Sentry for the member video portal.
//
// Bundled into the client, so it only sees build-time-inlined NEXT_PUBLIC_*
// env. The DSN is passed as a Docker build ARG (a DSN is a write-only ingestion
// key, so browser exposure is expected). Empty DSN => init is a no-op.
//
// The portal uses its OWN DSN (PORTAL_SENTRY_DSN in .env) so its events land in
// a different Sentry project than the admin and the backend/bot.
import * as Sentry from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT || "development",
    tracesSampleRate: Number(
      process.env.NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE ?? 0,
    ),
    // Privacy: members watch private Vimeo content — never attach PII by
    // default (GK-041; consistent with the relaxed-referrer portal posture).
    sendDefaultPii: false,
  });
}
