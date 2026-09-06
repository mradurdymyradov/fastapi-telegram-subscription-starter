"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getToken } from "@/lib/api";

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setReady(true);
  }, [router]);
  if (!ready) {
    return <div className="min-h-screen grid place-items-center text-muted-foreground text-sm">Загрузка…</div>;
  }
  return <>{children}</>;
}
