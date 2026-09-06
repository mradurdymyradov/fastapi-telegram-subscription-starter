"use client";

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

interface Point { date: string; amount: number }

export function RevenueChart({ data, currency = "USD" }: { data: Point[]; currency?: string }) {
  const sym = currency === "USD" ? "$" : currency === "RUB" ? "₽" : currency;
  // RUB shows the symbol after the number (ru-RU convention); USD/other before it.
  const axisFmt = (v: number) =>
    currency === "RUB" ? `${Math.round(v).toLocaleString("ru-RU")} ₽` : `${sym}${v}`;
  const tipFmt = (v: number) =>
    currency === "RUB" ? `${Math.round(v).toLocaleString("ru-RU")} ₽` : `${sym}${v.toFixed(2)}`;

  const formatted = data.map((p) => ({
    ...p,
    label: new Date(p.date).toLocaleDateString("ru-RU", { day: "2-digit", month: "short" }),
  }));
  return (
    <div className="w-full h-72">
      <ResponsiveContainer>
        <AreaChart data={formatted} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="rev" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="hsl(142 71% 45%)" stopOpacity={0.45} />
              <stop offset="95%" stopColor="hsl(142 71% 45%)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e5e7eb" />
          <XAxis dataKey="label" tick={{ fontSize: 11, fill: "#6b7280" }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fontSize: 11, fill: "#6b7280" }} axisLine={false} tickLine={false} tickFormatter={axisFmt} />
          <Tooltip
            contentStyle={{ border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 12 }}
            formatter={(v: number) => [tipFmt(v), "Выручка"]}
          />
          <Area type="monotone" dataKey="amount" stroke="hsl(142 71% 35%)" strokeWidth={2} fill="url(#rev)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
