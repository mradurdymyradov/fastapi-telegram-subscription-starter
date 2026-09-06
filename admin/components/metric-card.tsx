import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

interface Props {
  label: string;
  value: ReactNode;
  hint?: string;
  delta?: { value: number; positive?: boolean };
  icon?: ReactNode;
  accent?: "primary" | "warning" | "info" | "neutral";
}

const accentBg = {
  primary: "bg-primary/10 text-primary",
  warning: "bg-amber-100 text-amber-700",
  info: "bg-sky-100 text-sky-700",
  neutral: "bg-muted text-muted-foreground",
};

export function MetricCard({ label, value, hint, delta, icon, accent = "primary" }: Props) {
  return (
    <Card>
      <CardContent className="pt-6">
        <div className="flex items-start justify-between">
          <div>
            <div className="text-sm text-muted-foreground">{label}</div>
            <div className="mt-2 text-3xl font-semibold tracking-tight">{value}</div>
            {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
            {delta && (
              <div
                className={cn(
                  "mt-2 inline-flex items-center text-xs font-medium px-1.5 py-0.5 rounded",
                  delta.positive
                    ? "bg-emerald-100 text-emerald-700"
                    : "bg-red-100 text-red-700"
                )}
              >
                {delta.positive ? "↑" : "↓"} {Math.abs(delta.value)}%
              </div>
            )}
          </div>
          {icon && (
            <div className={cn("w-10 h-10 rounded-lg grid place-items-center", accentBg[accent])}>{icon}</div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
