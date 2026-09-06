const { withSentryConfig } = require("@sentry/nextjs");

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  // Next 14 still gates the instrumentation.ts hook behind this flag (stable in
  // Next 15). Required so GK-041's server/edge Sentry init actually loads.
  experimental: { instrumentationHook: true },
  async rewrites() {
    const backend = process.env.BACKEND_URL || "http://api:8000";
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
    ];
  },
};

// GK-041: Sentry build wrapper. Injects the browser init, the release, and the
// /monitoring tunnel route. Source-map upload to Sentry is OFF unless an auth
// token (kept out of git) is supplied, so the default Docker build needs no
// Sentry secrets and still succeeds.
const sentryBuildOptions = {
  org: process.env.SENTRY_ORG,
  project: process.env.SENTRY_PROJECT,
  authToken: process.env.SENTRY_AUTH_TOKEN,
  release: { name: process.env.SENTRY_RELEASE || undefined },
  // Tunnel browser events through a same-origin route so the strict Caddy CSP
  // (connect-src 'self') and ad-blockers don't drop them.
  tunnelRoute: "/monitoring",
  // Only upload source maps when a token is present; otherwise skip upload so
  // builds stay offline-friendly and never need network/Sentry credentials.
  sourcemaps: { disable: !process.env.SENTRY_AUTH_TOKEN },
  // Keep build output quiet and tree-shake the Sentry SDK logger from bundles.
  silent: true,
  disableLogger: true,
};

module.exports = withSentryConfig(nextConfig, sentryBuildOptions);
