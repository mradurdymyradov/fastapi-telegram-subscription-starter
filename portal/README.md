# membership_saas Member Portal (GK-091)

Closed web archive for active subscribers. Videos stay on Vimeo; this app only
lists synced metadata and gates access through the membership_saas subscription DB.

- **Login:** Telegram bot issues a one-time magic link → `/auth/magic` redeems it
  via the backend and sets an HttpOnly `membership_portal_session` cookie → ~30-day
  sliding session. Every protected request re-checks `has_portal_access`.
- **Routes:** `/` landing · `/auth/magic` redeem · `/auth/logout` · `/archive`
  list · `/archive/v/[vimeoId]` player · `/me` profile.
- **Backend:** all data via same-origin `/api/portal/*` (FastAPI). Server
  components forward the session cookie to `BACKEND_URL` internally.

## Env

| Var | Purpose | Example |
|---|---|---|
| `BACKEND_URL` | Internal API base for server-side fetches | `http://api:8000` |
| `NEXT_PUBLIC_BOT_USERNAME` | Bot handle for the landing/deep-link CTA | `membership_bot` |

## Vimeo privacy (set per video by Grant/Owner, see GK-090 §6)

"Hide from Vimeo" + embed restricted to `community.example.com`. The portal
Caddy block must use `Referrer-Policy: strict-origin-when-cross-origin` (NOT
`no-referrer`) or Vimeo domain privacy blocks playback. Direct-link leakage is an
accepted launch limitation (BLK-010) — access is revoked in our DB, not on Vimeo.

## Dev

```bash
npm install
BACKEND_URL=http://localhost:8000 npm run dev   # http://localhost:3001
```
