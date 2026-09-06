"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  ClipboardCopy,
  RefreshCw,
  Send,
  Trophy,
  WalletCards,
  XCircle,
} from "lucide-react";
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
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { QueryErrorGroup } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDateTime, formatMoney } from "@/lib/utils";

interface Leader {
  user_id: number;
  username: string | null;
  first_name: string | null;
  referrals: number;
}

interface PayoutCommission {
  id: number;
  referrer_id: number;
  referrer_username: string | null;
  referrer_first_name: string | null;
  referee_id: number;
  referee_username: string | null;
  referee_first_name: string | null;
  source_provider: string;
  source_invoice_id: string | null;
  source_amount: number;
  source_currency: string;
  fx_rate_to_usd: number | null;
  amount_usd: number;
  status: string;
  vests_at: string;
  vested_at: string | null;
  paid_at: string | null;
  payout_batch_id: number | null;
}

interface PayoutBatch {
  id: number;
  status: "draft" | "sent" | "paid" | "cancelled";
  currency: string;
  threshold_amount: number;
  total_amount: number;
  commission_count: number;
  note: string | null;
  created_at: string;
  sent_at: string | null;
  paid_at: string | null;
  cancelled_at: string | null;
  commissions: PayoutCommission[];
}

interface PayoutSummary {
  month: string;
  generated_at: string;
  unbatched_vested_count: number;
  unbatched_vested_amount_usd: number;
  batch_counts: Record<string, number>;
  batch_amounts_usd: Record<string, number>;
  telegram_text: string;
}

interface PartnerStats {
  referrer_id: number;
  referrer_username: string | null;
  referrer_first_name: string | null;
  invited_count: number;
  paid_count: number;
  accrued_usd: number;
  pending_usd: number;
  available_usd: number;
  paid_usd: number;
}

interface Relationship {
  referee_id: number;
  referee_username: string | null;
  referee_first_name: string | null;
  referee_joined_at: string;
  referrer_id: number;
  referrer_username: string | null;
  referrer_first_name: string | null;
  source: string | null;
  attributed_at: string | null;
  review_status: string | null;
  commission_count: number;
  accrued_usd: number;
  vested_usd: number;
  paid_usd: number;
}

const sourceLabels: Record<string, string> = {
  telegram_deeplink: "Telegram-ссылка",
  promo_code: "Промокод",
  admin: "Админ",
  legacy: "Legacy",
};

const reviewStatus: Record<string, { label: string; variant: "success" | "warning" | "muted" }> = {
  clear: { label: "OK", variant: "success" },
  suspicious: { label: "Подозрительно", variant: "warning" },
  dismissed: { label: "Отклонено", variant: "muted" },
};

const batchStatus: Record<
  PayoutBatch["status"],
  { label: string; variant: "success" | "warning" | "info" | "muted" }
> = {
  draft: { label: "Черновик", variant: "warning" },
  sent: { label: "Отправлено", variant: "info" },
  paid: { label: "Оплачено", variant: "success" },
  cancelled: { label: "Отменено", variant: "muted" },
};

// BLK-005 / GK-100: minimum accrued commissions before a payout batch can be
// drafted. Mirrors backend PAYOUT_THRESHOLD_USD; payouts run monthly via support.
const PAYOUT_THRESHOLD_USD = 100;

const transitionLabels = {
  sent: "Отметить отправленным",
  paid: "Отметить оплаченным",
  cancelled: "Отменить batch",
} as const;

type TransitionAction = keyof typeof transitionLabels;

function person(username: string | null, firstName: string | null, id: number) {
  if (username) return `@${username}`;
  return firstName || `User #${id}`;
}

/**
 * What the dollar figure above it was made from (GK-457).
 *
 * A rouble sale and a dollar sale used to land in the same column looking
 * identical, which is how a rouble figure got summed as dollars for two months
 * without anyone spotting it. A USD payment says nothing extra — the basis and
 * the total are the same money. Anything else shows the basis and the rate, and
 * a row with no rate says so instead of letting $0.00 read as "earned nothing".
 */
function CommissionBasis({ commission }: { commission: PayoutCommission }) {
  if (commission.source_currency === "USD" && commission.fx_rate_to_usd !== null) {
    return null;
  }
  const basis = `${commission.source_amount.toLocaleString("ru-RU")} ${commission.source_currency}`;
  if (commission.fx_rate_to_usd === null) {
    return (
      <div className="text-xs font-normal text-amber-600 dark:text-amber-500">
        {basis} · курс не записан
      </div>
    );
  }
  return (
    <div className="text-xs font-normal text-muted-foreground">
      {basis} · курс {commission.fx_rate_to_usd}
    </div>
  );
}

export default function ReferralsPage() {
  const queryClient = useQueryClient();
  const [batchNote, setBatchNote] = useState("");
  const [transition, setTransition] = useState<{
    batch: PayoutBatch;
    action: TransitionAction;
  } | null>(null);
  const [transitionTx, setTransitionTx] = useState("");
  const [transitionNote, setTransitionNote] = useState("");

  const board = useQuery<Leader[]>({
    queryKey: ["leaderboard-full"],
    queryFn: () => api<Leader[]>("/referrals/leaderboard?limit=50"),
  });

  const summary = useQuery<PayoutSummary>({
    queryKey: ["referral-payout-summary"],
    queryFn: () => api<PayoutSummary>("/referrals/payouts/summary"),
    refetchInterval: 60_000,
  });

  const commissions = useQuery<{ items: PayoutCommission[]; total: number; total_amount_usd: number }>({
    queryKey: ["referral-payout-commissions"],
    queryFn: () =>
      api<{ items: PayoutCommission[]; total: number; total_amount_usd: number }>(
        "/referrals/payouts/commissions?status=vested&batched=false&limit=200"
      ),
    refetchInterval: 60_000,
  });

  const batches = useQuery<{ items: PayoutBatch[]; total: number }>({
    queryKey: ["referral-payout-batches"],
    queryFn: () => api<{ items: PayoutBatch[]; total: number }>("/referrals/payouts/batches?limit=50"),
    refetchInterval: 60_000,
  });

  const relationships = useQuery<{ items: Relationship[]; total: number }>({
    queryKey: ["referral-relationships"],
    queryFn: () =>
      api<{ items: Relationship[]; total: number }>("/referrals/relationships?limit=200"),
    refetchInterval: 60_000,
  });

  const partners = useQuery<{ items: PartnerStats[]; total: number }>({
    queryKey: ["referral-partners"],
    queryFn: () =>
      api<{ items: PartnerStats[]; total: number }>("/referrals/partners?limit=200"),
    refetchInterval: 60_000,
  });

  const invalidatePayouts = () => {
    queryClient.invalidateQueries({ queryKey: ["referral-payout-summary"] });
    queryClient.invalidateQueries({ queryKey: ["referral-payout-commissions"] });
    queryClient.invalidateQueries({ queryKey: ["referral-payout-batches"] });
  };

  const generateBatch = useMutation({
    mutationFn: () =>
      api<PayoutBatch>("/referrals/payouts/batches", {
        method: "POST",
        body: JSON.stringify({ note: batchNote.trim() || undefined }),
      }),
    onSuccess: () => {
      setBatchNote("");
      invalidatePayouts();
    },
  });

  const transitionBatch = useMutation({
    mutationFn: ({
      batch,
      action,
      txHash,
      note,
    }: {
      batch: PayoutBatch;
      action: TransitionAction;
      txHash?: string;
      note?: string;
    }) =>
      api<PayoutBatch>(`/referrals/payouts/batches/${batch.id}/transition`, {
        method: "POST",
        body: JSON.stringify({ action, tx_hash: txHash, note }),
      }),
    onSuccess: () => {
      setTransition(null);
      setTransitionTx("");
      setTransitionNote("");
      invalidatePayouts();
    },
  });

  const openTransition = (batch: PayoutBatch, action: TransitionAction) => {
    setTransition({ batch, action });
    setTransitionTx("");
    setTransitionNote("");
  };

  // Summary eligibility is calculated per partner by the backend. The raw
  // commission list also includes partners still accumulating toward $100.
  const readyTotal = summary.data?.unbatched_vested_amount_usd ?? 0;
  const readyCount = summary.data?.unbatched_vested_count ?? 0;
  const vestedCount = commissions.data?.total ?? 0;
  const paidThisMonth = summary.data?.batch_amounts_usd.paid ?? 0;
  const draftCount = summary.data?.batch_counts.draft ?? 0;

  const canSubmitTransition = useMemo(() => {
    if (!transition) return false;
    if (transition.action !== "paid") return true;
    return Boolean(transitionTx.trim() || transitionNote.trim());
  }, [transition, transitionNote, transitionTx]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Рефералы и выплаты</h1>
          <p className="text-sm text-muted-foreground">
            {readyCount} комиссий готово к выплате · порог {formatMoney(PAYOUT_THRESHOLD_USD, "USD")} · ежемесячно через поддержку (1–5 число)
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={invalidatePayouts}>
            <RefreshCw className="h-4 w-4" />
            Обновить
          </Button>
          <Button onClick={() => generateBatch.mutate()} disabled={generateBatch.isPending || readyTotal < PAYOUT_THRESHOLD_USD}>
            <WalletCards className="h-4 w-4" />
            Создать batch
          </Button>
        </div>
      </div>

      {/* GK-468: this screen is built from six endpoints and every number on it
          falls back to 0 — "$0 готово к выплате" is indistinguishable from
          "выплаты не загрузились". One banner for the whole page rather than
          six stacked boxes; retry re-runs only what failed. */}
      <QueryErrorGroup
        queries={[board, summary, commissions, batches, relationships, partners]}
        title="Часть данных по рефералам не загрузилась"
        description="Суммы и счётчики ниже могут быть занижены или показывать нули — это не подтверждённые значения."
      />

      <div className="grid gap-3 md:grid-cols-4">
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-muted-foreground">Готово к выплате</div>
            <div className="mt-1 text-2xl font-semibold">{formatMoney(readyTotal, "USD")}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-muted-foreground">Комиссий</div>
            <div className="mt-1 text-2xl font-semibold">{readyCount}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-muted-foreground">Черновики</div>
            <div className="mt-1 text-2xl font-semibold">{draftCount}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-muted-foreground">Оплачено за месяц</div>
            <div className="mt-1 text-2xl font-semibold">{formatMoney(paidThisMonth, "USD")}</div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Статистика партнёров</CardTitle>
          <CardDescription>
            {partners.data?.total ?? 0} партнёров · видно сразу: приглашено, оплатило, накоплено (включая
            ожидающие) и доступно к выводу
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Партнёр</TableHead>
                <TableHead className="text-right">Приглашено</TableHead>
                <TableHead className="text-right">Оплатило</TableHead>
                <TableHead className="text-right">Накоплено всего</TableHead>
                <TableHead className="text-right">Доступно к выводу</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {partners.data?.items.map((partner) => (
                <TableRow key={partner.referrer_id}>
                  <TableCell className="font-medium">
                    {person(partner.referrer_username, partner.referrer_first_name, partner.referrer_id)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm">{partner.invited_count}</TableCell>
                  <TableCell className="text-right font-mono text-sm">{partner.paid_count}</TableCell>
                  <TableCell className="text-right font-medium">
                    {partner.accrued_usd > 0 ? formatMoney(partner.accrued_usd, "USD") : "—"}
                    {partner.pending_usd > 0 && (
                      <div className="text-xs font-normal text-muted-foreground">
                        ожидает {formatMoney(partner.pending_usd, "USD")}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-medium">
                    {partner.available_usd > 0 ? formatMoney(partner.available_usd, "USD") : "—"}
                  </TableCell>
                </TableRow>
              ))}
              {partners.data?.items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                    Партнёров пока нет.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Связи рефералов (кто кого пригласил)</CardTitle>
          <CardDescription>
            {relationships.data?.total ?? 0} связей · A → B видно с момента приглашения, до начисления комиссии
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Пригласил (A)</TableHead>
                <TableHead>Приглашённый (B)</TableHead>
                <TableHead>Источник</TableHead>
                <TableHead>Дата</TableHead>
                <TableHead className="text-right">Комиссии</TableHead>
                <TableHead className="text-right">Начислено</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {relationships.data?.items.map((rel) => {
                const status = rel.review_status ? reviewStatus[rel.review_status] : undefined;
                return (
                  <TableRow key={rel.referee_id}>
                    <TableCell className="font-medium">
                      {person(rel.referrer_username, rel.referrer_first_name, rel.referrer_id)}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap items-center gap-2">
                        {person(rel.referee_username, rel.referee_first_name, rel.referee_id)}
                        {status && rel.review_status !== "clear" && (
                          <Badge variant={status.variant}>{status.label}</Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {rel.source ? sourceLabels[rel.source] ?? rel.source : "—"}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatDateTime(rel.attributed_at ?? rel.referee_joined_at)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">{rel.commission_count}</TableCell>
                    <TableCell className="text-right font-medium">
                      {rel.accrued_usd > 0 ? formatMoney(rel.accrued_usd, "USD") : "—"}
                    </TableCell>
                  </TableRow>
                );
              })}
              {relationships.data?.items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="py-8 text-center text-muted-foreground">
                    Связей рефералов пока нет.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[1.5fr_1fr]">
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <CardTitle>Выплатные batch-и</CardTitle>
                <CardDescription>{batches.data?.total ?? 0} записей</CardDescription>
              </div>
              <Input
                value={batchNote}
                onChange={(event) => setBatchNote(event.target.value)}
                placeholder="Заметка к новому batch"
                className="max-w-sm"
              />
            </div>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Batch</TableHead>
                  <TableHead>Сумма</TableHead>
                  <TableHead>Комиссии</TableHead>
                  <TableHead>Даты</TableHead>
                  <TableHead className="text-right">Действия</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {batches.data?.items.map((batch) => {
                  const status = batchStatus[batch.status];
                  const isOpen = batch.status === "draft" || batch.status === "sent";
                  return (
                    <TableRow key={batch.id}>
                      <TableCell className="min-w-[220px]">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-xs">#{batch.id}</span>
                          <Badge variant={status.variant}>{status.label}</Badge>
                        </div>
                        {batch.note && (
                          <div className="mt-2 max-w-[360px] whitespace-pre-wrap break-words text-xs text-muted-foreground">
                            {batch.note}
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="font-medium">{formatMoney(batch.total_amount, batch.currency)}</TableCell>
                      <TableCell>
                        <div className="font-mono text-sm">{batch.commission_count}</div>
                        <div className="mt-1 space-y-1 text-xs text-muted-foreground">
                          {batch.commissions.slice(0, 3).map((commission) => (
                            <div key={commission.id}>
                              {person(
                                commission.referrer_username,
                                commission.referrer_first_name,
                                commission.referrer_id
                              )}{" "}
                              · {formatMoney(commission.amount_usd, "USD")}
                            </div>
                          ))}
                          {batch.commissions.length > 3 && <div>+{batch.commissions.length - 3}</div>}
                        </div>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        <div>Создан: {formatDateTime(batch.created_at)}</div>
                        {batch.sent_at && <div>Sent: {formatDateTime(batch.sent_at)}</div>}
                        {batch.paid_at && <div>Paid: {formatDateTime(batch.paid_at)}</div>}
                        {batch.cancelled_at && <div>Cancel: {formatDateTime(batch.cancelled_at)}</div>}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex flex-wrap justify-end gap-2">
                          {batch.status === "draft" && (
                            <Button size="sm" variant="outline" onClick={() => openTransition(batch, "sent")}>
                              <Send className="h-4 w-4" />
                              Sent
                            </Button>
                          )}
                          {isOpen && (
                            <Button size="sm" onClick={() => openTransition(batch, "paid")}>
                              <CheckCircle2 className="h-4 w-4" />
                              Paid
                            </Button>
                          )}
                          {isOpen && (
                            <Button size="sm" variant="ghost" onClick={() => openTransition(batch, "cancelled")}>
                              <XCircle className="h-4 w-4" />
                              Cancel
                            </Button>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
                {batches.data?.items.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                      Batch-ей пока нет.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex items-center justify-between gap-3">
              <div>
                <CardTitle>Telegram summary</CardTitle>
                <CardDescription>{summary.data?.month ?? ""}</CardDescription>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => navigator.clipboard.writeText(summary.data?.telegram_text ?? "")}
              >
                <ClipboardCopy className="h-4 w-4" />
                Copy
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <Textarea
              readOnly
              value={summary.data?.telegram_text ?? ""}
              className="min-h-[180px] font-mono text-xs"
            />
            <div className="mt-3 text-xs text-muted-foreground">
              {summary.data?.generated_at ? `Generated: ${formatDateTime(summary.data.generated_at)}` : ""}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Начисленные комиссии</CardTitle>
          <CardDescription>
            {vestedCount} без batch · выплата при балансе партнёра от {formatMoney(PAYOUT_THRESHOLD_USD, "USD")}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Реферер</TableHead>
                <TableHead>Приглашенный</TableHead>
                <TableHead>Источник</TableHead>
                <TableHead>Vested</TableHead>
                <TableHead className="text-right">Комиссия</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {commissions.data?.items.map((commission) => (
                <TableRow key={commission.id}>
                  <TableCell className="font-medium">
                    {person(commission.referrer_username, commission.referrer_first_name, commission.referrer_id)}
                  </TableCell>
                  <TableCell>
                    {person(commission.referee_username, commission.referee_first_name, commission.referee_id)}
                  </TableCell>
                  <TableCell>
                    <div className="uppercase text-xs text-muted-foreground">{commission.source_provider}</div>
                    <div className="max-w-[220px] truncate font-mono text-xs text-muted-foreground">
                      {commission.source_invoice_id ?? "—"}
                    </div>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {commission.vested_at ? formatDateTime(commission.vested_at) : formatDateTime(commission.vests_at)}
                  </TableCell>
                  <TableCell className="text-right font-medium">
                    {formatMoney(commission.amount_usd, "USD")}
                    <CommissionBasis commission={commission} />
                  </TableCell>
                </TableRow>
              ))}
              {commissions.data?.items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                    Нет комиссий для выплаты.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-3">
            <Trophy className="h-5 w-5 text-amber-500" />
            <div>
              <CardTitle>Лидерборд</CardTitle>
              <CardDescription>Топ-50 по количеству приглашенных пользователей</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-12">#</TableHead>
                <TableHead>Пользователь</TableHead>
                <TableHead className="text-right">Приглашений</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {board.data?.map((leader, index) => (
                <TableRow key={leader.user_id}>
                  <TableCell className="font-semibold text-muted-foreground">{index + 1}</TableCell>
                  <TableCell className="font-medium">
                    {person(leader.username, leader.first_name, leader.user_id)}
                  </TableCell>
                  <TableCell className="text-right font-mono">{leader.referrals}</TableCell>
                </TableRow>
              ))}
              {board.data?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={3} className="py-8 text-center text-muted-foreground">
                    Пока нет приглашений.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={transition !== null} onOpenChange={(open) => !open && setTransition(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{transition ? transitionLabels[transition.action] : ""}</DialogTitle>
            <DialogDescription>
              Batch #{transition?.batch.id} ·{" "}
              {transition ? formatMoney(transition.batch.total_amount, transition.batch.currency) : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <Input
              value={transitionTx}
              onChange={(event) => setTransitionTx(event.target.value)}
              placeholder="Tx hash / референс выплаты"
            />
            <Textarea
              value={transitionNote}
              onChange={(event) => setTransitionNote(event.target.value)}
              placeholder="Заметка"
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setTransition(null)}>
              Закрыть
            </Button>
            <Button
              disabled={!transition || !canSubmitTransition || transitionBatch.isPending}
              onClick={() =>
                transition &&
                transitionBatch.mutate({
                  batch: transition.batch,
                  action: transition.action,
                  txHash: transitionTx.trim() || undefined,
                  note: transitionNote.trim() || undefined,
                })
              }
            >
              <CheckCircle2 className="h-4 w-4" />
              Сохранить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
