// GK-041: Next.js instrumentation hook for the member video portal. Loaded once
// per server runtime at startup (enabled via experimental.instrumentationHook
// in next.config.js for Next 14). Imports the matching Sentry init for the
// active runtime so SSR and middleware/edge errors are captured.
export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    await import("./sentry.server.config");
  }
  if (process.env.NEXT_RUNTIME === "edge") {
    await import("./sentry.edge.config");
  }
}
