"use client";

import { useMemo, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/utils";

interface SupportMsg {
  id: number;
  role: string;
  content: string;
  delivery_status: string | null;
  created_at: string;
}

const SUPPORT_PAGE = 50;

interface User {
  id: number;
  tg_id: number;
  username: string | null;
  first_name: string | null;
  joined_at: string;
  referral_code: string;
  bonus_days: number;
  is_banned: boolean;
  subscription_status: string;
  subscription_expires_at: string | null;
}

export default function UsersPage() {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState<User | null>(null);
  const [extendDays, setExtendDays] = useState(7);
  const qc = useQueryClient();

  const users = useQuery<{ items: User[]; total: number }>({
    queryKey: ["users", q, status],
    queryFn: () => {
      // GK-381 (B10): "Все статусы" is the empty value. The API validates
      // sub_status against ^(active|expired|none)$, so sending sub_status=""
      // 422'd the request and made the default "all" view render as an empty
      // table. Omit the param entirely when no status is selected; only send it
      // for the active/expired/none filters.
      const params = new URLSearchParams({ limit: "50" });
      if (q) params.set("q", q);
      if (status) params.set("sub_status", status);
      return api(`/users?${params.toString()}`);
    },
  });

  const action = useMutation({
    mutationFn: (body: { id: number; action: string; days?: number }) =>
      api(`/users/${body.id}/actions`, { method: "POST", body: JSON.stringify({ action: body.action, days: body.days }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setSelected(null);
    },
  });

  // GK-378: a user's complete support transcript, reachable from their card.
  // Lazy — only fetched while the dialog for this user is open.
  const supportHistory = useInfiniteQuery<SupportMsg[]>({
    queryKey: ["user-support", selected?.id],
    queryFn: ({ pageParam }) =>
      api<SupportMsg[]>(
        `/support/messages?user_id=${selected!.id}&limit=${SUPPORT_PAGE}&offset=${Number(pageParam)}`,
      ),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) =>
      lastPage.length === SUPPORT_PAGE
        ? pages.reduce((total, page) => total + page.length, 0)
        : undefined,
    enabled: !!selected,
  });
  const supportChronological = useMemo(() => {
    const seen = new Set<number>();
    const rows = (supportHistory.data?.pages ?? []).flat().filter((message) => {
      if (seen.has(message.id)) return false;
      seen.add(message.id);
      return true;
    });
    return rows.reverse();
  }, [supportHistory.data]);

  const statusVariant = (s: string) =>
    s === "active" ? "success" : s === "expired" ? "warning" : "muted";
  const statusLabel = (s: string) =>
    s === "active" ? "Активен" : s === "expired" ? "Истёк" : "Без подписки";

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Пользователи</h1>
        <p className="text-sm text-muted-foreground">
          {users.isError ? "количество неизвестно" : `${users.data?.total ?? 0} всего`}
        </p>
      </div>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap gap-3">
            <Input
              placeholder="Поиск по username, имени, tg_id…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              className="max-w-xs"
            />
            <Select value={status} onChange={(e) => setStatus(e.target.value)} className="max-w-[180px]">
              <option value="">Все статусы</option>
              <option value="active">С активной</option>
              <option value="expired">Просроченная</option>
              <option value="none">Без подписки</option>
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          {users.isError && (
            <QueryError
              error={users.error}
              onRetry={() => users.refetch()}
              retrying={users.isFetching}
              title="Список пользователей не загрузился"
              description="Пустая таблица ниже — это отсутствие ответа, а не отсутствие пользователей."
              className="mb-4"
            />
          )}
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Пользователь</TableHead>
                <TableHead>TG ID</TableHead>
                <TableHead>Подписка</TableHead>
                <TableHead>Бонус-дни</TableHead>
                <TableHead>Реф-код</TableHead>
                <TableHead>В сообществе с</TableHead>
                <TableHead></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.data?.items.map((u) => (
                <TableRow key={u.id}>
                  <TableCell>
                    <div className="font-medium">
                      {u.username ? `@${u.username}` : u.first_name || `User #${u.id}`}
                    </div>
                    {u.is_banned && <Badge variant="destructive" className="mt-1">Бан</Badge>}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{u.tg_id}</TableCell>
                  <TableCell>
                    <Badge variant={statusVariant(u.subscription_status)}>
                      {statusLabel(u.subscription_status)}
                    </Badge>
                    {u.subscription_expires_at && (
                      <div className="text-xs text-muted-foreground mt-1">до {formatDate(u.subscription_expires_at)}</div>
                    )}
                  </TableCell>
                  <TableCell>{u.bonus_days}</TableCell>
                  <TableCell className="font-mono text-xs">{u.referral_code}</TableCell>
                  <TableCell className="text-muted-foreground text-sm">{formatDate(u.joined_at)}</TableCell>
                  <TableCell>
                    <Button variant="ghost" size="sm" onClick={() => setSelected(u)}>
                      Действия
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              {users.data?.items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground py-8">
                    Ничего не найдено
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={!!selected} onOpenChange={(o) => !o && setSelected(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {selected?.username ? `@${selected.username}` : selected?.first_name || `User #${selected?.id}`}
            </DialogTitle>
            <DialogDescription>Действия с пользователем</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Label className="flex-1">Продлить на N дней</Label>
              <Input
                type="number"
                value={extendDays}
                onChange={(e) => setExtendDays(Number(e.target.value))}
                className="w-24"
              />
              <Button
                onClick={() => selected && action.mutate({ id: selected.id, action: "extend_days", days: extendDays })}
              >
                Продлить
              </Button>
            </div>
            {selected?.is_banned ? (
              <Button variant="outline" className="w-full" onClick={() => action.mutate({ id: selected.id, action: "unban" })}>
                Разбанить
              </Button>
            ) : (
              <Button variant="destructive" className="w-full" onClick={() => selected && action.mutate({ id: selected.id, action: "ban" })}>
                Забанить
              </Button>
            )}

            <div className="border-t pt-3">
              <div className="mb-2 text-sm font-medium">История поддержки</div>
              {supportHistory.isError ? (
                <QueryError
                  error={supportHistory.error}
                  onRetry={() => supportHistory.refetch()}
                  retrying={supportHistory.isFetching}
                  title="История поддержки не загрузилась"
                />
              ) : supportHistory.isLoading ? (
                <p className="text-xs text-muted-foreground">Загрузка…</p>
              ) : supportChronological.length === 0 ? (
                <p className="text-xs text-muted-foreground">Обращений в поддержку не было.</p>
              ) : (
                <div className="space-y-2">
                  {supportHistory.hasNextPage && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="w-full"
                      disabled={supportHistory.isFetchingNextPage}
                      onClick={() => supportHistory.fetchNextPage()}
                    >
                      {supportHistory.isFetchingNextPage ? "Загрузка…" : "Загрузить более ранние"}
                    </Button>
                  )}
                  <div className="max-h-56 space-y-2 overflow-y-auto pr-1">
                    {supportChronological.map((m) => {
                      const mine = m.role === "assistant";
                      return (
                        <div key={m.id} className={`flex flex-col ${mine ? "items-end" : "items-start"}`}>
                          <div
                            className={`max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-line ${
                              mine ? "bg-primary text-primary-foreground" : "bg-muted text-foreground"
                            }`}
                          >
                            {m.content}
                          </div>
                          <div className="mt-0.5 flex items-center gap-2 text-[11px] text-muted-foreground">
                            <span>{mine ? "Куратор" : "Пользователь"} · {formatDateTime(m.created_at)}</span>
                            {m.role === "assistant" && m.delivery_status === "failed" && (
                              <Badge variant="warning">не доставлено</Badge>
                            )}
                            {m.role === "user" && m.delivery_status === "failed" && (
                              <Badge variant="warning">маршрутизация не удалась</Badge>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setSelected(null)}>
              Закрыть
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
