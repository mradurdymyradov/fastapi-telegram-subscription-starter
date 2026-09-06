import { NextRequest, NextResponse } from "next/server";

import { PORTAL_COOKIE, externalBase } from "@/lib/backend";

const BACKEND = process.env.BACKEND_URL || "http://api:8000";

export const dynamic = "force-dynamic";

// Redeems a one-time magic-link token: server-side POST to the backend, which
// validates the token, re-checks subscription access, and opens a session. On
// success we set the HttpOnly session cookie from the returned token and send
// the user into the archive. The raw token never reaches the browser as JSON.
export async function GET(req: NextRequest) {
  const origin = externalBase(req);
  const token = req.nextUrl.searchParams.get("token");
  if (!token) {
    return NextResponse.redirect(new URL("/?error=link", origin));
  }

  let res: Response;
  try {
    res = await fetch(`${BACKEND}/api/portal/redeem`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-forwarded-for": req.headers.get("x-forwarded-for") ?? "",
      },
      body: JSON.stringify({ token }),
      cache: "no-store",
    });
  } catch {
    return NextResponse.redirect(new URL("/?error=server", origin));
  }

  if (!res.ok) {
    const reason = res.status === 403 ? "inactive" : "link";
    return NextResponse.redirect(new URL(`/?error=${reason}`, origin));
  }

  const { session_token, max_age_seconds } = (await res.json()) as {
    session_token: string;
    max_age_seconds: number;
  };

  const redirect = NextResponse.redirect(new URL("/archive", origin));
  redirect.cookies.set({
    name: PORTAL_COOKIE,
    value: session_token,
    httpOnly: true,
    // Secure by default (prod = HTTPS). Set PORTAL_COOKIE_SECURE=false only for
    // local plain-HTTP testing on http://localhost where a Secure cookie may be
    // dropped by some browsers.
    secure: process.env.PORTAL_COOKIE_SECURE !== "false",
    sameSite: "lax",
    path: "/",
    maxAge: max_age_seconds,
  });
  return redirect;
}
