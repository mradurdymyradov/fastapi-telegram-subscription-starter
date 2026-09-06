"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, RefreshCw, Ticket, Users2, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { QueryError } from "@/components/query-error";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, formatMoney } from "@/lib/utils";

interface PromoCode {
  id: number;
  code: string;
  description: string | null;
  discount_type: "percent" | "fixed";
  percent_off: number | null;
  amount_off: number | null;
  amount_off_currency: string;
  applies_to_plan_codes: string[];
  max_redemptions: number | null;
  redeemed_count: number;
  valid_from: string | null;
  valid_until: string | null;
  is_active: boolean;
  referrer_user_id: number | null;
  referrer_username: string | null;
  created_at: string;
  updated_at: string;
}

interface Redemption {
  id: number;
  user_id: number;
  username: string | null;
  payment_id: number | null;
  status: string;
  plan_code: string | null;
  currency: string;
  original_amount: number;
  discount_amount: number;
  final_amount: number;
  created_at: string;
  cancelled_at: string | null;
}

interface FormState {
  code: string;
  description: string;
  discount_type: "percent" | "fixed";
  percent_off: string;
  amount_off: string;
  amount_off_currency: "USD" | "RUB";
  applies_to_plan_codes: string;
  max_redemptions: string;
  valid_from: string;
  valid_until: string;
  is_active: boolean;
  referrer_user_id: string;
}

const EMPTY_FORM: FormState = {
  code: "",
  description: "",
  discount_type: "percent",
  percent_off: "20",
  amount_off: "",
  amount_off_currency: "USD",
  applies_to_plan_codes: "",
  max_redemptions: "",
  valid_from: "",
  valid_until: "",
  is_active: true,
  referrer_user_id: "",
};

function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(
    d.getMinutes()
  )}`;
}

function fromLocalInput(local: string): string | null {
  if (!local.trim()) return null;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

function formToPayload(form: FormState) {
  return {
    code: form.code.trim().toUpperCase(),
    description: form.description.trim() || null,
    discount_type: form.discount_type,
    percent_off: form.discount_type === "percent" ? Number(form.percent_off) : null,
    amount_off: form.discount_type === "fixed" ? Number(form.amount_off) : null,
    amount_off_currency: form.amount_off_currency,
    applies_to_plan_codes: form.applies_to_plan_codes
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    max_redemptions: form.max_redemptions.trim() ? Number(form.max_redemptions) : null,
    valid_from: fromLocalInput(form.valid_from),
    valid_until: fromLocalInput(form.valid_until),
    is_active: form.is_active,
    referrer_user_id: form.referrer_user_id.trim() ? Number(form.referrer_user_id) : null,
  };
}

function promoToForm(p: PromoCode): FormState {
  return {
    code: p.code,
    description: p.description ?? "",
    discount_type: p.discount_type,
    percent_off: p.percent_off != null ? String(p.percent_off) : "",
    amount_off: p.amount_off != null ? String(p.amount_off) : "",
    amount_off_currency: (p.amount_off_currency as "USD" | "RUB") || "USD",
    applies_to_plan_codes: (p.applies_to_plan_codes || []).join(", "),
    max_redemptions: p.max_redemptions != null ? String(p.max_redemptions) : "",
    valid_from: toLocalInput(p.valid_from),
    valid_until: toLocalInput(p.valid_until),
    is_active: p.is_active,
    referrer_user_id: p.referrer_user_id != null ? String(p.referrer_user_id) : "",
  };
}

function discountLabel(p: PromoCode): string {
  if (p.discount_type === "percent") return `${p.percent_off ?? 0}%`;
  return formatMoney(p.amount_off ?? 0, p.amount_off_currency);
}

function windowState(p: PromoCode): { label: string; variant: "success" | "warning" | "muted" } {
  if (!p.is_active) return { label: "Отключён", variant: "muted" };
  const now = Date.now();
  if (p.valid_from && new Date(p.valid_from).getTime() > now)
    return { label: "Ещё не активен", variant: "warning" };
  if (p.valid_until && new Date(p.valid_until).getTime() < now)
    return { label: "Истёк", variant: "warning" };
  if (p.max_redemptions != null && p.redeemed_count >= p.max_redemptions)
    return { label: "Исчерпан", variant: "warning" };
  return { label: "Активен", variant: "success" };
}

export default function PromoCodesPage() {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<PromoCode | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [redemptionsFor, setRedemptionsFor] = useState<PromoCode | null>(null);

  const codes = useQuery<PromoCode[]>({
    queryKey: ["promocodes"],
    queryFn: () => api<PromoCode[]>("/promocodes?limit=500"),
  });

  const redemptions = useQuery<Redemption[]>({
    queryKey: ["promocode-redemptions", redemptionsFor?.id],
    queryFn: () => api<Redemption[]>(`/promocodes/${redemptionsFor!.id}/redemptions?limit=500`),
    enabled: redemptionsFor !== null,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["promocodes"] });

  const save = useMutation({
    mutationFn: () => {
      const payload = formToPayload(form);
      if (editing) {
        return api<PromoCode>(`/promocodes/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
      }
      return api<PromoCode>("/promocodes", { method: "POST", body: JSON.stringify(payload) });
    },
    onSuccess: () => {
      setDialogOpen(false);
      setEditing(null);
      setForm(EMPTY_FORM);
      setFormError(null);
      invalidate();
    },
    onError: (err) => {
      const body = err instanceof ApiError ? err.body : null;
      const detail =
        body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : null;
      setFormError(typeof detail === "string" ? detail : "Не удалось сохранить промокод.");
    },
  });

  const deactivate = useMutation({
    mutationFn: (id: number) => api<PromoCode>(`/promocodes/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setFormError(null);
    setDialogOpen(true);
  };

  const openEdit = (p: PromoCode) => {
    setEditing(p);
    setForm(promoToForm(p));
    setFormError(null);
    setDialogOpen(true);
  };

  const stats = useMemo(() => {
    const list = codes.data ?? [];
    const active = list.filter((p) => windowState(p).label === "Активен").length;
    const redemptionsTotal = list.reduce((acc, p) => acc + p.redeemed_count, 0);
    return { total: list.length, active, redemptionsTotal };
  }, [codes.data]);

  const canSubmit =
    form.code.trim().length >= 2 &&
    (form.discount_type === "percent" ? Number(form.percent_off) > 0 : Number(form.amount_off) > 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Промокоды</h1>
          <p className="text-sm text-muted-foreground">
            Скидочные коды со своим лимитом, сроком и привязкой к рефереру
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={invalidate}>
            <RefreshCw className="h-4 w-4" />
            Обновить
          </Button>
          <Button onClick={openCreate}>
            <Plus className="h-4 w-4" />
            Создать промокод
          </Button>
        </div>
      </div>

      {/* GK-468: all three counters are derived from `codes`, so a failed fetch
          renders three confident zeros. */}
      {codes.isError && (
        <QueryError
          error={codes.error}
          onRetry={() => codes.refetch()}
          retrying={codes.isFetching}
          title="Промокоды не загрузились"
          description="Нули в счётчиках ниже — это отсутствие ответа, а не отсутствие кодов."
        />
      )}

      <div className="grid gap-3 md:grid-cols-3">
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-muted-foreground">Всего кодов</div>
            <div className="mt-1 text-2xl font-semibold">{stats.total}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-muted-foreground">Активных</div>
            <div className="mt-1 text-2xl font-semibold">{stats.active}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-muted-foreground">Всего использований</div>
            <div className="mt-1 text-2xl font-semibold">{stats.redemptionsTotal}</div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Список промокодов</CardTitle>
          <CardDescription>
            {codes.isError ? "количество неизвестно" : `${codes.data?.length ?? 0} записей`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Код</TableHead>
                <TableHead>Скидка</TableHead>
                <TableHead>Тарифы</TableHead>
                <TableHead>Использования</TableHead>
                <TableHead>Срок</TableHead>
                <TableHead>Статус</TableHead>
                <TableHead className="text-right">Действия</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {codes.data?.map((p) => {
                const status = windowState(p);
                return (
                  <TableRow key={p.id}>
                    <TableCell>
                      <div className="font-mono font-medium">{p.code}</div>
                      {p.description && (
                        <div className="max-w-[260px] truncate text-xs text-muted-foreground">
                          {p.description}
                        </div>
                      )}
                      {p.referrer_user_id && (
                        <div className="mt-1 text-xs text-muted-foreground">
                          реферер: {p.referrer_username ? `@${p.referrer_username}` : `#${p.referrer_user_id}`}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="font-medium">{discountLabel(p)}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {p.applies_to_plan_codes.length ? p.applies_to_plan_codes.join(", ") : "все"}
                    </TableCell>
                    <TableCell className="font-mono text-sm">
                      {p.redeemed_count}
                      {p.max_redemptions != null ? ` / ${p.max_redemptions}` : " / ∞"}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      <div>с: {p.valid_from ? formatDateTime(p.valid_from) : "—"}</div>
                      <div>по: {p.valid_until ? formatDateTime(p.valid_until) : "—"}</div>
                    </TableCell>
                    <TableCell>
                      <Badge variant={status.variant}>{status.label}</Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex flex-wrap justify-end gap-2">
                        <Button size="sm" variant="outline" onClick={() => setRedemptionsFor(p)}>
                          <Users2 className="h-4 w-4" />
                          {p.redeemed_count}
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => openEdit(p)}>
                          <Pencil className="h-4 w-4" />
                        </Button>
                        {p.is_active && (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => deactivate.mutate(p.id)}
                            disabled={deactivate.isPending}
                          >
                            <XCircle className="h-4 w-4" />
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
              {codes.data?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                    Промокодов пока нет.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {/* Create / edit dialog */}
      <Dialog
        open={dialogOpen}
        onOpenChange={(open) => {
          setDialogOpen(open);
          if (!open) setFormError(null);
        }}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Ticket className="h-5 w-5" />
              {editing ? `Промокод ${editing.code}` : "Новый промокод"}
            </DialogTitle>
            <DialogDescription>
              Промокод заменяет реферальную скидку, если оба применимы. Скидка действует только на
              первый платёж.
            </DialogDescription>
          </DialogHeader>

          <div className="grid max-h-[60vh] gap-4 overflow-y-auto px-1 py-1 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="code">Код</Label>
              <Input
                id="code"
                value={form.code}
                onChange={(e) => setForm({ ...form, code: e.target.value.toUpperCase() })}
                placeholder="WELCOME20"
                className="font-mono"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="discount_type">Тип скидки</Label>
              <Select
                id="discount_type"
                value={form.discount_type}
                onChange={(e) =>
                  setForm({ ...form, discount_type: e.target.value as "percent" | "fixed" })
                }
              >
                <option value="percent">Процент</option>
                <option value="fixed">Фикс. сумма</option>
              </Select>
            </div>

            {form.discount_type === "percent" ? (
              <div className="space-y-1.5">
                <Label htmlFor="percent_off">Процент скидки</Label>
                <Input
                  id="percent_off"
                  type="number"
                  min={0.01}
                  max={100}
                  step={0.01}
                  value={form.percent_off}
                  onChange={(e) => setForm({ ...form, percent_off: e.target.value })}
                />
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-2">
                <div className="space-y-1.5">
                  <Label htmlFor="amount_off">Сумма скидки</Label>
                  <Input
                    id="amount_off"
                    type="number"
                    min={0.01}
                    step={0.01}
                    value={form.amount_off}
                    onChange={(e) => setForm({ ...form, amount_off: e.target.value })}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="amount_off_currency">Валюта</Label>
                  <Select
                    id="amount_off_currency"
                    value={form.amount_off_currency}
                    onChange={(e) =>
                      setForm({ ...form, amount_off_currency: e.target.value as "USD" | "RUB" })
                    }
                  >
                    <option value="USD">USD</option>
                    <option value="RUB">RUB</option>
                  </Select>
                </div>
              </div>
            )}

            <div className="space-y-1.5">
              <Label htmlFor="max_redemptions">Лимит использований</Label>
              <Input
                id="max_redemptions"
                type="number"
                min={1}
                value={form.max_redemptions}
                onChange={(e) => setForm({ ...form, max_redemptions: e.target.value })}
                placeholder="пусто = без лимита"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="plans">Тарифы (коды через запятую)</Label>
              <Input
                id="plans"
                value={form.applies_to_plan_codes}
                onChange={(e) => setForm({ ...form, applies_to_plan_codes: e.target.value })}
                placeholder="пусто = все (1m, 6m, 12m)"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="valid_from">Действует с</Label>
              <Input
                id="valid_from"
                type="datetime-local"
                value={form.valid_from}
                onChange={(e) => setForm({ ...form, valid_from: e.target.value })}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="valid_until">Действует по</Label>
              <Input
                id="valid_until"
                type="datetime-local"
                value={form.valid_until}
                onChange={(e) => setForm({ ...form, valid_until: e.target.value })}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="referrer">ID реферера (необязательно)</Label>
              <Input
                id="referrer"
                type="number"
                min={1}
                value={form.referrer_user_id}
                onChange={(e) => setForm({ ...form, referrer_user_id: e.target.value })}
                placeholder="инфлюенсер-код → начисляет комиссию"
              />
            </div>
            <div className="flex items-center gap-2 pt-6">
              <input
                id="is_active"
                type="checkbox"
                className="h-4 w-4"
                checked={form.is_active}
                onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
              />
              <Label htmlFor="is_active">Активен</Label>
            </div>

            <div className="space-y-1.5 sm:col-span-2">
              <Label htmlFor="description">Описание</Label>
              <Textarea
                id="description"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="Внутренняя заметка о промокоде"
              />
            </div>
          </div>

          {formError && <div className="px-1 text-sm text-destructive">{formError}</div>}

          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Отмена
            </Button>
            <Button disabled={!canSubmit || save.isPending} onClick={() => save.mutate()}>
              {editing ? "Сохранить" : "Создать"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Redemptions dialog */}
      <Dialog open={redemptionsFor !== null} onOpenChange={(open) => !open && setRedemptionsFor(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Использования {redemptionsFor?.code}</DialogTitle>
            <DialogDescription>
              {redemptions.data?.length ?? 0} записей · {redemptionsFor?.redeemed_count ?? 0} применено
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-[60vh] overflow-y-auto">
            {redemptions.isError && (
              <QueryError
                error={redemptions.error}
                onRetry={() => redemptions.refetch()}
                retrying={redemptions.isFetching}
                title="Использования не загрузились"
                className="mb-4"
              />
            )}
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Пользователь</TableHead>
                  <TableHead>Тариф</TableHead>
                  <TableHead className="text-right">Было</TableHead>
                  <TableHead className="text-right">Скидка</TableHead>
                  <TableHead className="text-right">Стало</TableHead>
                  <TableHead>Когда</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {redemptions.data?.map((r) => (
                  <TableRow key={r.id}>
                    <TableCell>{r.username ? `@${r.username}` : `#${r.user_id}`}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">{r.plan_code ?? "—"}</TableCell>
                    <TableCell className="text-right">{formatMoney(r.original_amount, r.currency)}</TableCell>
                    <TableCell className="text-right text-emerald-600">
                      −{formatMoney(r.discount_amount, r.currency)}
                    </TableCell>
                    <TableCell className="text-right font-medium">
                      {formatMoney(r.final_amount, r.currency)}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {formatDateTime(r.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
                {redemptions.data?.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={6} className="py-8 text-center text-muted-foreground">
                      Пока никто не использовал этот код.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRedemptionsFor(null)}>
              Закрыть
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
