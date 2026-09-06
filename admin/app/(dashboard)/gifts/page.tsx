"use client";

import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { QueryError } from "@/components/query-error";
import { api } from "@/lib/api";
import { formatDateTime, formatMoney } from "@/lib/utils";

interface Payment {
  id: number;
  username: string | null;
  plan_name: string | null;
  amount: number;
  currency: string;
  status: string;
  is_gift: boolean;
  gift_recipient_username: string | null;
  created_at: string;
}

export default function GiftsPage() {
  const list = useQuery<{ items: Payment[] }>({
    queryKey: ["gifts"],
    queryFn: () => api<{ items: Payment[] }>("/payments?limit=100"),
    select: (d) => ({ items: d.items.filter((p) => p.is_gift) }),
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">🎁 Подарочные подписки</h1>
        <p className="text-sm text-muted-foreground">Транзакции, в которых получатель ≠ покупателю</p>
      </div>
      <Card>
        <CardHeader />
        <CardContent>
          {list.isError && (
            <QueryError
              error={list.error}
              onRetry={() => list.refetch()}
              retrying={list.isFetching}
              title="Подарочные подписки не загрузились"
              className="mb-4"
            />
          )}
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>#</TableHead>
                <TableHead>От кого</TableHead>
                <TableHead>Кому</TableHead>
                <TableHead>Тариф</TableHead>
                <TableHead>Сумма</TableHead>
                <TableHead>Статус</TableHead>
                <TableHead>Дата</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.data?.items.map((p) => (
                <TableRow key={p.id}>
                  <TableCell className="font-mono text-xs">{p.id}</TableCell>
                  <TableCell>{p.username ? `@${p.username}` : "—"}</TableCell>
                  <TableCell>{p.gift_recipient_username ? `@${p.gift_recipient_username}` : "—"}</TableCell>
                  <TableCell>{p.plan_name}</TableCell>
                  <TableCell>{formatMoney(p.amount, p.currency)}</TableCell>
                  <TableCell>
                    <Badge variant={p.status === "succeeded" ? "success" : "muted"}>{p.status}</Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">{formatDateTime(p.created_at)}</TableCell>
                </TableRow>
              ))}
              {list.data?.items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground py-8">
                    Пока без подарков.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
