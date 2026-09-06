FROM node:20-alpine AS deps
WORKDIR /app
# Harden npm against flaky / short-lived connections (the MTU/VPN download
# breakage documented in memory `docker_local_blocker`): retry transient registry
# failures instead of letting an IncompleteRead silently abort a download.
RUN npm config set fetch-retries 5 \
 && npm config set fetch-retry-mintimeout 20000 \
 && npm config set fetch-retry-maxtimeout 120000
COPY package.json ./
RUN npm install --no-audit --no-fund
# Pin the platform SWC binary so `next build` never has to download it mid-build.
# node:20-alpine is musl-based, so the needed package is @next/swc-linux-x64-musl
# (NOT the -gnu/glibc variant). The version is derived from the installed `next`
# so it can never drift on a Next.js bump. Installing it as an explicit, non-
# optional step means a failed download fails the build loudly HERE, instead of
# being swallowed as an optional dep and re-attempted (and dying) during `next
# build`.
RUN npm install --no-audit --no-fund --no-save \
      "@next/swc-linux-x64-musl@$(node -p "require('next/package.json').version")"

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# GK-041: Sentry build inputs. NEXT_PUBLIC_* values are inlined into the client
# bundle by `next build`, so the browser DSN must be present HERE (build time),
# not at runtime. Source-map upload stays off unless SENTRY_AUTH_TOKEN is set
# (see next.config.js). All default to empty so a build with no Sentry creds
# still succeeds. Note: build ARGs land in image history — only the optional
# SENTRY_AUTH_TOKEN is sensitive and it lives solely in the host .env, never git.
ARG NEXT_PUBLIC_SENTRY_DSN=""
ARG NEXT_PUBLIC_SENTRY_ENVIRONMENT=""
ARG NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE=""
ARG SENTRY_RELEASE=""
ARG SENTRY_ORG=""
ARG SENTRY_PROJECT=""
ARG SENTRY_AUTH_TOKEN=""
ENV NEXT_PUBLIC_SENTRY_DSN=$NEXT_PUBLIC_SENTRY_DSN \
    NEXT_PUBLIC_SENTRY_ENVIRONMENT=$NEXT_PUBLIC_SENTRY_ENVIRONMENT \
    NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE=$NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE \
    SENTRY_RELEASE=$SENTRY_RELEASE \
    SENTRY_ORG=$SENTRY_ORG \
    SENTRY_PROJECT=$SENTRY_PROJECT \
    SENTRY_AUTH_TOKEN=$SENTRY_AUTH_TOKEN
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3001
ENV PORT=3001 HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
