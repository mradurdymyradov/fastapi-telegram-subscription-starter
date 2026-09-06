"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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
import { CheckCircle2, XCircle } from "lucide-react";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDateTime, formatMoney } from "@/lib/utils";

interface Payment {
  id: number;
  username: string | null;
  plan_name: string | null;
  provider: string;
  amount: number;
  currency: string;
  status: string;
  note: string | null;
  screenshot_url: string | null;
  tx_hash: string | null;
  tx_network: string | null;
  tx_confirmed_at: string | null;
  created_at: string;
}

export default function ManualModerationPage() {
  const qc = useQueryClient();
  const [rejecting, setRejecting] = useState<Payment | null>(null);
  const [approving, setApproving] = useState<Payment | null>(null);
  const [txInputs, setTxInputs] = useState<Record<number, string>>({});
  const [reason, setReason] = useState("");

  const list = useQuery<{ items: Payment[]; total: number }>({
    queryKey: ["manual-queue"],
    queryFn: () => api(`/payments?status=awaiting_review&limit=100`),
    refetchInterval: 15_000,
  });

  const decide = useMutation({
    mutationFn: (b: { id: number; decision: "approve" | "reject"; reason?: string }) =>
      api(`/payments/${b.id}/moderate`, {
        method: "POST",
        body: JSON.stringify({ decision: b.decision, reason: b.reason }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["manual-queue"] });
      qc.invalidateQueries({ queryKey: ["sum"] });
      setRejecting(null);
      setApproving(null);
      setReason("");
    },
  });

  const verifyUsdt = useMutation({
    mutationFn: (b: { id: number; tx_hash: string }) =>
      api(`/payments/${b.id}/verify-usdt`, {
        method: "POST",
        body: JSON.stringify({ tx_hash: b.tx_hash }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["manual-queue"] });
      qc.invalidateQueries({ queryKey: ["sum"] });
    },
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Модерация ручных платежей</h1>
        <p className="text-sm text-muted-foreground">
          Zelle / USDT — после подтверждения юзер сразу получит инвайт в сообщество.
        </p>
      </div>

      {/* GK-468: without this the page rendered nothing at all on failure —
          indistinguishable from an empty queue, on the screen that decides
          whether a paying member gets their invite. */}
      {list.isError && (
        <QueryError
          error={list.error}
          onRetry={() => list.refetch()}
          retrying={list.isFetching}
          title="Очередь модерации не загрузилась"
          description="Пустой экран здесь не означает, что платежей на модерации нет."
        />
      )}
      {list.isLoading && <div className="text-sm text-muted-foreground">Загрузка…</div>}
      {list.data?.items.length === 0 && (
        <Card>
          <CardContent className="py-12 text-center text-muted-foreground">
            🎉 Очередь пуста — нет ожидающих платежей.
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {list.data?.items.map((p) => (
          <Card key={p.id}>
            <CardHeader>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <CardTitle className="text-base">
                    Платёж #{p.id} · {formatMoney(p.amount, p.currency)}
                  </CardTitle>
                  <CardDescription>
                    {p.username ? `@${p.username}` : "—"} · {p.plan_name || "—"} ·{" "}
                    <span className="uppercase">{p.provider}</span>
                  </CardDescription>
                </div>
                <Badge variant="warning">Ждёт проверки</Badge>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              {p.note && (
                <div className="text-xs bg-muted rounded p-2 whitespace-pre-line">{p.note}</div>
              )}
              {p.provider === "usdt" && (
                <div className="space-y-2 rounded border p-3 text-xs">
                  <div className="font-medium">USDT tx hash verification</div>
                  {p.tx_hash && (
                    <div className="text-muted-foreground break-all">
                      Stored {p.tx_network || "USDT"} hash: {p.tx_hash}
                    </div>
                  )}
                  <div className="flex gap-2">
                    <Input
                      className="font-mono text-xs"
                      placeholder="Paste TRC20/ERC20 transaction hash"
                      value={txInputs[p.id] ?? p.tx_hash ?? ""}
                      onChange={(e) =>
                        setTxInputs((prev) => ({ ...prev, [p.id]: e.target.value }))
                      }
                    />
                    <Button
                      variant="outline"
                      disabled={verifyUsdt.isPending || !(txInputs[p.id] ?? p.tx_hash ?? "").trim()}
                      onClick={() =>
                        verifyUsdt.mutate({
                          id: p.id,
                          tx_hash: (txInputs[p.id] ?? p.tx_hash ?? "").trim(),
                        })
                      }
                    >
                      Verify
                    </Button>
                  </div>
                  <div className="text-muted-foreground">
                    Valid hashes auto-activate access. Ambiguous hashes stay in this queue.
                  </div>
                </div>
              )}
              {p.screenshot_url ? (
                <a href={p.screenshot_url} target="_blank" rel="noreferrer">
                  <img src={p.screenshot_url} alt="proof" className="rounded border max-h-72 object-contain" />
                </a>
              ) : (
                <div className="text-xs text-muted-foreground italic">
                  Скриншот ещё не получен — юзер только что инициировал платёж.
                </div>
              )}
              <div className="text-xs text-muted-foreground">Создан {formatDateTime(p.created_at)}</div>
              <div className="flex gap-2 pt-2">
                <Button
                  className="flex-1"
                  onClick={() =>
                    p.provider === "usdt"
                      ? setApproving(p)
                      : decide.mutate({ id: p.id, decision: "approve" })
                  }
                >
                  <CheckCircle2 className="w-4 h-4" /> Одобрить
                </Button>
                <Button variant="destructive" className="flex-1" onClick={() => setRejecting(p)}>
                  <XCircle className="w-4 h-4" /> Отклонить
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Dialog open={!!approving} onOpenChange={(o) => !o && setApproving(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Manual USDT approval #{approving?.id}</DialogTitle>
            <DialogDescription>
              Use this only for ambiguous explorer results. The reason is stored in the audit log.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            placeholder="Example: explorer temporarily unavailable, checked manually in Tronscan/Etherscan"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={4}
          />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setApproving(null)}>
              Cancel
            </Button>
            <Button
              disabled={!reason.trim()}
              onClick={() =>
                approving &&
                decide.mutate({ id: approving.id, decision: "approve", reason })
              }
            >
              Approve with reason
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!rejecting} onOpenChange={(o) => !o && setRejecting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Отклонить платёж #{rejecting?.id}</DialogTitle>
            <DialogDescription>
              Причина будет отправлена пользователю в Telegram. Это безвозвратное действие.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            placeholder="Например: скриншот нечитаем, сумма не совпадает…"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={4}
          />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setRejecting(null)}>
              Отмена
            </Button>
            <Button
              variant="destructive"
              disabled={!reason.trim()}
              onClick={() => rejecting && decide.mutate({ id: rejecting.id, decision: "reject", reason })}
            >
              Отклонить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
