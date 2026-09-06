"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/select";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/utils";

interface AuditRow {
  id: number;
  actor_admin_id: number | null;
  actor_label: string | null;
  actor_ip: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

interface ActorRow {
  actor_admin_id: number;
  email: string | null;
}

// Curated moderation action set — mirrors MODERATION_ACTIONS in
// backend/app/api/routers/audit.py. Used to colour the badge and to know which
// rows are member-facing moderation decisions.
const MODERATION_ACTIONS = new Set([
  "payment.approve",
  "payment.reject",
  "payment.usdt_verify",
  "payment.refund.request",
  "payment.refund.resolve",
  "payment.refund.sync",
  "support.reply",
]);

// Human-readable Russian labels. Anything unmapped falls back to the raw code.
const ACTION_LABELS: Record<string, string> = {
  "payment.approve": "Платёж одобрен",
  "payment.reject": "Платёж отклонён",
  "payment.usdt_verify": "USDT проверен",
  "payment.refund.request": "Возврат инициирован",
  "payment.refund.resolve": "Возврат подтверждён",
  "payment.refund.sync": "Возврат синхронизирован",
  "support.reply": "Ответ в поддержке",
  "broadcast.create": "Рассылка создана",
  "plan.create": "Тариф создан",
  "plan.update": "Тариф изменён",
  "promocode.create": "Промокод создан",
  "promocode.update": "Промокод изменён",
  "promocode.deactivate": "Промокод отключён",
  "webhook.create": "Вебхук добавлен",
  "webhook.delete": "Вебхук удалён",
  "user.block": "Пользователь заблокирован",
  "user.unblock": "Пользователь разблокирован",
  "user.ban": "Пользователь заблокирован",
  "user.unban": "Пользователь разблокирован",
  "user.extend_days": "Подписка пользователя продлена",
  "referral_payout_batch.create": "Партнёрская выплата создана",
  "referral_payout_batch.sent": "Партнёрская выплата отправлена",
  "referral_payout_batch.paid": "Партнёрская выплата оплачена",
  "referral_payout_batch.cancelled": "Партнёрская выплата отменена",
  "crm_export.google_sheets": "Экспорт в Google Sheets",
  "reconciliation.run": "Сверка запущена",
  "reconciliation.item_resolve": "Пункт сверки закрыт",
  "reconciliation.item_reopen": "Пункт сверки открыт повторно",
  "admin.totp.enable": "2FA включена",
  "admin.totp.disable": "2FA отключена",
  "admin.totp.recovery_login": "Вход по резервному коду",
  "archive.sync.run": "Синхронизация архива",
  "archive.module.create": "Раздел архива создан",
  "archive.module.update": "Раздел архива изменён",
  "archive.module.delete": "Раздел архива удалён",
  "archive.membership.add": "Видео добавлено в раздел",
  "archive.membership.remove": "Видео убрано из раздела",
  "archive.video.update": "Видео архива изменено",
};

function actionLabel(a: string): string {
  return ACTION_LABELS[a] ?? a;
}

// Where to send the admin to open the (now closed) underlying item.
function targetHref(r: AuditRow): string | null {
  if (r.target_type === "payment" && r.target_id) return `/payments?focus=${r.target_id}`;
  if (r.target_type === "refund") {
    const pid = r.details?.payment_id;
    return typeof pid === "number" ? `/payments?focus=${pid}` : "/payments";
  }
  if (r.action === "support.reply") return "/support";
  if (r.target_type === "user" && r.target_id) return "/users";
  return null;
}

function targetText(r: AuditRow): string {
  if (!r.target_type) return "—";
  const labels: Record<string, string> = { payment: "Платёж", refund: "Возврат", user: "Пользователь" };
  const noun = labels[r.target_type] ?? r.target_type;
  return `${noun} #${r.target_id ?? "—"}`;
}

export default function AuditPage() {
  const [category, setCategory] = useState<"moderation" | "all">("moderation");
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");

  const actors = useQuery<ActorRow[]>({
    queryKey: ["audit-actors"],
    queryFn: () => api<ActorRow[]>("/audit/actors"),
  });

  const list = useQuery<AuditRow[]>({
    queryKey: ["audit", category, action, actor],
    queryFn: () => {
      const p = new URLSearchParams();
      p.set("limit", "200");
      // A specific action is more precise than the broad category — when one is
      // chosen, drop the category filter so they can't contradict each other.
      if (action) p.set("action", action);
      else if (category === "moderation") p.set("category", "moderation");
      if (actor) p.set("actor_admin_id", actor);
      return api<AuditRow[]>(`/audit/log?${p.toString()}`);
    },
    refetchInterval: 30_000,
  });

  const actionOptions = Object.entries(ACTION_LABELS).sort((a, b) => a[1].localeCompare(b[1], "ru"));

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">🛡️ Журнал модерации</h1>
        <p className="text-sm text-muted-foreground">
          Кто, когда и что решал по платежам, возвратам и обращениям. Записи нельзя удалить.
        </p>
      </div>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap gap-3">
            <Select
              value={category}
              onChange={(e) => setCategory(e.target.value as "moderation" | "all")}
              className="max-w-[200px]"
            >
              <option value="moderation">Только модерация</option>
              <option value="all">Все действия</option>
            </Select>
            <Select value={action} onChange={(e) => setAction(e.target.value)} className="max-w-[240px]">
              <option value="">Любое действие</option>
              {actionOptions.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </Select>
            <Select value={actor} onChange={(e) => setActor(e.target.value)} className="max-w-[240px]">
              <option value="">Любой администратор</option>
              {actors.data?.map((a) => (
                <option key={a.actor_admin_id} value={String(a.actor_admin_id)}>
                  {a.email ?? `admin #${a.actor_admin_id}`}
                </option>
              ))}
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          {/* GK-468: an empty journal and an unreachable journal looked identical,
              on the one page whose whole purpose is "these records cannot be
              deleted" — silence here must never pass for "никто ничего не делал". */}
          {list.isError && (
            <QueryError
              error={list.error}
              onRetry={() => list.refetch()}
              retrying={list.isFetching}
              title="Журнал не загрузился"
              description="Отсутствие записей ниже не означает отсутствие действий."
              className="mb-4"
            />
          )}
          {list.isLoading && <div className="text-sm text-muted-foreground">Загрузка…</div>}
          <div className="divide-y">
            {list.data?.map((r) => {
              const href = targetHref(r);
              const isMod = MODERATION_ACTIONS.has(r.action);
              return (
                <div key={r.id} className="py-3 grid grid-cols-[auto_1fr] gap-3">
                  <Badge variant={isMod ? "warning" : "muted"}>{actionLabel(r.action)}</Badge>
                  <div className="min-w-0">
                    <div className="text-xs text-muted-foreground mb-1">
                      {r.actor_label ?? (r.actor_admin_id ? `admin #${r.actor_admin_id}` : "система")} · IP{" "}
                      {r.actor_ip ?? "—"} · {formatDateTime(r.created_at)}
                    </div>
                    <div className="text-sm">
                      {href ? (
                        <Link href={href} className="text-primary hover:underline">
                          {targetText(r)} →
                        </Link>
                      ) : (
                        targetText(r)
                      )}
                    </div>
                    {Object.keys(r.details ?? {}).length > 0 && (
                      <pre className="mt-1 text-xs bg-muted rounded p-2 overflow-x-auto whitespace-pre-wrap break-words">
                        {JSON.stringify(r.details, null, 2)}
                      </pre>
                    )}
                  </div>
                </div>
              );
            })}
            {list.data?.length === 0 && (
              <div className="text-center text-muted-foreground py-6">
                Нет записей под выбранный фильтр.
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
