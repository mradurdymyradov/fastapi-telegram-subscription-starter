"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BarChart3,
  Users,
  CreditCard,
  Gift,
  Trophy,
  Megaphone,
  Settings,
  Plug,
  ListChecks,
  MessageCircle,
  PackageOpen,
  ShieldCheck,
  Scale,
  Film,
  FolderTree,
  RefreshCw,
  Ticket,
  LogOut,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api, setToken } from "@/lib/api";
import { PANEL_NAME_PLACEHOLDER } from "@/lib/branding";

const nav = [
  { href: "/reconciliation", label: "Сверка", icon: Scale },
  { href: "/", label: "Дашборд", icon: BarChart3 },
  { href: "/users", label: "Пользователи", icon: Users },
  { href: "/subscriptions", label: "Подписки", icon: PackageOpen },
  { href: "/payments", label: "Платежи", icon: CreditCard },
  { href: "/payments/manual", label: "Модерация", icon: ListChecks, badge: "moderation" },
  { href: "/referrals", label: "Рефералы", icon: Trophy },
  { href: "/promocodes", label: "Промокоды", icon: Ticket },
  { href: "/gifts", label: "Подарки", icon: Gift },
  { href: "/broadcasts", label: "Рассылки", icon: Megaphone },
  { href: "/archive/videos", label: "Видео архива", icon: Film },
  { href: "/archive/modules", label: "Разделы архива", icon: FolderTree },
  { href: "/archive/sync", label: "Синхро Vimeo", icon: RefreshCw },
  { href: "/support", label: "Поддержка", icon: MessageCircle },
  { href: "/integrations", label: "Интеграции", icon: Plug },
  { href: "/audit", label: "Журнал действий", icon: ShieldCheck },
  { href: "/settings", label: "Настройки", icon: Settings },
];

export function Sidebar({ awaitingCount = 0 }: { awaitingCount?: number }) {
  const pathname = usePathname();
  return (
    <aside className="w-60 shrink-0 border-r bg-card flex flex-col h-screen sticky top-0">
      <div className="px-5 py-5 border-b">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-md bg-primary text-primary-foreground grid place-items-center font-bold">M</div>
          <div>
            <div className="text-sm font-semibold leading-tight">{PANEL_NAME_PLACEHOLDER}</div>
            <div className="text-xs text-muted-foreground">Закрытое сообщество</div>
          </div>
        </div>
      </div>
      <nav className="flex-1 overflow-y-auto px-2 py-3 space-y-1">
        {nav.map((item) => {
          const Icon = item.icon;
          const active = pathname === item.href || (item.href !== "/" && pathname.startsWith(item.href));
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors",
                active
                  ? "bg-primary/10 text-primary font-medium"
                  : "text-muted-foreground hover:bg-accent hover:text-foreground"
              )}
            >
              <Icon className="w-4 h-4" />
              <span className="flex-1">{item.label}</span>
              {item.badge === "moderation" && awaitingCount > 0 && (
                <span className="text-xs px-1.5 py-0.5 rounded-full bg-warning/20 text-amber-700 font-semibold">
                  {awaitingCount}
                </span>
              )}
            </Link>
          );
        })}
      </nav>
      <div className="border-t p-3">
        <button
          onClick={async () => {
            // Tell the server to blacklist this jti before we drop the token
            // locally, so a stolen-already token can't outlive the click.
            try {
              await api("/auth/logout", { method: "POST" });
            } catch {
              // ignore — we still wipe the local token and redirect
            }
            setToken(null);
            window.location.href = "/login";
          }}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground hover:text-destructive transition-colors"
        >
          <LogOut className="w-4 h-4" /> Выйти
        </button>
      </div>
    </aside>
  );
}
