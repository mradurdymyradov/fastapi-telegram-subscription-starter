"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Select } from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/utils";

interface Broadcast {
  id: number;
  title: string;
  message: string;
  segment: string;
  status: string;
  sent_count: number;
  failed_count: number;
  created_at: string;
  sent_at: string | null;
}

const statusVariant: Record<string, "success" | "warning" | "muted" | "destructive"> = {
  sent: "success",
  sending: "warning",
  draft: "muted",
  failed: "destructive",
};

export default function BroadcastsPage() {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [message, setMessage] = useState("");
  const [segment, setSegment] = useState("all");

  const list = useQuery<Broadcast[]>({
    queryKey: ["bcasts"],
    queryFn: () => api<Broadcast[]>("/broadcasts"),
    refetchInterval: 10_000,
  });

  const create = useMutation({
    mutationFn: () =>
      api("/broadcasts", { method: "POST", body: JSON.stringify({ title, message, segment }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["bcasts"] });
      setTitle("");
      setMessage("");
    },
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">📣 Рассылки</h1>
        <p className="text-sm text-muted-foreground">Сообщения отправятся через бота со скоростью ~20/сек</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Новая рассылка</CardTitle>
          <CardDescription>HTML-разметка поддерживается (&lt;b&gt;, &lt;i&gt;, &lt;a&gt;)</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div className="space-y-1.5 sm:col-span-2">
              <Label>Заголовок (для админа)</Label>
              <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Напр. Анонс новой практики" />
            </div>
            <div className="space-y-1.5">
              <Label>Сегмент</Label>
              <Select value={segment} onChange={(e) => setSegment(e.target.value)}>
                <option value="all">Все юзеры</option>
                <option value="active">С активной подпиской</option>
                <option value="expired">Истёкшие подписки</option>
              </Select>
            </div>
          </div>
          <div className="space-y-1.5">
            <Label>Сообщение</Label>
            <Textarea rows={6} value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Текст сообщения…" />
          </div>
          <div className="flex justify-end">
            <Button disabled={!title.trim() || !message.trim() || create.isPending} onClick={() => create.mutate()}>
              {create.isPending ? "Отправка…" : "Запустить рассылку"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>История</CardTitle>
        </CardHeader>
        <CardContent>
          {list.isError && (
            <QueryError
              error={list.error}
              onRetry={() => list.refetch()}
              retrying={list.isFetching}
              title="История рассылок не загрузилась"
              className="mb-4"
            />
          )}
          <div className="divide-y">
            {list.data?.map((b) => (
              <div key={b.id} className="py-3 flex items-center gap-3">
                <div className="flex-1 min-w-0">
                  <div className="font-medium truncate">{b.title}</div>
                  <div className="text-xs text-muted-foreground line-clamp-1">{b.message}</div>
                  <div className="text-xs text-muted-foreground mt-1">
                    Сегмент: {b.segment} · Отправлено {b.sent_count}, ошибок {b.failed_count} ·{" "}
                    {b.sent_at ? formatDateTime(b.sent_at) : formatDateTime(b.created_at)}
                  </div>
                </div>
                <Badge variant={statusVariant[b.status] ?? "muted"}>{b.status}</Badge>
              </div>
            ))}
            {list.data?.length === 0 && (
              <div className="text-center text-muted-foreground py-6">Пока без рассылок.</div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
