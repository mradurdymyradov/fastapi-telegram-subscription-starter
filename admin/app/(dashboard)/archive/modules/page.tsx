"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { FolderPlus, Trash2 } from "lucide-react";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";

interface Module {
  id: number;
  code: string;
  title: string;
  description: string | null;
  sort_order: number;
  is_active: boolean;
  video_count: number;
  source: "vimeo" | "manual";
  vimeo_album_id: string | null;
}

export default function ArchiveModulesPage() {
  const qc = useQueryClient();
  const [code, setCode] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [sortOrder, setSortOrder] = useState("0");

  const list = useQuery<Module[]>({
    queryKey: ["archive-modules"],
    queryFn: () => api<Module[]>("/archive/modules"),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["archive-modules"] });

  const create = useMutation({
    mutationFn: () =>
      api("/archive/modules", {
        method: "POST",
        body: JSON.stringify({
          code,
          title,
          description: description || null,
          sort_order: Number(sortOrder) || 0,
        }),
      }),
    onSuccess: () => {
      invalidate();
      setCode("");
      setTitle("");
      setDescription("");
      setSortOrder("0");
    },
  });

  const patch = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      api(`/archive/modules/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
    onSuccess: invalidate,
  });

  const del = useMutation({
    mutationFn: (id: number) => api(`/archive/modules/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">🗂 Разделы архива</h1>
        <p className="text-sm text-muted-foreground">
          Группируйте видео по темам (например «Практики», «Лекции 2024»). Видео назначаются
          в разделы на странице «Видео архива».
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Разделы с меткой <span className="font-medium">Vimeo</span> создаются из шоукейсов
          Vimeo автоматически. Их можно переименовать, переупорядочить и скрыть — правки
          сохранятся при следующей синхронизации. Удалить такой раздел нельзя (синхронизация
          создаст его заново) — используйте «Скрыть».
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Новый раздел</CardTitle>
          <CardDescription>Код — латиница/цифры/дефис, используется как идентификатор.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
            <div className="space-y-1.5">
              <Label>Код</Label>
              <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="practices" />
            </div>
            <div className="space-y-1.5 sm:col-span-2">
              <Label>Название</Label>
              <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Практики" />
            </div>
            <div className="space-y-1.5">
              <Label>Порядок</Label>
              <Input
                type="number"
                value={sortOrder}
                onChange={(e) => setSortOrder(e.target.value)}
              />
            </div>
            <div className="space-y-1.5 sm:col-span-3">
              <Label>Описание (опционально)</Label>
              <Input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Короткое описание раздела"
              />
            </div>
            <div className="flex items-end">
              <Button
                className="w-full"
                disabled={!code || !title || create.isPending}
                onClick={() => create.mutate()}
              >
                <FolderPlus className="h-4 w-4" /> Создать
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Разделы</CardTitle>
        </CardHeader>
        <CardContent>
          {list.isError && (
            <QueryError
              error={list.error}
              onRetry={() => list.refetch()}
              retrying={list.isFetching}
              title="Разделы не загрузились"
              className="mb-4"
            />
          )}
          <div className="divide-y">
            {list.data?.map((m) => (
              <div key={m.id} className="flex items-center gap-3 py-3">
                <Badge variant={m.is_active ? "success" : "muted"}>{m.code}</Badge>
                <Badge variant={m.source === "vimeo" ? "default" : "muted"}>
                  {m.source === "vimeo" ? "Vimeo" : "Ручной"}
                </Badge>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{m.title}</div>
                  <div className="truncate text-xs text-muted-foreground">
                    {m.video_count} видео{m.description ? ` · ${m.description}` : ""}
                  </div>
                </div>
                <Input
                  type="number"
                  defaultValue={m.sort_order}
                  className="w-20"
                  title="Порядок"
                  onBlur={(e) => {
                    const v = Number(e.target.value) || 0;
                    if (v !== m.sort_order) patch.mutate({ id: m.id, body: { sort_order: v } });
                  }}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => patch.mutate({ id: m.id, body: { is_active: !m.is_active } })}
                >
                  {m.is_active ? "Скрыть" : "Показать"}
                </Button>
                {m.source === "vimeo" ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled
                    title="Раздел из Vimeo — удалить нельзя (синхронизация создаст заново). Скройте его."
                  >
                    <Trash2 className="h-4 w-4 text-muted-foreground" />
                  </Button>
                ) : (
                  <Button
                    variant="ghost"
                    size="sm"
                    title="Удалить (видео не удаляются, открепляются)"
                    onClick={() => {
                      if (confirm(`Удалить раздел «${m.title}»? Видео останутся, но открепятся.`))
                        del.mutate(m.id);
                    }}
                  >
                    <Trash2 className="h-4 w-4 text-destructive" />
                  </Button>
                )}
              </div>
            ))}
            {list.data?.length === 0 && (
              <div className="py-6 text-center text-sm text-muted-foreground">
                Разделов пока нет.
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
