"use client";

import { useEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDateTime, relativeDate } from "@/lib/utils";

interface Msg {
  id: number;
  user_id: number;
  username: string | null;
  first_name: string | null;
  role: string;
  content: string;
  delivery_status: string | null;
  created_at: string;
}

interface Conversation {
  user_id: number;
  username: string | null;
  first_name: string | null;
  last_message: string;
  last_role: string;
  last_delivery_status: string | null;
  last_message_at: string;
  message_count: number;
  unanswered: boolean;
}

interface ConversationsPage {
  items: Conversation[];
  total: number;
}

const CONV_PAGE = 50;
const THREAD_PAGE = 50;

function who(u: { username: string | null; first_name: string | null; user_id: number }) {
  return u.username ? `@${u.username}` : u.first_name || `User #${u.user_id}`;
}

// Delivery state is best-effort, so we only surface it when it carries signal:
// a failed route/delivery (needs attention) or a confirmed delivery.
function DeliveryBadge({ role, status }: { role: string; status: string | null }) {
  if (!status) return null;
  if (role === "assistant") {
    if (status === "delivered") return <Badge variant="success">доставлено</Badge>;
    if (status === "failed") return <Badge variant="warning">не доставлено</Badge>;
    return null;
  }
  // user ticket → curator routing outcome
  if (status === "routed") return <Badge variant="muted">→ кураторам</Badge>;
  if (status === "failed") return <Badge variant="warning">маршрутизация не удалась</Badge>;
  return null; // "skipped" (no curator chat configured) is the silent default
}

export default function SupportPage() {
  const qc = useQueryClient();
  const [selectedUserId, setSelectedUserId] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const conversations = useInfiniteQuery<ConversationsPage>({
    queryKey: ["support-conversations"],
    queryFn: ({ pageParam }) =>
      api<ConversationsPage>(
        `/support/conversations?limit=${CONV_PAGE}&offset=${Number(pageParam)}`,
      ),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((total, page) => total + page.items.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
    refetchInterval: 15_000,
  });

  const thread = useInfiniteQuery<Msg[]>({
    queryKey: ["support-thread", selectedUserId],
    queryFn: ({ pageParam }) =>
      api<Msg[]>(
        `/support/messages?user_id=${selectedUserId}&limit=${THREAD_PAGE}&offset=${Number(pageParam)}`,
      ),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) =>
      lastPage.length === THREAD_PAGE
        ? pages.reduce((total, page) => total + page.length, 0)
        : undefined,
    enabled: selectedUserId !== null,
    refetchInterval: 15_000,
  });

  // Pages and rows arrive newest-first. De-duplicate IDs in case a new message
  // shifts offset boundaries during a background refresh, then render oldest-first.
  const chronological = useMemo(
    () => {
      const seen = new Set<number>();
      const rows = (thread.data?.pages ?? []).flat().filter((message) => {
        if (seen.has(message.id)) return false;
        seen.add(message.id);
        return true;
      });
      return rows.reverse();
    },
    [thread.data],
  );

  const convItems = useMemo(
    () => {
      const seen = new Set<number>();
      return (conversations.data?.pages ?? []).flatMap((page) => page.items).filter((item) => {
        if (seen.has(item.user_id)) return false;
        seen.add(item.user_id);
        return true;
      });
    },
    [conversations.data],
  );
  const selectedConv = convItems.find((c) => c.user_id === selectedUserId);

  // Clear per-conversation compose state whenever another thread is opened.
  useEffect(() => {
    setDraft("");
  }, [selectedUserId]);

  const reply = useMutation({
    mutationFn: (vars: { user_id: number; content: string }) =>
      api<{ delivered: boolean }>("/support/reply", {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    onSuccess: (data) => {
      setNotice(
        data.delivered
          ? "Ответ отправлен пользователю."
          : "Ответ сохранён, но доставить в Telegram не удалось (возможно, пользователь не запускал бота).",
      );
      setDraft("");
      qc.invalidateQueries({ queryKey: ["support-thread"] });
      qc.invalidateQueries({ queryKey: ["support-conversations"] });
    },
    onError: () => setNotice("Не удалось отправить ответ. Попробуйте ещё раз."),
  });

  const conversationTotal = conversations.data?.pages[0]?.total ?? 0;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">💬 Поддержка</h1>
        <p className="text-sm text-muted-foreground">
          Диалоги с пользователями — отвечайте прямо здесь
        </p>
      </div>

      {notice && (
        <div className="rounded-lg border bg-accent px-4 py-3 text-sm text-accent-foreground">
          {notice}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[340px_1fr]">
        {/* Conversation list — hidden on mobile once a thread is open */}
        <Card className={selectedUserId !== null ? "hidden lg:block" : ""}>
          <CardHeader>
            <CardTitle>Диалоги</CardTitle>
            <CardDescription>
              {conversations.isError ? "количество неизвестно" : `${conversationTotal} всего`} · новые сверху
            </CardDescription>
          </CardHeader>
          <CardContent className="p-0">
            {conversations.isError && (
              <QueryError
                error={conversations.error}
                onRetry={() => conversations.refetch()}
                retrying={conversations.isFetching}
                title="Диалоги не загрузились"
                description="Пустой список — это отсутствие ответа, а не отсутствие обращений."
                className="m-4"
              />
            )}
            <div className="divide-y">
              {convItems.map((c) => (
                <button
                  key={c.user_id}
                  onClick={() => {
                    setSelectedUserId(c.user_id);
                    setNotice(null);
                  }}
                  className={`flex w-full flex-col gap-1 px-4 py-3 text-left transition-colors hover:bg-accent ${
                    c.user_id === selectedUserId ? "bg-accent" : ""
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">{who(c)}</span>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {relativeDate(c.last_message_at)}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {c.unanswered && <Badge variant="info">новый</Badge>}
                    <span className="truncate text-xs text-muted-foreground">
                      {c.last_role === "assistant" ? "Вы: " : ""}
                      {c.last_message}
                    </span>
                  </div>
                </button>
              ))}
              {conversations.isSuccess && convItems.length === 0 && (
                <div className="py-8 text-center text-sm text-muted-foreground">
                  Сообщений пока нет.
                </div>
              )}
            </div>
            {conversations.hasNextPage && (
              <div className="border-t p-3">
                <Button
                  variant="outline"
                  size="sm"
                  className="w-full"
                  disabled={conversations.isFetchingNextPage}
                  onClick={() => conversations.fetchNextPage()}
                >
                  {conversations.isFetchingNextPage ? "Загрузка…" : "Загрузить ещё"}
                </Button>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Thread / reply pane */}
        <Card className={selectedUserId === null ? "hidden lg:block" : ""}>
          {selectedUserId === null ? (
            <CardContent className="flex h-full min-h-[300px] items-center justify-center text-sm text-muted-foreground">
              Выберите диалог слева, чтобы прочитать переписку и ответить.
            </CardContent>
          ) : (
            <>
              <CardHeader>
                <div className="flex items-center gap-3">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="lg:hidden"
                    onClick={() => setSelectedUserId(null)}
                  >
                    ← Назад
                  </Button>
                  <div>
                    <CardTitle>{selectedConv ? who(selectedConv) : `User #${selectedUserId}`}</CardTitle>
                    <CardDescription>
                      Ответ уходит пользователю в Telegram от имени бота.
                    </CardDescription>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                {thread.isError && (
                  <QueryError
                    error={thread.error}
                    onRetry={() => thread.refetch()}
                    retrying={thread.isFetching}
                    title="Переписка не загрузилась"
                    description="Не отвечайте, пока не увидите историю — контекст обращения сейчас неизвестен."
                  />
                )}
                {thread.hasNextPage && (
                  <div className="text-center">
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={thread.isFetchingNextPage}
                      onClick={() => thread.fetchNextPage()}
                    >
                      {thread.isFetchingNextPage ? "Загрузка…" : "Загрузить более ранние"}
                    </Button>
                  </div>
                )}
                <div className="max-h-[55vh] space-y-3 overflow-y-auto pr-1">
                  {chronological.map((m) => {
                    const mine = m.role === "assistant";
                    return (
                      <div
                        key={m.id}
                        className={`flex flex-col ${mine ? "items-end" : "items-start"}`}
                      >
                        <div
                          className={`max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-line ${
                            mine
                              ? "bg-primary text-primary-foreground"
                              : "bg-muted text-foreground"
                          }`}
                        >
                          {m.content}
                        </div>
                        <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                          <span>{formatDateTime(m.created_at)}</span>
                          <DeliveryBadge role={m.role} status={m.delivery_status} />
                        </div>
                      </div>
                    );
                  })}
                  {thread.isSuccess && chronological.length === 0 && (
                    <div className="py-6 text-center text-sm text-muted-foreground">
                      Переписка пуста.
                    </div>
                  )}
                </div>

                <div className="space-y-2 border-t pt-3">
                  <Textarea
                    rows={3}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    placeholder="Ваш ответ…"
                  />
                  <div className="flex justify-end">
                    <Button
                      size="sm"
                      disabled={!draft.trim() || reply.isPending}
                      onClick={() =>
                        selectedUserId &&
                        reply.mutate({ user_id: selectedUserId, content: draft.trim() })
                      }
                    >
                      {reply.isPending ? "Отправка…" : "Отправить"}
                    </Button>
                  </div>
                </div>
              </CardContent>
            </>
          )}
        </Card>
      </div>
    </div>
  );
}
