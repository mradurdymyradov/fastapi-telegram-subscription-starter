import { PortalHeader } from "@/components/portal-header";
import { ServiceUnavailable } from "@/components/service-unavailable";

// GK-467. Whole-page version of the failure panel, with the header kept so the
// member can still move around (other sections, profile, exit) while whatever
// broke recovers. Every portal page that talks to the backend renders this when
// `backendDown(res)` — see lib/backend.ts.
export function PortalUnavailable({ active }: { active?: "archive" | "me" }) {
  return (
    <>
      <PortalHeader active={active} />
      <main className="container max-w-5xl py-8">
        <ServiceUnavailable />
      </main>
    </>
  );
}
