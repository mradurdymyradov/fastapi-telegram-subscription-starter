"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { ApiError } from "@/lib/api";

// GK-468. `retry: 1` was the wrong shape for a panel a human sits in front of:
// one immediate retry rides out nothing (a restarting api container is down for
// seconds, not milliseconds), and once both attempts were spent the query stayed
// failed until somebody thought to reload the page.
//
// Two retries with a short exponential back-off (0.5s, 1s) covers a container
// restart or a momentary blip without the operator noticing, and still surfaces
// a real outage in ~2s rather than pretending. Deliberately NOT longer: past a
// couple of seconds the panel is lying about how long it will keep trying, and
// every failed query now has its own visible retry button anyway.
//
// 4xx is never retried — it is an answer, not a blip. A 401 has already sent us
// to /login inside `api()` by this point, and retrying a 403/404 just burns time
// before showing the error the operator needs to see.
export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
            retry: (failureCount, error) => {
              if (error instanceof ApiError && error.status < 500) return false;
              return failureCount < 2;
            },
            retryDelay: (attempt) => Math.min(500 * 2 ** attempt, 4000),
          },
        },
      })
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
