"use client";

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Play, RefreshCw, RotateCcw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Select } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/utils";

interface ReconciliationRun {
  id: number;
  status: string;
  triggered_by: string;
  provider_scope: string;
  started_at: string;
  finished_at: string | null;
  items_count: number;
  open_items_count: number;
  summary: Record<string, unknown>;
  error: string | null;
}

interface ReconciliationItem {
  id: number;
  run_id: number;
  provider: string;
  severity: string;
  issue_type: string;
  entity_type: string;
  entity_id: string | null;
  external_id: string | null;
  status: string;
  title: string;
  description: string;
  expected_state: Record<string, unknown>;
  observed_state: Record<string, unknown>;
  resolve_note: string | null;
  resolved_by_admin_id: number | null;
  resolved_by_email: string | null;
  resolved_at: string | null;
  created_at: string;
  first_seen_at: string | null;
}

/**
 * GK-430: a finding detected again tonight is not the same news as one detected
 * for the first time. `first_seen_at` is carried across runs, so a condition that
 * has stood since June says so instead of claiming it appeared last night.
 */
function isNewInRun(item: ReconciliationItem, run: ReconciliationRun | null): boolean {
  if (!run || !item.first_seen_at) return false;
  return Date.parse(item.first_seen_at) >= Date.parse(run.started_at);
}

const severityBadge: Record<string, "info" | "warning" | "destructive" | "muted"> = {
  info: "info",
  warning: "warning",
  critical: "destructive",
};

const statusBadge: Record<string, "success" | "warning" | "muted"> = {
  open: "warning",
  resolved: "success",
  completed: "success",
  completed_with_errors: "warning",
  running: "muted",
  failed: "warning",
};

export default function ReconciliationPage() {
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  const requestedRun = Number(searchParams.get("run") || "");
  const [selectedRunId, setSelectedRunId] = useState<number | null>(
    Number.isFinite(requestedRun) && requestedRun > 0 ? requestedRun : null
  );
  const [statusFilter, setStatusFilter] = useState("open");
  const [providerFilter, setProviderFilter] = useState("");
  const [resolving, setResolving] = useState<ReconciliationItem | null>(null);
  const [resolveNote, setResolveNote] = useState("");

  const runs = useQuery<{ items: ReconciliationRun[]; total: number }>({
    queryKey: ["reconciliation-runs"],
    queryFn: () => api("/reconciliation/runs?limit=50"),
    refetchInterval: 60_000,
  });

  useEffect(() => {
    if (selectedRunId || !runs.data?.items.length) return;
    setSelectedRunId(runs.data.items[0].id);
  }, [runs.data?.items, selectedRunId]);

  const activeRun = useMemo(
    () => runs.data?.items.find((run) => run.id === selectedRunId) ?? null,
    [runs.data?.items, selectedRunId]
  );

  const items = useQuery<{ items: ReconciliationItem[]; total: number }>({
    queryKey: ["reconciliation-items", selectedRunId, statusFilter, providerFilter],
    enabled: selectedRunId !== null,
    queryFn: () => {
      const params = new URLSearchParams({ limit: "300" });
      if (statusFilter) params.set("status", statusFilter);
      if (providerFilter) params.set("provider", providerFilter);
      return api(`/reconciliation/runs/${selectedRunId}/items?${params.toString()}`);
    },
    refetchInterval: 60_000,
  });

  const triggerRun = useMutation({
    mutationFn: () =>
      api<ReconciliationRun>("/reconciliation/runs", {
        method: "POST",
        body: JSON.stringify({
          providers: providerFilter ? [providerFilter] : undefined,
        }),
      }),
    onSuccess: (run) => {
      setSelectedRunId(run.id);
      queryClient.invalidateQueries({ queryKey: ["reconciliation-runs"] });
      queryClient.invalidateQueries({ queryKey: ["reconciliation-items"] });
    },
  });

  const resolveMutation = useMutation({
    mutationFn: ({
      item,
      action,
      note,
    }: {
      item: ReconciliationItem;
      action: "resolve" | "reopen";
      note?: string;
    }) =>
      api<ReconciliationItem>(`/reconciliation/items/${item.id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ action, note }),
      }),
    onSuccess: () => {
      setResolving(null);
      setResolveNote("");
      queryClient.invalidateQueries({ queryKey: ["reconciliation-runs"] });
      queryClient.invalidateQueries({ queryKey: ["reconciliation-items"] });
    },
  });

  const rows = items.data?.items ?? [];
  const byProvider = (activeRun?.summary?.by_provider ?? {}) as Record<string, number>;
  const bySeverity = (activeRun?.summary?.by_severity ?? {}) as Record<string, number>;
  const newItems = Number(activeRun?.summary?.new_items ?? 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Сверка платежей</h1>
          <p className="text-sm text-muted-foreground">
            {runs.isError ? "количество запусков неизвестно" : `${runs.data?.total ?? 0} запусков`}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => queryClient.invalidateQueries({ queryKey: ["reconciliation-runs"] })}>
            <RefreshCw className="h-4 w-4" />
            Обновить
          </Button>
          <Button onClick={() => triggerRun.mutate()} disabled={triggerRun.isPending}>
            <Play className="h-4 w-4" />
            Запустить
          </Button>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[360px_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>Запуски</CardTitle>
          </CardHeader>
          <CardContent>
            {/* GK-468: with runs unloaded the whole right-hand side falls back
                to zeros — "0 открытых, 0 critical" reads as a clean reconciliation
                when in fact nothing was read at all. */}
            {runs.isError && (
              <QueryError
                error={runs.error}
                onRetry={() => runs.refetch()}
                retrying={runs.isFetching}
                title="Запуски сверки не загрузились"
                description="Счётчики справа показывают нули только потому, что данных нет."
                className="mb-4"
              />
            )}
            <div className="space-y-2">
              {runs.data?.items.map((run) => (
                <button
                  key={run.id}
                  type="button"
                  onClick={() => setSelectedRunId(run.id)}
                  className={`w-full rounded-md border px-3 py-3 text-left transition-colors ${
                    run.id === selectedRunId ? "border-primary bg-primary/5" : "hover:bg-muted"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs">#{run.id}</span>
                    <Badge variant={statusBadge[run.status] ?? "muted"}>{run.status}</Badge>
                  </div>
                  <div className="mt-2 text-sm font-medium">
                    {run.open_items_count} открытых / {run.items_count} всего
                    {Number(run.summary?.new_items ?? 0) > 0 && (
                      <span className="ml-1 text-primary">
                        · {Number(run.summary?.new_items ?? 0)} новых
                      </span>
                    )}
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {formatDateTime(run.started_at)} · {run.provider_scope} · {run.triggered_by}
                  </div>
                </button>
              ))}
              {runs.data?.items.length === 0 && (
                <div className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
                  Запусков пока нет.
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        <div className="space-y-4">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <Card>
              <CardContent className="pt-6">
                <div className="text-sm text-muted-foreground">Открытые</div>
                <div className="mt-1 text-2xl font-semibold">{activeRun?.open_items_count ?? 0}</div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <div className="text-sm text-muted-foreground">Новые</div>
                <div className="mt-1 text-2xl font-semibold">{newItems}</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  впервые за всю историю сверок
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <div className="text-sm text-muted-foreground">Critical</div>
                <div className="mt-1 text-2xl font-semibold">{bySeverity.critical ?? 0}</div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <div className="text-sm text-muted-foreground">Stripe / Lava / USDT</div>
                <div className="mt-1 text-2xl font-semibold">
                  {byProvider.stripe ?? 0} / {byProvider.lava ?? 0} / {byProvider.usdt ?? 0}
                </div>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <CardTitle>Расхождения</CardTitle>
                <div className="flex flex-wrap gap-2">
                  <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="max-w-[160px]">
                    <option value="">Все статусы</option>
                    <option value="open">Открытые</option>
                    <option value="resolved">Решенные</option>
                  </Select>
                  <Select value={providerFilter} onChange={(e) => setProviderFilter(e.target.value)} className="max-w-[160px]">
                    <option value="">Все методы</option>
                    <option value="stripe">Stripe</option>
                    <option value="lava">Lava</option>
                    <option value="usdt">USDT</option>
                  </Select>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              {items.isError && (
                <QueryError
                  error={items.error}
                  onRetry={() => items.refetch()}
                  retrying={items.isFetching}
                  title="Расхождения не загрузились"
                  description="Пустая таблица ниже — это отсутствие ответа, а не отсутствие расхождений."
                  className="mb-4"
                />
              )}
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Сигнал</TableHead>
                    <TableHead>Сущность</TableHead>
                    <TableHead>Состояние</TableHead>
                    <TableHead>Решение</TableHead>
                    <TableHead className="text-right">Действие</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((item) => (
                    <TableRow key={item.id}>
                      <TableCell className="min-w-[280px]">
                        <div className="flex flex-wrap gap-2">
                          <Badge variant={severityBadge[item.severity] ?? "muted"}>{item.severity}</Badge>
                          <Badge variant="muted">{item.provider}</Badge>
                          <Badge variant="outline">{item.issue_type}</Badge>
                          {isNewInRun(item, activeRun) && <Badge variant="info">новое</Badge>}
                        </div>
                        <div className="mt-2 font-medium">{item.title}</div>
                        <div className="mt-1 text-sm text-muted-foreground">{item.description}</div>
                        <details className="mt-2 text-xs">
                          <summary className="cursor-pointer text-muted-foreground">State</summary>
                          <div className="mt-2 grid gap-2 xl:grid-cols-2">
                            <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 whitespace-pre-wrap break-words">
                              {JSON.stringify(item.expected_state, null, 2)}
                            </pre>
                            <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 whitespace-pre-wrap break-words">
                              {JSON.stringify(item.observed_state, null, 2)}
                            </pre>
                          </div>
                        </details>
                      </TableCell>
                      <TableCell>
                        <div className="text-sm">{item.entity_type}</div>
                        <div className="font-mono text-xs text-muted-foreground">
                          {item.entity_id ?? "—"}
                        </div>
                        {item.external_id && (
                          <div className="mt-1 max-w-[220px] truncate font-mono text-xs text-muted-foreground">
                            {item.external_id}
                          </div>
                        )}
                      </TableCell>
                      <TableCell>
                        <Badge variant={statusBadge[item.status] ?? "muted"}>{item.status}</Badge>
                        <div className="mt-2 text-xs text-muted-foreground">
                          {formatDateTime(item.created_at)}
                        </div>
                        {item.first_seen_at && !isNewInRun(item, activeRun) && (
                          <div className="mt-1 text-xs text-muted-foreground">
                            стоит с {formatDateTime(item.first_seen_at)}
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="min-w-[220px]">
                        {item.resolved_at ? (
                          <div className="text-sm">
                            <div>{item.resolve_note}</div>
                            <div className="mt-1 text-xs text-muted-foreground">
                              {item.resolved_by_email ?? `admin #${item.resolved_by_admin_id}`} ·{" "}
                              {formatDateTime(item.resolved_at)}
                            </div>
                          </div>
                        ) : (
                          <span className="text-sm text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        {item.status === "open" ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => {
                              setResolving(item);
                              setResolveNote("");
                            }}
                          >
                            <CheckCircle2 className="h-4 w-4" />
                            Решить
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => resolveMutation.mutate({ item, action: "reopen" })}
                          >
                            <RotateCcw className="h-4 w-4" />
                            Открыть
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                  {rows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                        Нет записей.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </div>
      </div>

      <Dialog open={resolving !== null} onOpenChange={(open) => !open && setResolving(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Решить расхождение</DialogTitle>
            <DialogDescription>{resolving?.title}</DialogDescription>
          </DialogHeader>
          <Textarea
            value={resolveNote}
            onChange={(e) => setResolveNote(e.target.value)}
            placeholder="Что проверено и почему расхождение закрыто"
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setResolving(null)}>
              Отмена
            </Button>
            <Button
              disabled={!resolveNote.trim() || !resolving || resolveMutation.isPending}
              onClick={() =>
                resolving &&
                resolveMutation.mutate({
                  item: resolving,
                  action: "resolve",
                  note: resolveNote.trim(),
                })
              }
            >
              <CheckCircle2 className="h-4 w-4" />
              Сохранить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
