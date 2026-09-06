const { withSentryConfig } = require("@sentry/nextjs");

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  // Next 14 still gates the instrumentation.ts hook behind this flag (stable in
  // Next 15). Required so GK-041's server/edge Sentry init actually loads.
  experimental: { instrumentationHook: true },
  // Same-origin proxy so client-side fetches and the bot's magic-link land on
  // the FastAPI backend. In the deployed topology Caddy proxies /api/* directly
  // to the API; this rewrite is the fallback for local `next dev` / standalone.
  async rewrites() {
    const backend = process.env.BACKEND_URL || "http://api:8000";
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

// GK-041: Sentry build wrapper (mirrors admin/next.config.js). Browser events
// tunnel through a same-origin /monitoring route so the portal's Vimeo-aware
// CSP (connect-src 'self' https://player.vimeo.com) needs no Sentry host added.
// Source-map upload is OFF unless SENTRY_AUTH_TOKEN (kept out of git) is set.
const sentryBuildOptions = {
  org: process.env.SENTRY_ORG,
  project: process.env.SENTRY_PROJECT,
  authToken: process.env.SENTRY_AUTH_TOKEN,
  release: { name: process.env.SENTRY_RELEASE || undefined },
  tunnelRoute: "/monitoring",
  sourcemaps: { disable: !process.env.SENTRY_AUTH_TOKEN },
  silent: true,
  disableLogger: true,
};

module.exports = withSentryConfig(nextConfig, sentryBuildOptions);
