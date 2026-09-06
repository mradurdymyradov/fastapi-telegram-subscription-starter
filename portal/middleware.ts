import { NextRequest, NextResponse } from "next/server";

// Cheap edge gate: bounce requests to protected pages that have no session
// cookie at all straight to the landing page. This is NOT the real access
// check — the backend re-validates the session and re-runs `has_portal_access`
// on every `/api/portal/*` call — it just avoids rendering a shell for an
// obviously-anonymous visitor.
export function middleware(req: NextRequest) {
  if (req.cookies.has("membership_portal_session")) return NextResponse.next();
  const url = req.nextUrl.clone();
  url.pathname = "/";
  url.search = "?error=login";
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/archive/:path*", "/me"],
};
