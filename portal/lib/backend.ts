import { cookies } from "next/headers";
import type { NextRequest } from "next/server";

// The externally-visible base URL for redirects. In Next.js standalone,
// `req.nextUrl.origin` reflects the server's bind hostname (0.0.0.0), which a
// browser can't navigate to — so derive it from the Host header the client
// actually used. Behind Caddy that's the prod domain + X-Forwarded-Proto: https.
export function externalBase(req: NextRequest): string {
  const host = req.headers.get("host") ?? "localhost:3001";
  const proto =
    req.headers.get("x-forwarded-proto") ??
    (host.startsWith("localhost") || host.startsWith("127.0.0.1") ? "http" : "https");
  return `${proto}://${host}`;
}

// Server-side calls into the FastAPI backend. The portal app is internal-only
// and reaches the API directly over the compose network; the browser never sees
// these. The `membership_portal_session` cookie is forwarded so the backend can resolve
// + slide the session and re-check `has_portal_access`.
const BACKEND = process.env.BACKEND_URL || "http://api:8000";
export const PORTAL_COOKIE = "membership_portal_session";
const BACKEND_TIMEOUT_MS = 8000;

// GK-467. Two distinct failures, because they mean different things to the
// member: `unreachable` = we never got an answer at all (connection refused
// while the api container restarts, a timeout, or a body that wasn't the JSON
// we were promised); otherwise the backend answered and `status` is what it
// said. Treating the first as "your session died" would sign people out during
// every deploy, which is exactly what used to happen.
export type BackendFailure =
  | { ok: false; unreachable: true; status?: undefined }
  | { ok: false; unreachable: false; status: number };

export type BackendResult<T> = { ok: true; data: T } | BackendFailure;

/** Nothing the member did wrong and nothing they can fix — show the calm
 *  "недоступен, попробуйте через минуту" panel and let them retry. Callers
 *  must check this BEFORE reading `status`, or a 5xx blip reads as a logout. */
export const backendDown = (res: BackendFailure): boolean =>
  res.unreachable || res.status >= 500;

// The api container is restarted by every `up -d --build`, so a refused
// connection here is routine, not exotic. Nothing in this function throws.
export async function backendGet<T>(path: string): Promise<BackendResult<T>> {
  const token = cookies().get(PORTAL_COOKIE)?.value;
  let res: Response;
  try {
    res = await fetch(`${BACKEND}/api${path}`, {
      headers: token ? { cookie: `${PORTAL_COOKIE}=${token}` } : {},
      cache: "no-store",
      // A restarting container can accept the socket and then never answer.
      // Without this the page hangs until Next.js gives up and renders a 500.
      signal: AbortSignal.timeout(BACKEND_TIMEOUT_MS),
    });
  } catch {
    return { ok: false, unreachable: true };
  }
  if (!res.ok) return { ok: false, unreachable: false, status: res.status };
  try {
    return { ok: true, data: (await res.json()) as T };
  } catch {
    // 2xx with a truncated / non-JSON body — same practical outcome as no answer.
    return { ok: false, unreachable: true };
  }
}

export const BOT_USERNAME =
  process.env.NEXT_PUBLIC_BOT_USERNAME || "membership_bot";

export const botDeepLink = (payload = "portal") =>
  `https://t.me/${BOT_USERNAME}?start=${payload}`;
