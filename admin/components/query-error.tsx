"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

// GK-468. Until this existed, a failed query rendered exactly like an empty
// one: "—", "Пока пусто", an empty table. An operator could not tell "нет
// выручки на этой неделе" from "эндпоинт отвечает 500", which is the difference
// between a quiet week and an outage nobody is looking at.
//
// One component for all of it, so the failure state is the same everywhere and
// nobody has to invent a seventeenth variant.

/** Short technical line under the headline. Operators quote it to support, so
 *  it says what actually happened rather than a generic apology. */
export function errorDetail(error: unknown): string {
  if (error instanceof ApiError) return `Сервер ответил ошибкой ${error.status}.`;
  return "Нет связи с сервером.";
}

export function QueryError({
  error,
  onRetry,
  retrying = false,
  title = "Не удалось загрузить данные",
  description,
  className,
}: {
  error?: unknown;
  onRetry?: () => void;
  /** `isFetching` of the query — disables the button while the retry is in flight. */
  retrying?: boolean;
  title?: string;
  /** Extra sentence for places where "what you're NOT seeing" needs spelling out. */
  description?: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        // flex-wrap + a min-width on the text: inside a narrow card (the
        // dashboard's referrer column) the retry button drops to its own line
        // instead of being squeezed into two characters per word.
        "flex flex-wrap items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3",
        className
      )}
    >
      <AlertTriangle className="w-5 h-5 shrink-0 mt-0.5 text-destructive" />
      <div className="min-w-[200px] flex-1 space-y-1">
        <div className="text-sm font-medium">{title}</div>
        <div className="text-xs text-muted-foreground">
          {errorDetail(error)}
          {description ? ` ${description}` : ""}
        </div>
      </div>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry} disabled={retrying} className="shrink-0">
          <RefreshCw className={cn("w-3.5 h-3.5", retrying && "animate-spin")} />
          {retrying ? "Обновляю…" : "Повторить"}
        </Button>
      )}
    </div>
  );
}

/** The subset of a TanStack query result this needs — deliberately structural
 *  so `useQuery` and `useInfiniteQuery` results both satisfy it. */
export interface QueryLike {
  isError: boolean;
  error: unknown;
  isFetching: boolean;
  refetch: () => unknown;
}

/** One banner for a screen built from several queries, so a page with six
 *  endpoints doesn't grow six stacked error boxes. Retry re-runs every query
 *  that actually failed, not all of them. */
export function QueryErrorGroup({
  queries,
  title,
  description,
  className,
}: {
  queries: QueryLike[];
  title?: string;
  description?: string;
  className?: string;
}) {
  const failed = queries.filter((q) => q.isError);
  if (failed.length === 0) return null;
  return (
    <QueryError
      error={failed[0].error}
      onRetry={() => failed.forEach((q) => q.refetch())}
      retrying={failed.some((q) => q.isFetching)}
      title={title}
      description={description}
      className={className}
    />
  );
}
