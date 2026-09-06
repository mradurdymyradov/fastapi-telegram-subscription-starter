"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Plug, Trash2, Zap } from "lucide-react";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/utils";

interface Webhook {
  id: number;
  provider: string;
  name: string;
  url: string;
  secret: string | null;
  events: string[];
  enabled: boolean;
  created_at: string;
}

interface Log {
  id: number;
  event: string;
  response_status: number | null;
  created_at: string;
}

interface LaunchFlags {
  enable_zelle: boolean;
  enable_webhook_integrations: boolean;
  enable_ai_support: boolean;
}

const EVENTS = ["payment.succeeded", "subscription.activated", "subscription.expired", "user.joined"];

export default function IntegrationsPage() {
  const qc = useQueryClient();
  const [provider, setProvider] = useState("make");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");

  const flags = useQuery<LaunchFlags>({
    queryKey: ["launch-flags"],
    queryFn: () => api<LaunchFlags>("/config/launch-flags"),
    staleTime: 60_000,
  });
  const integrationsEnabled = flags.data?.enable_webhook_integrations ?? false;

  const list = useQuery<Webhook[]>({ queryKey: ["webhooks"], queryFn: () => api<Webhook[]>("/integrations/webhooks") });
  const logs = useQuery<Log[]>({
    queryKey: ["webhook-logs"],
    queryFn: () => api<Log[]>("/integrations/logs?limit=20"),
    refetchInterval: 15_000,
  });

  const create = useMutation({
    mutationFn: () =>
      api("/integrations/webhooks", {
        method: "POST",
        body: JSON.stringify({ provider, name, url, secret: secret || null, events: EVENTS, enabled: true }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["webhooks"] });
      setName("");
      setUrl("");
      setSecret("");
    },
  });

  const del = useMutation({
    mutationFn: (id: number) => api(`/integrations/webhooks/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });

  const test = useMutation({
    mutationFn: (id: number) => api(`/integrations/webhooks/${id}/test`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["webhook-logs"] }),
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">🔌 Интеграции</h1>
        <p className="text-sm text-muted-foreground">
          Подключите AmoCRM, Make или Zapier — события будут уходить туда автоматически.
        </p>
      </div>

      {/* GK-468: `integrationsEnabled` falls back to false, so a failed flag
          fetch renders the "Скрыто для запуска" card as if that were confirmed.
          Say which one it is. */}
      {flags.isError && (
        <QueryError
          error={flags.error}
          onRetry={() => flags.refetch()}
          retrying={flags.isFetching}
          title="Состояние launch-флагов неизвестно"
          description="Страница ведёт себя так, будто интеграции выключены — это безопасное предположение, а не прочитанное значение."
        />
      )}

      {!integrationsEnabled && (
        <Card className="border-amber-300 bg-amber-50/60">
          <CardHeader>
            <CardTitle className="text-amber-900">Скрыто для запуска</CardTitle>
            <CardDescription className="text-amber-900/80">
              AmoCRM, Make и Zapier-вебхуки выключены для запуска (решение Гранта от 21.05.2026).
              Существующие записи остаются доступны для аудита и удаления; новые подключения и тестовые отправки
              временно недоступны. Включается через <code>ENABLE_WEBHOOK_INTEGRATIONS=true</code> в .env.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {integrationsEnabled && (
        <Card>
          <CardHeader>
            <CardTitle>Добавить webhook</CardTitle>
            <CardDescription>
              Для Make/Zapier — создайте Webhook trigger и вставьте URL. Для AmoCRM — используйте «Входящие webhooks» в разделе «Интеграции».
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
              <div className="space-y-1.5">
                <Label>Провайдер</Label>
                <Select value={provider} onChange={(e) => setProvider(e.target.value)}>
                  <option value="make">Make</option>
                  <option value="zapier">Zapier</option>
                  <option value="amocrm">AmoCRM</option>
                  <option value="custom">Custom</option>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Название</Label>
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Например: AmoCRM лиды" />
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <Label>URL</Label>
                <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://hook.make.com/…" />
              </div>
              <div className="space-y-1.5 sm:col-span-3">
                <Label>Secret (опционально)</Label>
                <Input value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="HMAC ключ" />
              </div>
              <div className="space-y-1.5 flex items-end">
                <Button className="w-full" disabled={!url || !name || create.isPending} onClick={() => create.mutate()}>
                  <Plug className="w-4 h-4" /> Подключить
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Подключённые webhooks</CardTitle>
          </CardHeader>
          <CardContent>
            {list.isError && (
              <QueryError
                error={list.error}
                onRetry={() => list.refetch()}
                retrying={list.isFetching}
                title="Подключения не загрузились"
                className="mb-4"
              />
            )}
            <div className="divide-y">
              {list.data?.map((w) => (
                <div key={w.id} className="py-3 flex items-center gap-3">
                  <Badge variant={w.enabled ? "success" : "muted"}>{w.provider}</Badge>
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-sm truncate">{w.name}</div>
                    <div className="text-xs text-muted-foreground truncate">{w.url}</div>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => test.mutate(w.id)}
                    title={integrationsEnabled ? "Test ping" : "Test ping disabled — ENABLE_WEBHOOK_INTEGRATIONS=false"}
                    disabled={!integrationsEnabled}
                  >
                    <Zap className="w-4 h-4" />
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => del.mutate(w.id)} title="Delete">
                    <Trash2 className="w-4 h-4 text-destructive" />
                  </Button>
                </div>
              ))}
              {list.data?.length === 0 && (
                <div className="text-center text-muted-foreground py-6 text-sm">Пока без подключений.</div>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Лог отправок</CardTitle>
            <CardDescription>20 последних попыток</CardDescription>
          </CardHeader>
          <CardContent>
            {logs.isError && (
              <QueryError
                error={logs.error}
                onRetry={() => logs.refetch()}
                retrying={logs.isFetching}
                title="Лог отправок не загрузился"
                className="mb-4"
              />
            )}
            <div className="divide-y">
              {logs.data?.map((l) => (
                <div key={l.id} className="py-2.5 flex items-center gap-3 text-sm">
                  <Badge
                    variant={
                      l.response_status === null
                        ? "muted"
                        : l.response_status >= 200 && l.response_status < 300
                        ? "success"
                        : "destructive"
                    }
                  >
                    {l.response_status ?? "—"}
                  </Badge>
                  <div className="flex-1 font-mono text-xs">{l.event}</div>
                  <div className="text-xs text-muted-foreground">{formatDateTime(l.created_at)}</div>
                </div>
              ))}
              {logs.data?.length === 0 && (
                <div className="text-center text-muted-foreground py-6 text-sm">Логов пока нет.</div>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
