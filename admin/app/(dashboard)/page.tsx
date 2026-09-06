"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CreditCard, DollarSign, ListChecks, TrendingDown, UserPlus, Users } from "lucide-react";
import { MetricCard } from "@/components/metric-card";
import { QueryError } from "@/components/query-error";
import { RevenueChart } from "@/components/revenue-chart";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import { cn, formatMoney, relativeDate } from "@/lib/utils";

interface CurrencyAmount { currency: string; amount: number }

// GK-437. "ok" = the newest dump was decrypted and restored within the window.
// The three failure states are distinct because they need different actions:
// deploy the canary, restart it, or go and fix the backups.
interface BackupVerification {
  state: "ok" | "never" | "stale" | "failed";
  verified_at: string | null;
  age_hours: number | null;
  dump_file: string | null;
  detail: string | null;
  headline: string;
}

interface Summary {
  active_subs: number;
  // GK-483: the client's own team — real access, deliberately not counted as
  // paying. Shown beside `active_subs` rather than folded into it, because
  // Grant asked the team to stay visible in the panel, just not as subscribers.
  comp_subs: number;
  new_users_7d: number;
  // Per-currency revenue — RUB and USD are reported separately, never summed (GK-414).
  revenue_30d: CurrencyAmount[];
  revenue_7d: CurrencyAmount[];
  mrr_usd: number;
  revenue_30d_usd: number;
  revenue_7d_usd: number;
  awaiting_review: number;
  // GK-433: cancellations the provider refused, still waiting on a human.
  manual_cancellations_open: number;
  churn_30d_pct: number;
  total_users: number;
  // GK-437: whether the newest backup was actually restorable, and when.
  backup_verification: BackupVerification;
}

interface RevenuePoint { date: string; amount: number }

const currencySymbol = (c: string) => (c === "USD" ? "$" : c === "RUB" ? "₽" : c);

/** Stack each currency on its own line; RUB is never folded into a USD total. */
function CurrencyLines({ items, empty }: { items?: CurrencyAmount[]; empty: string }) {
  if (!items || items.length === 0) return <>{empty}</>;
  return (
    <div className="flex flex-col gap-0.5">
      {items.map((c) => (
        <span key={c.currency}>{formatMoney(c.amount, c.currency)}</span>
      ))}
    </div>
  );
}

const joinCurrencies = (items?: CurrencyAmount[]) =>
  items && items.length ? items.map((c) => formatMoney(c.amount, c.currency)).join(" · ") : "";

interface PaymentRow {
  id: number;
  username: string | null;
  plan_name: string | null;
  amount: number;
  currency: string;
  provider: string;
  status: string;
  created_at: string;
}

interface Leader { user_id: number; username: string | null; first_name: string | null; referrals: number }

const providerLabel: Record<string, string> = { stripe: "Stripe", lava: "Lava", zelle: "Zelle", usdt: "USDT" };
const statusBadge: Record<string, { variant: "success" | "warning" | "destructive" | "muted"; label: string }> = {
  succeeded: { variant: "success", label: "Оплачено" },
  awaiting_review: { variant: "warning", label: "На модерации" },
  failed: { variant: "destructive", label: "Ошибка" },
  pending: { variant: "muted", label: "Ожидает" },
};

export default function DashboardPage() {
  const summary = useQuery<Summary>({ queryKey: ["sum"], queryFn: () => api<Summary>("/metrics/summary") });
  const s = summary.data;

  // Currencies actually present in the last 30 days, largest first — drives the chart toggle.
  const chartCurrencies = useMemo(() => {
    const items = [...(s?.revenue_30d ?? [])].sort((a, b) => b.amount - a.amount);
    return items.map((c) => c.currency);
  }, [s?.revenue_30d]);

  const [pickedCurrency, setPickedCurrency] = useState<string | null>(null);
  const chartCurrency = pickedCurrency ?? chartCurrencies[0] ?? "USD";

  const revenue = useQuery<RevenuePoint[]>({
    queryKey: ["rev30", chartCurrency],
    queryFn: () => api<RevenuePoint[]>(`/metrics/revenue?days=30&currency=${encodeURIComponent(chartCurrency)}`),
  });
  const recent = useQuery<{ items: PaymentRow[] }>({
    queryKey: ["recent-pay"],
    queryFn: () => api<{ items: PaymentRow[] }>("/payments?limit=8"),
  });
  const leaders = useQuery<Leader[]>({ queryKey: ["leaders"], queryFn: () => api<Leader[]>("/referrals/leaderboard?limit=5") });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Дашборд</h1>
        <p className="text-sm text-muted-foreground">Ключевые метрики за последние 30 дней.</p>
        {/* The successful case gets a line too, not just the failures: the point
            of GK-437 is that "the newest backup was restorable as of X" is a
            fact somebody can read, rather than an assumption nobody tests. */}
        {s && s.backup_verification.state === "ok" && (
          <p className="text-xs text-muted-foreground mt-1">
            Бэкап: последний дамп{" "}
            {s.backup_verification.dump_file ? <code>{s.backup_verification.dump_file}</code> : ""}{" "}
            развёрнут из шифра и проверён{" "}
            {s.backup_verification.verified_at
              ? relativeDate(s.backup_verification.verified_at)
              : "—"}
            .
          </p>
        )}
      </div>

      {/* GK-433: the manual-cancellation queue existed on the Подписки page and
          still went unworked for 13 days, because seeing it required opening
          that page. Anything above zero here is a member who asked to stop
          being charged and whose card is still live at the provider. */}
      {s && s.manual_cancellations_open > 0 && (
        <Link
          href="/subscriptions"
          className="flex items-center gap-3 rounded-lg border border-amber-500/60 bg-amber-50 px-4 py-3 text-amber-900 hover:bg-amber-100 transition-colors"
        >
          <AlertTriangle className="w-5 h-5 shrink-0" />
          <span className="text-sm">
            <b>{s.manual_cancellations_open}</b>{" "}
            {s.manual_cancellations_open === 1 ? "подписку" : "подписок"} нужно отменить вручную в
            панели провайдера — списание всё ещё возможно. Открыть очередь →
          </span>
        </Link>
      )}

      {/* GK-437: "бэкап сделан" и "бэкап разворачивается" — разные утверждения.
          На 2026-08-09 приватный ключ age не проходил собственную контрольную
          сумму, а задача бэкапа каждую ночь рапортовала об успехе. Здесь всегда
          написано, когда последний дамп в последний раз действительно
          развернули — либо почему этого не знает никто. */}
      {s && s.backup_verification.state !== "ok" && (
        <div className="flex items-start gap-3 rounded-lg border border-red-500/60 bg-red-50 px-4 py-3 text-red-900">
          <AlertTriangle className="w-5 h-5 shrink-0 mt-0.5" />
          <div className="text-sm space-y-1">
            <div>
              <b>{s.backup_verification.headline}.</b>{" "}
              {s.backup_verification.state === "failed"
                ? "Файлы бэкапов есть, но последний из них не разворачивается — значит бэкапа нет."
                : "Пока проверка не идёт, «бэкап есть» — это предположение, а не факт."}
            </div>
            {s.backup_verification.detail && (
              <div className="text-xs opacity-80 break-words">{s.backup_verification.detail}</div>
            )}
            <div className="text-xs opacity-80">
              Что делать: docs/runbooks/backup_restore.md
            </div>
          </div>
        </div>
      )}

      {/* GK-468: a failed /metrics/summary used to leave every card on "—"
          forever, which reads as "ноль", not as "неизвестно" — and it also
          silently swallows the two alert banners above, so an unresolved
          backup failure or an uncancelled subscription would look like all
          clear. One loud block instead of four quiet lies. */}
      {summary.isError ? (
        <QueryError
          error={summary.error}
          onRetry={() => summary.refetch()}
          retrying={summary.isFetching}
          title="Метрики не загрузились"
          description="Показатели ниже не равны нулю — они неизвестны. Предупреждения о бэкапах и ручных отменах на этой странице сейчас тоже не проверяются."
        />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <MetricCard
            label="Выручка / 30 дней"
            value={s ? <CurrencyLines items={s.revenue_30d} empty={formatMoney(0, "USD")} /> : "—"}
            hint={s ? `${joinCurrencies(s.revenue_7d) || formatMoney(0, "USD")} за 7 дней` : ""}
            icon={<DollarSign className="w-5 h-5" />}
          />
          <MetricCard
            label="Активных подписок"
            value={s ? s.active_subs : "—"}
            hint={
              s
                ? `${s.total_users} юзеров всего` +
                  (s.comp_subs > 0 ? ` · команда: ${s.comp_subs} (не в счёте)` : "")
                : ""
            }
            icon={<Users className="w-5 h-5" />}
            accent="info"
          />
          <MetricCard
            label="Новых за 7 дней"
            value={s ? `+${s.new_users_7d}` : "—"}
            icon={<UserPlus className="w-5 h-5" />}
            accent="primary"
          />
          <MetricCard
            label="Churn / 30 дней"
            value={s ? `${s.churn_30d_pct}%` : "—"}
            icon={<TrendingDown className="w-5 h-5" />}
            accent="warning"
          />
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-2">
          <CardHeader>
            <div className="flex items-center justify-between gap-3">
              <div>
                <CardTitle>Выручка</CardTitle>
                <CardDescription>Успешные платежи по дням — {currencySymbol(chartCurrency)} {chartCurrency}</CardDescription>
              </div>
              <div className="flex items-center gap-2">
                {chartCurrencies.length > 1 && (
                  <div className="inline-flex rounded-md border p-0.5">
                    {chartCurrencies.map((c) => (
                      <button
                        key={c}
                        type="button"
                        onClick={() => setPickedCurrency(c)}
                        className={cn(
                          "px-2.5 py-1 text-xs font-medium rounded",
                          c === chartCurrency ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"
                        )}
                      >
                        {currencySymbol(c)} {c}
                      </button>
                    ))}
                  </div>
                )}
                {s && s.awaiting_review > 0 && (
                  <Badge variant="warning" className="gap-1">
                    <ListChecks className="w-3 h-3" /> {s.awaiting_review} ждут модерации
                  </Badge>
                )}
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {revenue.isError ? (
              <QueryError
                error={revenue.error}
                onRetry={() => revenue.refetch()}
                retrying={revenue.isFetching}
                title="График выручки не загрузился"
                description="Пустой график здесь означал бы отсутствие платежей — это не так."
              />
            ) : revenue.data ? (
              <RevenueChart data={revenue.data} currency={chartCurrency} />
            ) : (
              <div className="h-72 grid place-items-center text-muted-foreground text-sm">Загрузка…</div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Топ-5 рефереров</CardTitle>
            <CardDescription>Кто приводит больше всего</CardDescription>
          </CardHeader>
          <CardContent>
            {leaders.isError ? (
              <QueryError
                error={leaders.error}
                onRetry={() => leaders.refetch()}
                retrying={leaders.isFetching}
                title="Рефереры не загрузились"
              />
            ) : leaders.data && leaders.data.length > 0 ? (
              <ol className="space-y-2.5">
                {leaders.data.map((l, i) => (
                  <li key={l.user_id} className="flex items-center gap-3">
                    <span className="w-6 text-center text-sm font-semibold text-muted-foreground">
                      {["🥇", "🥈", "🥉"][i] ?? `${i + 1}.`}
                    </span>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium truncate">
                        {l.username ? `@${l.username}` : l.first_name || `User #${l.user_id}`}
                      </div>
                      <div className="text-xs text-muted-foreground">{l.referrals} приглашённых</div>
                    </div>
                  </li>
                ))}
              </ol>
            ) : leaders.isLoading ? (
              <div className="text-sm text-muted-foreground">Загрузка…</div>
            ) : (
              <div className="text-sm text-muted-foreground">Пока пусто.</div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Последние платежи</CardTitle>
              <CardDescription>8 самых свежих транзакций</CardDescription>
            </div>
            <CreditCard className="w-5 h-5 text-muted-foreground" />
          </div>
        </CardHeader>
        <CardContent>
          {recent.isError ? (
            <QueryError
              error={recent.error}
              onRetry={() => recent.refetch()}
              retrying={recent.isFetching}
              title="Последние платежи не загрузились"
              description="Это не значит, что платежей нет."
            />
          ) : recent.data?.items.length ? (
            <div className="divide-y">
              {recent.data.items.map((p) => {
                const b = statusBadge[p.status] ?? { variant: "muted" as const, label: p.status };
                return (
                  <div key={p.id} className="py-3 flex items-center gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="font-medium text-sm truncate">
                        {p.username ? `@${p.username}` : `User`} · {p.plan_name || "—"}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {providerLabel[p.provider] || p.provider} · {relativeDate(p.created_at)}
                      </div>
                    </div>
                    <div className="text-sm font-semibold">{formatMoney(p.amount, p.currency)}</div>
                    <Badge variant={b.variant}>{b.label}</Badge>
                  </div>
                );
              })}
            </div>
          ) : recent.isLoading ? (
            <div className="text-sm text-muted-foreground py-6 text-center">Загрузка…</div>
          ) : (
            <div className="text-sm text-muted-foreground py-6 text-center">Платежей пока нет.</div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
