"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDate } from "@/lib/utils";

interface Sub {
  id: number;
  user_id: number;
  username: string | null;
  plan_name: string;
  status: string;
  source: string;
  started_at: string;
  expires_at: string;
  // GK-483: the client's own team. The row is `active` like any other because
  // access behaves identically; this is what says it is not a paying member,
  // and it is why the row is missing from «Активных подписок» on the dashboard.
  is_comp: boolean;
  cancel_state: string | null;
  cancel_requested_at: string | null;
  cancel_confirmed_at: string | null;
  cancel_resolved_at: string | null;
  // GK-432: set means the bot stopped trying to remove this member from
  // Telegram. They are still in the channel and only a human can change that.
  access_revoke_abandoned_at: string | null;
  access_revoke_error: string | null;
}

interface Cancellation {
  subscription_id: number;
  user_id: number;
  tg_id: number;
  username: string | null;
  plan_name: string;
  provider: string | null;
  provider_subscription_id: string | null;
  buyer_email: string | null;
  payment_id: number | null;
  expires_at: string;
  cancel_state: string | null;
  cancel_requested_at: string | null;
  cancel_failure_reason: string | null;
  cancel_resolved_at: string | null;
}

const statusVariant: Record<string, "success" | "warning" | "muted" | "info"> = {
  active: "success",
  expired: "warning",
  cancelled: "muted",
  gifted: "info",
};

// GK-377: "the member asked" and "the provider stopped charging" are different
// facts. Showing them as one badge is what let cancellation requests sit
// unnoticed while cards kept being charged.
const cancelLabel: Record<string, { text: string; variant: "success" | "warning" | "muted" }> = {
  provider_confirmed: { text: "Отменено у провайдера", variant: "success" },
  manual_required: { text: "Нужна ручная отмена", variant: "warning" },
  requested: { text: "Запрошена", variant: "muted" },
};

function CancelBadge({ state }: { state: string | null }) {
  if (!state) return <span className="text-muted-foreground text-sm">—</span>;
  const meta = cancelLabel[state] ?? { text: state, variant: "muted" as const };
  return <Badge variant={meta.variant}>{meta.text}</Badge>;
}

// GK-432: the hourly job used to retry an impossible removal forever — 647
// attempts on one row — and the panel showed nothing at all, so nobody knew a
// member with an expired subscription was still sitting in the channel. When
// the bot gives up, the row says so and names the reason Telegram gave.
function AccessRevokeBadge({ sub }: { sub: Sub }) {
  if (!sub.access_revoke_abandoned_at) return null;
  return (
    <div className="mt-1">
      <Badge variant="destructive">не удалён из Telegram</Badge>
      <div className="text-xs text-muted-foreground mt-0.5 max-w-[18rem]">
        Бот прекратил попытки {formatDate(sub.access_revoke_abandoned_at)}. Удалите участника
        вручную.
        {sub.access_revoke_error ? <> Ошибка: {sub.access_revoke_error}</> : null}
      </div>
    </div>
  );
}

// GK-483: Grant asked to stay visible in the panel «отдельным статусом, не как
// платящих». `status` still says `active` — the access machinery treats these
// rows exactly like a paid one — so the label has to come from the flag, and it
// has to be the first thing read on the row rather than a footnote.
function CompBadge({ sub }: { sub: Sub }) {
  if (!sub.is_comp) return null;
  return <Badge variant="info">команда</Badge>;
}

// A comp row keeps an `expires_at`, and nothing acts on it any more: the access
// predicate returns true regardless and the hourly removal job cannot reach the
// row. Printing the date unqualified would say the opposite of what is true.
function ExpiryCell({ sub }: { sub: Sub }) {
  if (sub.is_comp) {
    return (
      <div className="text-sm">
        <div>бессрочно</div>
        <div className="text-xs text-muted-foreground">
          дата в строке ({formatDate(sub.expires_at)}) больше ни на что не влияет
        </div>
      </div>
    );
  }
  return <span className="text-sm">{formatDate(sub.expires_at)}</span>;
}

// GK-433: the only deadline this queue has is the next charge. Showing the date
// alone made a row renewing in three days look identical to one renewing in
// three months, so the days remaining are spelled out and coloured.
const URGENT_DAYS = 7;

function daysUntil(iso: string): number {
  return Math.ceil((new Date(iso).getTime() - Date.now()) / 86_400_000);
}

function ChargeCountdown({ expiresAt }: { expiresAt: string }) {
  const left = daysUntil(expiresAt);
  const tone =
    left < 0 ? "text-muted-foreground" : left <= URGENT_DAYS ? "text-red-600 font-medium" : "";
  return (
    <div className={tone}>
      <div>{formatDate(expiresAt)}</div>
      <div className="text-xs">
        {left < 0 ? "дата прошла" : left === 0 ? "сегодня" : `через ${left} дн.`}
      </div>
    </div>
  );
}

function CancellationQueue() {
  const qc = useQueryClient();
  const queue = useQuery<{ items: Cancellation[]; total: number }>({
    queryKey: ["cancellations"],
    queryFn: () => api("/subscriptions/cancellations?open_only=true&limit=100"),
  });

  const resolve = useMutation({
    mutationFn: (id: number) =>
      api(`/subscriptions/${id}/cancellation/resolve`, {
        method: "POST",
        body: JSON.stringify({ note: "Отменено вручную в панели провайдера" }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cancellations"] });
      qc.invalidateQueries({ queryKey: ["subs"] });
    },
  });

  const items = queue.data?.items ?? [];
  // GK-468: this block hides itself when there is nothing to do, so a failed
  // fetch used to hide it too — the one queue whose whole point (GK-433) is
  // that a member asked to stop being charged and the card is still live.
  // "Не знаем" has to look different from "всё чисто".
  if (queue.isError) {
    return (
      <QueryError
        error={queue.error}
        onRetry={() => queue.refetch()}
        retrying={queue.isFetching}
        title="Очередь ручных отмен не загрузилась"
        description="Пока она не открывается, нельзя утверждать, что отменять нечего."
      />
    );
  }
  if (queue.isLoading || items.length === 0) return null;

  return (
    <Card className="border-amber-500/50">
      <CardHeader>
        <h2 className="text-lg font-semibold">Требуют ручной отмены автопродления</h2>
        <p className="text-sm text-muted-foreground">
          {items.length}: подписку нужно остановить в панели провайдера вручную — автоматическая
          отмена не подтверждена, списание всё ещё возможно.
        </p>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Пользователь</TableHead>
              <TableHead>Провайдер</TableHead>
              <TableHead>Контракт</TableHead>
              <TableHead>Email покупателя</TableHead>
              <TableHead>Платёж</TableHead>
              <TableHead>Запрошено</TableHead>
              <TableHead>Списание до</TableHead>
              <TableHead>Причина</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((c) => (
              <TableRow key={c.subscription_id}>
                <TableCell className="font-medium">
                  {c.username ? `@${c.username}` : `User #${c.user_id}`}
                  <div className="text-xs text-muted-foreground">tg {c.tg_id}</div>
                </TableCell>
                <TableCell className="text-sm">{c.provider ?? "—"}</TableCell>
                <TableCell className="text-xs font-mono">
                  {c.provider_subscription_id ?? (
                    // Normal for Lava RUB — no purchase webhook means no contract
                    // id ever reached us; only the dashboard can stop it.
                    <span className="text-amber-600">нет id — искать по email</span>
                  )}
                </TableCell>
                <TableCell className="text-sm">{c.buyer_email ?? "—"}</TableCell>
                <TableCell className="text-sm">{c.payment_id ? `#${c.payment_id}` : "—"}</TableCell>
                <TableCell className="text-sm">
                  {c.cancel_requested_at ? formatDate(c.cancel_requested_at) : "—"}
                </TableCell>
                <TableCell className="text-sm">
                  <ChargeCountdown expiresAt={c.expires_at} />
                </TableCell>
                <TableCell className="text-xs text-muted-foreground max-w-[220px] truncate">
                  {c.cancel_failure_reason ?? "—"}
                </TableCell>
                <TableCell>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={resolve.isPending}
                    onClick={() => resolve.mutate(c.subscription_id)}
                  >
                    Отменено вручную
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

export default function SubsPage() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  // GK-483: "" = all, "true" = only the team, "false" = only real subscribers.
  const [comp, setComp] = useState("");
  const subs = useQuery<{ items: Sub[]; total: number }>({
    queryKey: ["subs", status, comp],
    queryFn: () =>
      api(
        `/subscriptions?limit=100${status ? "&status=" + status : ""}${
          comp ? "&comp=" + comp : ""
        }`
      ),
  });

  // GK-483: the one place the team mark is applied, and it is applied by a named
  // admin — the API writes an audit row for every toggle. Who is on the team is
  // a list only Grant can supply; it must never be inferred from who happens to
  // be in the chat.
  const setCompFlag = useMutation({
    mutationFn: ({ id, is_comp }: { id: number; is_comp: boolean }) =>
      api(`/subscriptions/${id}/comp`, {
        method: "POST",
        body: JSON.stringify({ is_comp }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subs"] });
      qc.invalidateQueries({ queryKey: ["sum"] });
    },
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Подписки</h1>
        <p className="text-sm text-muted-foreground">
          {subs.isError ? "количество неизвестно" : `${subs.data?.total ?? 0} всего`}
        </p>
      </div>
      <CancellationQueue />
      <Card>
        <CardHeader>
          <div className="flex flex-wrap gap-2">
            <Select value={status} onChange={(e) => setStatus(e.target.value)} className="max-w-[180px]">
              <option value="">Все</option>
              <option value="active">Активные</option>
              <option value="expired">Истёкшие</option>
              <option value="cancelled">Отменённые</option>
              <option value="gifted">Подарочные</option>
            </Select>
            {/* GK-483 */}
            <Select value={comp} onChange={(e) => setComp(e.target.value)} className="max-w-[220px]">
              <option value="">Команда и подписчики</option>
              <option value="false">Только подписчики</option>
              <option value="true">Только команда</option>
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          {subs.isError && (
            <QueryError
              error={subs.error}
              onRetry={() => subs.refetch()}
              retrying={subs.isFetching}
              title="Подписки не загрузились"
              description="Пустая таблица ниже — это отсутствие ответа, а не отсутствие подписок."
              className="mb-4"
            />
          )}
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Пользователь</TableHead>
                <TableHead>Тариф</TableHead>
                <TableHead>Статус</TableHead>
                <TableHead>Автопродление</TableHead>
                <TableHead>Источник</TableHead>
                <TableHead>Начало</TableHead>
                <TableHead>Окончание</TableHead>
                <TableHead>Команда</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {subs.data?.items.map((s) => (
                <TableRow key={s.id}>
                  <TableCell className="font-medium">{s.username ? `@${s.username}` : `User #${s.user_id}`}</TableCell>
                  <TableCell>{s.plan_name}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-1">
                      <CompBadge sub={s} />
                      <Badge variant={statusVariant[s.status]}>{s.status}</Badge>
                    </div>
                    <AccessRevokeBadge sub={s} />
                  </TableCell>
                  <TableCell><CancelBadge state={s.cancel_state} /></TableCell>
                  <TableCell className="text-muted-foreground text-sm">{s.source}</TableCell>
                  <TableCell className="text-sm">{formatDate(s.started_at)}</TableCell>
                  <TableCell><ExpiryCell sub={s} /></TableCell>
                  <TableCell>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={setCompFlag.isPending}
                      onClick={() => {
                        // Both directions are consequential and neither is
                        // obvious from the button: marking grants access that
                        // never expires, unmarking hands the row back to the
                        // hourly removal job, which for an old date means the
                        // member is removed from the channel within the hour.
                        const who = s.username ? `@${s.username}` : `User #${s.user_id}`;
                        const question = s.is_comp
                          ? `Снять отметку «команда» с ${who}? Доступ снова начнёт истекать по дате ${formatDate(
                              s.expires_at
                            )} — если она уже прошла, участника удалит из канала ближайшая проверка.`
                          : `Отметить ${who} как команду? Подписка перестанет учитываться как платящая, а доступ перестанет истекать.`;
                        if (confirm(question)) {
                          setCompFlag.mutate({ id: s.id, is_comp: !s.is_comp });
                        }
                      }}
                    >
                      {s.is_comp ? "Снять" : "Отметить"}
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
