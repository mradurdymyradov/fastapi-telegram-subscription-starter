import { NextRequest, NextResponse } from "next/server";

import { PORTAL_COOKIE, externalBase } from "@/lib/backend";

const BACKEND = process.env.BACKEND_URL || "http://api:8000";

export const dynamic = "force-dynamic";

// Revoke the server-side session row, then clear the cookie and return to the
// landing page. GET so a plain link works; the action is idempotent and not a
// state mutation that needs CSRF protection (it only ends your own session).
export async function GET(req: NextRequest) {
  const origin = externalBase(req);
  const token = req.cookies.get(PORTAL_COOKIE)?.value;
  if (token) {
    try {
      await fetch(`${BACKEND}/api/portal/logout`, {
        method: "POST",
        headers: { cookie: `${PORTAL_COOKIE}=${token}` },
        cache: "no-store",
      });
    } catch {
      // Best-effort: even if the backend call fails we still drop the cookie.
    }
  }
  const res = NextResponse.redirect(new URL("/?error=loggedout", origin));
  res.cookies.delete(PORTAL_COOKIE);
  return res;
}
