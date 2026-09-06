"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { AlertTriangle, ExternalLink, X } from "lucide-react";
import { QueryErrorGroup } from "@/components/query-error";
import { api } from "@/lib/api";

interface Module {
  id: number;
  title: string;
  code: string;
}

interface Membership {
  module_id: number;
  source: "vimeo" | "admin";
}

interface ArchiveVideo {
  id: number;
  vimeo_id: number;
  title: string;
  description: string | null;
  thumbnail_url: string | null;
  module_id: number | null;
  memberships: Membership[];
  sort_order: number;
  visibility: string;
  vimeo_privacy: string | null;
  privacy_warning: boolean;
  synced_at: string | null;
}

const VISIBILITY_LABEL: Record<string, string> = {
  visible: "Видно",
  hidden: "Скрыто",
  draft: "Черновик",
};

export default function ArchiveVideosPage() {
  const qc = useQueryClient();
  const videos = useQuery<ArchiveVideo[]>({
    queryKey: ["archive-videos"],
    queryFn: () => api<ArchiveVideo[]>("/archive/videos"),
  });
  const modules = useQuery<Module[]>({
    queryKey: ["archive-modules"],
    queryFn: () => api<Module[]>("/archive/modules"),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["archive-videos"] });

  const patch = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      api(`/archive/videos/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
    onSuccess: invalidate,
  });

  const addMembership = useMutation({
    mutationFn: ({ videoId, moduleId }: { videoId: number; moduleId: number }) =>
      api(`/archive/videos/${videoId}/modules/${moduleId}`, { method: "POST" }),
    onSuccess: invalidate,
  });

  const removeMembership = useMutation({
    mutationFn: ({ videoId, moduleId }: { videoId: number; moduleId: number }) =>
      api(`/archive/videos/${videoId}/modules/${moduleId}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });

  const moduleTitle = (id: number) =>
    modules.data?.find((m) => m.id === id)?.title ?? `#${id}`;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">🎞 Видео архива</h1>
        <p className="text-sm text-muted-foreground">
          Распределяйте видео по разделам, управляйте видимостью и порядком. Метаданные и
          группировка по шоукейсам приходят из Vimeo при синхронизации; сами файлы остаются на
          Vimeo. Одно видео может быть в нескольких разделах.
        </p>
      </div>

      <Card className="border-sky-200 bg-sky-50/50">
        <CardHeader>
          <CardTitle className="text-sky-900">О разделах и приватности</CardTitle>
          <CardDescription className="text-sky-900/80">
            Метка <span className="font-medium">Vimeo</span> на разделе означает, что привязка
            пришла из шоукейса Vimeo; метка <span className="font-medium">вручную</span> — что её
            добавили здесь. Ручные привязки и удаления сохраняются при следующей синхронизации.
            Жёлтый значок «приватность» означает, что у видео на Vimeo не выставлен embed-only
            режим — это управляется в кабинете Vimeo, не здесь (это не DRM).
          </CardDescription>
        </CardHeader>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>
            Видео {videos.isError ? "" : `(${videos.data?.length ?? 0})`}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {/* GK-468: `modules` feeds the "add to section" dropdown on every row,
              so its failure silently narrows the options rather than saying so. */}
          <QueryErrorGroup
            queries={[videos, modules]}
            title="Архив не загрузился"
            description="Пустой список — это отсутствие ответа. Разделы в выпадающих списках тоже могут быть неполными."
            className="mb-4"
          />
          <div className="divide-y">
            {videos.data?.map((v) => {
              const memberModuleIds = new Set(v.memberships.map((m) => m.module_id));
              const available = modules.data?.filter((m) => !memberModuleIds.has(m.id)) ?? [];
              return (
                <div key={v.id} className="flex flex-col gap-3 py-3 lg:flex-row lg:items-start">
                  <div className="flex min-w-0 flex-1 items-center gap-3">
                    <div className="h-12 w-20 shrink-0 overflow-hidden rounded bg-muted">
                      {v.thumbnail_url && (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={v.thumbnail_url} alt="" className="h-full w-full object-cover" />
                      )}
                    </div>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">{v.title}</span>
                        {v.privacy_warning && (
                          <Badge variant="warning" title={`Vimeo privacy: ${v.vimeo_privacy}`}>
                            <AlertTriangle className="mr-1 h-3 w-3" /> приватность
                          </Badge>
                        )}
                      </div>
                      <a
                        href={`https://vimeo.com/${v.vimeo_id}`}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                      >
                        vimeo/{v.vimeo_id} <ExternalLink className="h-3 w-3" />
                      </a>
                      {/* Membership chips (M2M) */}
                      <div className="mt-2 flex flex-wrap items-center gap-1.5">
                        {v.memberships.length === 0 && (
                          <span className="text-xs text-muted-foreground">— без раздела —</span>
                        )}
                        {v.memberships.map((m) => (
                          <span
                            key={m.module_id}
                            className="inline-flex items-center gap-1 rounded-md border bg-background px-2 py-0.5 text-xs"
                          >
                            {moduleTitle(m.module_id)}
                            <Badge variant={m.source === "vimeo" ? "default" : "muted"} className="px-1 py-0">
                              {m.source === "vimeo" ? "Vimeo" : "вручную"}
                            </Badge>
                            <button
                              type="button"
                              title="Убрать из раздела"
                              className="text-muted-foreground hover:text-destructive"
                              onClick={() =>
                                removeMembership.mutate({ videoId: v.id, moduleId: m.module_id })
                              }
                            >
                              <X className="h-3 w-3" />
                            </button>
                          </span>
                        ))}
                      </div>
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-2">
                    <Select
                      value=""
                      className="w-44"
                      disabled={available.length === 0}
                      onChange={(e) => {
                        if (e.target.value)
                          addMembership.mutate({ videoId: v.id, moduleId: Number(e.target.value) });
                        e.target.value = "";
                      }}
                    >
                      <option value="">+ в раздел…</option>
                      {available.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.title}
                        </option>
                      ))}
                    </Select>

                    <Select
                      value={v.visibility}
                      className="w-32"
                      onChange={(e) => patch.mutate({ id: v.id, body: { visibility: e.target.value } })}
                    >
                      {Object.entries(VISIBILITY_LABEL).map(([val, label]) => (
                        <option key={val} value={val}>
                          {label}
                        </option>
                      ))}
                    </Select>

                    <Input
                      type="number"
                      defaultValue={v.sort_order}
                      className="w-20"
                      title="Порядок"
                      onBlur={(e) => {
                        const val = Number(e.target.value) || 0;
                        if (val !== v.sort_order) patch.mutate({ id: v.id, body: { sort_order: val } });
                      }}
                    />
                  </div>
                </div>
              );
            })}
            {videos.data?.length === 0 && (
              <div className="py-8 text-center text-sm text-muted-foreground">
                Видео пока нет. Запустите синхронизацию на странице «Синхронизация архива».
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
