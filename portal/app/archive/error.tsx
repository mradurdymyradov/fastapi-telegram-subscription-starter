"use client";

// GK-467. Segment boundary for the archive. Same failure as the root
// `error.tsx`, but it keeps the portal header on screen — so a member whose
// video page died can still reach the other sections, their profile, or the
// exit, instead of landing on a dead end with one button.
import * as Sentry from "@sentry/nextjs";
import { useEffect } from "react";

import { PortalHeader } from "@/components/portal-header";
import { ReloadButton } from "@/components/reload-button";
import { ServiceUnavailable } from "@/components/service-unavailable";

export default function ArchiveError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <>
      <PortalHeader active="archive" />
      <main className="container max-w-3xl py-16">
        <ServiceUnavailable action={<ReloadButton onClick={() => reset()} />} />
      </main>
    </>
  );
}
