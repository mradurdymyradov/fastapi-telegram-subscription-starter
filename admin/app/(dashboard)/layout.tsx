"use client";

import { useQuery } from "@tanstack/react-query";
import { Sidebar } from "@/components/sidebar";
import { AuthGuard } from "@/components/auth-guard";
import { api } from "@/lib/api";

interface Summary {
  awaiting_review: number;
}

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const { data } = useQuery<Summary>({
    queryKey: ["metrics-summary"],
    queryFn: () => api<Summary>("/metrics/summary"),
    refetchInterval: 60_000,
  });

  return (
    <AuthGuard>
      <div className="flex min-h-screen bg-muted/30">
        <Sidebar awaitingCount={data?.awaiting_review ?? 0} />
        <main className="flex-1 min-w-0">
          <div className="max-w-[1400px] mx-auto px-6 py-6">{children}</div>
        </main>
      </div>
    </AuthGuard>
  );
}
