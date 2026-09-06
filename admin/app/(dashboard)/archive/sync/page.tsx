"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { RefreshCw, AlertTriangle, CheckCircle2 } from "lucide-react";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/utils";

interface SyncStatus {
  token_configured: boolean;
  last_synced_at: string | null;
  total_videos: number;
  visible_videos: number;
  hidden_videos: number;
  privacy_warnings: number;
  synced_modules: number;
  manual_modules: number;
  total_memberships: number;
}

interface SyncRunResult {
  ok: boolean;
  skipped: boolean;
  created: number;
  updated: number;
  hidden: number;
  total_fetched: number;
  error: string | null;
  warnings: string[];
  showcase_ok: boolean;
  showcase_skipped: boolean;
  showcase_error: string | null;
  modules_created: number;
  modules_updated: number;
  memberships_added: number;
  memberships_removed: number;
}

export default function ArchiveSyncPage() {
  const qc = useQueryClient();
  const status = useQuery<SyncStatus>({
    queryKey: ["archive-sync-status"],
    queryFn: () => api<SyncStatus>("/archive/sync/status"),
  });

  const run = useMutation({
    mutationFn: () => api<SyncRunResult>("/archive/sync", { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["archive-sync-status"] });
      qc.invalidateQueries({ queryKey: ["archive-videos"] });
    },
  });

  const s = status.data;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">🎬 Синхронизация архива</h1>
        <p className="text-sm text-muted-foreground">
          Подтягивает метаданные видео из Vimeo (только чтение). Видео остаются на Vimeo —
          мы храним лишь название, описание, обложку и id для портала участников.
        </p>
      </div>

      {/* GK-468: every Stat below falls back to "—" and the token warning is
          gated on `s`, so a failed status fetch looks like a healthy archive
          that simply has nothing in it. */}
      {status.isError && (
        <QueryError
          error={status.error}
          onRetry={() => status.refetch()}
          retrying={status.isFetching}
          title="Состояние синхронизации не загрузилось"
          description="Прочерки ниже означают «неизвестно», а не «ноль»."
        />
      )}

      {s && !s.token_configured && (
        <Card className="border-amber-300 bg-amber-50/60">
          <CardHeader>
            <CardTitle className="text-amber-900">VIMEO_API_TOKEN не задан</CardTitle>
            <CardDescription className="text-amber-900/80">
              Добавьте токен Vimeo в <code>.env</code> (<code>VIMEO_API_TOKEN=…</code>), чтобы
              синхронизация заработала. Сейчас она пропускается, существующие записи не меняются.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Всего видео" value={s?.total_videos ?? "—"} />
        <Stat label="Видимых" value={s?.visible_videos ?? "—"} />
        <Stat label="Скрытых" value={s?.hidden_videos ?? "—"} />
        <Stat
          label="Предупреждений приватности"
          value={s?.privacy_warnings ?? "—"}
          warn={(s?.privacy_warnings ?? 0) > 0}
        />
      </div>

      <div className="grid grid-cols-3 gap-4">
        <Stat label="Разделы из Vimeo" value={s?.synced_modules ?? "—"} />
        <Stat label="Ручные разделы" value={s?.manual_modules ?? "—"} />
        <Stat label="Привязок видео↔раздел" value={s?.total_memberships ?? "—"} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Запустить синхронизацию</CardTitle>
          <CardDescription>
            Последняя синхронизация:{" "}
            {s?.last_synced_at ? formatDateTime(s.last_synced_at) : "ещё не запускалась"}.
            Автоматически выполняется ежедневно в 04:00 UTC.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Button
            disabled={run.isPending || (s && !s.token_configured)}
            onClick={() => run.mutate()}
          >
            <RefreshCw className={"w-4 h-4 " + (run.isPending ? "animate-spin" : "")} />
            {run.isPending ? "Синхронизирую…" : "Синхронизировать сейчас"}
          </Button>

          {run.data && (
            <div className="rounded-lg border p-4 text-sm">
              {run.data.skipped ? (
                <p className="text-amber-700">
                  Пропущено: токен Vimeo не настроен ({run.data.error}).
                </p>
              ) : run.data.ok ? (
                <p className="flex items-center gap-2 text-emerald-700">
                  <CheckCircle2 className="h-4 w-4" />
                  Готово: получено {run.data.total_fetched}, добавлено {run.data.created},
                  обновлено {run.data.updated}, скрыто {run.data.hidden}.
                </p>
              ) : (
                <p className="flex items-center gap-2 text-destructive">
                  <AlertTriangle className="h-4 w-4" />
                  Ошибка синхронизации ({run.data.error}). Существующие видео не изменены.
                </p>
              )}
              {run.data.showcase_skipped ? (
                <p className="mt-1 text-amber-700">Шоукейсы пропущены (токен не настроен).</p>
              ) : run.data.showcase_ok ? (
                <p className="mt-1 flex items-center gap-2 text-emerald-700">
                  <CheckCircle2 className="h-4 w-4" />
                  Разделы: +{run.data.modules_created} новых, ~{run.data.modules_updated} обновлено;
                  привязки: +{run.data.memberships_added}, −{run.data.memberships_removed}.
                </p>
              ) : (
                <p className="mt-1 flex items-center gap-2 text-destructive">
                  <AlertTriangle className="h-4 w-4" />
                  Ошибка синхронизации шоукейсов ({run.data.showcase_error}). Группировка не изменена.
                </p>
              )}
              {run.data.warnings.length > 0 && (
                <ul className="mt-2 list-disc space-y-0.5 pl-5 text-xs text-amber-700">
                  {run.data.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Stat({ label, value, warn }: { label: string; value: number | string; warn?: boolean }) {
  return (
    <Card>
      <CardContent className="pt-5">
        <div className={"text-2xl font-semibold " + (warn ? "text-amber-600" : "")}>{value}</div>
        <div className="mt-1 text-xs text-muted-foreground">{label}</div>
      </CardContent>
    </Card>
  );
}
