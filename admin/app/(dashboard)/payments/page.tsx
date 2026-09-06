"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { RotateCcw } from "lucide-react";
import { QueryError } from "@/components/query-error";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, formatMoney } from "@/lib/utils";

interface Payment {
  id: number;
  username: string | null;
  plan_name: string | null;
  provider: string;
  amount: number;
  currency: string;
  status: string;
  is_gift: boolean;
  gift_recipient_username: string | null;
  created_at: string;
  refunded_amount: number;
  refundable: boolean;
  refund_mode: string; // auto | manual
}

const statusBadge: Record<string, { variant: "success" | "warning" | "destructive" | "muted"; label: string }> = {
  succeeded: { variant: "success", label: "Оплачено" },
  awaiting_review: { variant: "warning", label: "На модерации" },
  failed: { variant: "destructive", label: "Ошибка" },
  pending: { variant: "muted", label: "Ожидание" },
  refunded: { variant: "muted", label: "Возврат" },
};

export default function PaymentsPage() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [provider, setProvider] = useState("");
  const [refunding, setRefunding] = useState<Payment | null>(null);
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [forceManual, setForceManual] = useState(false);
  // ?focus=<id> deep-link from the moderation journal (/audit). Read on the
  // client only so the page doesn't need a Suspense boundary for prerender.
  const [focusId, setFocusId] = useState<number | null>(null);

  const list = useQuery<{ items: Payment[]; total: number }>({
    queryKey: ["payments", status, provider, focusId],
    queryFn: () => {
      const params = new URLSearchParams({ limit: "100" });
      if (focusId !== null) {
        params.set("payment_id", String(focusId));
      } else {
        if (status) params.set("status", status);
        if (provider) params.set("provider", provider);
      }
      return api(`/payments?${params.toString()}`);
    },
  });

  useEffect(() => {
    const f = new URLSearchParams(window.location.search).get("focus");
    if (f && /^\d+$/.test(f)) setFocusId(Number(f));
  }, []);

  useEffect(() => {
    if (focusId == null || !list.data) return;
    document.getElementById(`pay-${focusId}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focusId, list.data]);

  const refund = useMutation({
    mutationFn: (b: { id: number; amount: number | null; reason: string; manual: boolean }) =>
      api(`/payments/${b.id}/refund`, {
        method: "POST",
        body: JSON.stringify({ amount: b.amount, reason: b.reason, manual: b.manual }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["payments"] });
      qc.invalidateQueries({ queryKey: ["sum"] });
      closeDialog();
    },
  });

  function openDialog(p: Payment) {
    setRefunding(p);
    setAmount("");
    setReason("");
    setForceManual(false);
  }
  function closeDialog() {
    setRefunding(null);
    setAmount("");
    setReason("");
    setForceManual(false);
    refund.reset();
  }

  const remaining = refunding ? Math.max(refunding.amount - refunding.refunded_amount, 0) : 0;
  const isManual = !!refunding && (refunding.refund_mode === "manual" || forceManual);
  const parsedAmount = amount.trim() === "" ? null : Number(amount);
  const amountValid =
    parsedAmount === null || (Number.isFinite(parsedAmount) && parsedAmount > 0 && parsedAmount <= remaining + 1e-9);
  const willFullyRefund = parsedAmount === null || parsedAmount >= remaining - 1e-9;
  const reasonValid = !isManual || reason.trim().length > 0;
  const refundError = refund.error instanceof ApiError ? refund.error : null;
  const refundErrorDetail =
    refundError && typeof refundError.body === "object" && refundError.body !== null
      ? (refundError.body as { detail?: string }).detail
      : null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Платежи</h1>
          <p className="text-sm text-muted-foreground">
            {list.isError ? "количество неизвестно" : `${list.data?.total ?? 0} всего`}
          </p>
        </div>
        <Button variant="outline" asChild>
          <Link href="/payments/manual">Очередь модерации →</Link>
        </Button>
      </div>
      <Card>
        <CardHeader>
          <div className="flex flex-wrap gap-3">
            <Select value={status} onChange={(e) => setStatus(e.target.value)} className="max-w-[180px]">
              <option value="">Все статусы</option>
              <option value="succeeded">Оплаченные</option>
              <option value="awaiting_review">На модерации</option>
              <option value="failed">Ошибка</option>
              <option value="refunded">Возвраты</option>
            </Select>
            <Select value={provider} onChange={(e) => setProvider(e.target.value)} className="max-w-[180px]">
              <option value="">Все провайдеры</option>
              <option value="stripe">Stripe</option>
              <option value="lava">Lava</option>
              <option value="zelle">Zelle</option>
              <option value="usdt">USDT</option>
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          {list.isError && (
            <QueryError
              error={list.error}
              onRetry={() => list.refetch()}
              retrying={list.isFetching}
              title="Платежи не загрузились"
              description="Пустая таблица ниже — это отсутствие ответа, а не отсутствие платежей."
              className="mb-4"
            />
          )}
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>#</TableHead>
                <TableHead>Покупатель</TableHead>
                <TableHead>Получатель</TableHead>
                <TableHead>Тариф</TableHead>
                <TableHead>Сумма</TableHead>
                <TableHead>Метод</TableHead>
                <TableHead>Статус</TableHead>
                <TableHead>Дата</TableHead>
                <TableHead className="text-right">Действие</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.data?.items.map((p) => {
                const b = statusBadge[p.status] ?? { variant: "muted" as const, label: p.status };
                return (
                  <TableRow
                    key={p.id}
                    id={`pay-${p.id}`}
                    className={p.id === focusId ? "bg-warning/15 ring-2 ring-warning/60" : undefined}
                  >
                    <TableCell className="font-mono text-xs">{p.id}</TableCell>
                    <TableCell>{p.username ? `@${p.username}` : "—"}</TableCell>
                    <TableCell>
                      {p.is_gift ? (
                        <span>
                          🎁 {p.gift_recipient_username ? `@${p.gift_recipient_username}` : "—"}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">себе</span>
                      )}
                    </TableCell>
                    <TableCell>{p.plan_name || "—"}</TableCell>
                    <TableCell className="font-medium">
                      {formatMoney(p.amount, p.currency)}
                      {p.refunded_amount > 0 && (
                        <span className="block text-xs text-muted-foreground">
                          возврат: {formatMoney(p.refunded_amount, p.currency)}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="uppercase text-xs text-muted-foreground">{p.provider}</TableCell>
                    <TableCell><Badge variant={b.variant}>{b.label}</Badge></TableCell>
                    <TableCell className="text-sm text-muted-foreground">{formatDateTime(p.created_at)}</TableCell>
                    <TableCell className="text-right">
                      {p.refundable ? (
                        <Button variant="outline" size="sm" onClick={() => openDialog(p)}>
                          <RotateCcw className="w-3.5 h-3.5" /> Возврат
                        </Button>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={!!refunding} onOpenChange={(o) => !o && closeDialog()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Возврат — Платёж #{refunding?.id}</DialogTitle>
            <DialogDescription>
              {refunding && (
                <>
                  Оплачено {formatMoney(refunding.amount, refunding.currency)} ·{" "}
                  уже возвращено {formatMoney(refunding.refunded_amount, refunding.currency)} ·{" "}
                  доступно к возврату {formatMoney(remaining, refunding.currency)}.
                </>
              )}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <div className="space-y-1">
              <label className="text-sm font-medium">Сумма возврата</label>
              <Input
                type="number"
                step="0.01"
                min="0"
                placeholder={`Полный возврат (${remaining.toFixed(2)})`}
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Пусто = полный возврат остатка. Меньше остатка = частичный.
              </p>
            </div>

            <div className="space-y-1">
              <label className="text-sm font-medium">
                Причина{isManual ? " (обязательно)" : " (необязательно)"}
              </label>
              <Textarea
                placeholder="Например: запрос клиента, дубликат платежа…"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                rows={3}
              />
            </div>

            {refunding?.refund_mode === "auto" ? (
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={forceManual}
                  onChange={(e) => setForceManual(e.target.checked)}
                />
                Я уже сделал возврат вручную в дашборде провайдера (записать без вызова API)
              </label>
            ) : (
              <div className="rounded border bg-muted p-2 text-xs text-muted-foreground">
                {refunding?.provider === "usdt" || refunding?.provider === "zelle"
                  ? "Ручной возврат: отправьте средства обратно вручную, затем запишите возврат здесь."
                  : "Авто-возврат недоступен для этого платежа — сделайте возврат в дашборде провайдера и запишите его здесь вручную."}
              </div>
            )}

            {willFullyRefund && amountValid && (
              <div className="rounded border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-400">
                ⚠️ Полный возврат закроет доступ пользователя: подписка завершится сразу,
                и он будет удалён из закрытого канала.
              </div>
            )}

            {refundErrorDetail && (
              <div className="rounded border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
                {refundErrorDetail}
              </div>
            )}
          </div>

          <DialogFooter>
            <Button variant="ghost" onClick={closeDialog}>
              Отмена
            </Button>
            <Button
              variant="destructive"
              disabled={refund.isPending || !amountValid || !reasonValid}
              onClick={() =>
                refunding &&
                refund.mutate({
                  id: refunding.id,
                  amount: parsedAmount,
                  reason: reason.trim(),
                  manual: forceManual,
                })
              }
            >
              Оформить возврат
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
