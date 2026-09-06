import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function formatMoney(amount: number, currency = "USD"): string {
  const symbol = currency === "USD" ? "$" : currency === "RUB" ? "₽" : currency;
  if (currency !== "RUB") return `${symbol}${amount.toFixed(2)}`;
  // F26 (GK-468): this used to be Math.round(), which let the refund dialog
  // contradict itself — "доступно к возврату 1 500 ₽" above a placeholder of
  // 1499.50, a discrepancy of up to ₽0.99 on the one screen where the number
  // is the decision. Whole rubles still print without a fractional part, so
  // amounts that were already exact don't get noisier.
  const digits = Number.isInteger(amount) ? 0 : 2;
  const formatted = amount.toLocaleString("ru-RU", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return `${formatted} ${symbol}`;
}

export function formatDate(d: string | Date): string {
  const date = typeof d === "string" ? new Date(d) : d;
  return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
}

export function formatDateTime(d: string | Date): string {
  const date = typeof d === "string" ? new Date(d) : d;
  return date.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function relativeDate(d: string | Date): string {
  const date = typeof d === "string" ? new Date(d) : d;
  const diff = Math.floor((Date.now() - date.getTime()) / 1000);
  if (diff < 60) return "только что";
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  if (diff < 604800) return `${Math.floor(diff / 86400)} дн назад`;
  return formatDate(date);
}
