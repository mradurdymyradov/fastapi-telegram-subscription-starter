// GK-041: Next.js instrumentation hook. Loaded once per server runtime at
// startup (enabled via experimental.instrumentationHook in next.config.js for
// Next 14). Imports the matching Sentry init for the active runtime so SSR and
// edge errors are captured. The browser init lives in sentry.client.config.ts,
// which withSentryConfig wires into the client bundle automatically.
export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    await import("./sentry.server.config");
  }
  if (process.env.NEXT_RUNTIME === "edge") {
    await import("./sentry.edge.config");
  }
}
