"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, setToken, ApiError } from "@/lib/api";
import { PANEL_NAME_PLACEHOLDER } from "@/lib/branding";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [requiresTotp, setRequiresTotp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await api<{ token: string | null; requires_totp: boolean }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({
          email,
          password,
          ...(requiresTotp ? { totp_code: totpCode } : {}),
        }),
      });
      if (res.requires_totp && !res.token) {
        setRequiresTotp(true);
        setTotpCode("");
        return;
      }
      if (!res.token) {
        setError("Неверный код двухфакторной аутентификации");
        return;
      }
      setToken(res.token);
      router.replace("/");
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setError("Слишком много попыток. Подождите минуту и попробуйте снова.");
      } else if (requiresTotp) {
        setError("Неверный код 2FA или код восстановления");
      } else {
        setError("Неверный email или пароль");
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="min-h-screen grid place-items-center bg-gradient-to-br from-emerald-50 via-background to-sky-50 p-4">
      <Card className="w-full max-w-sm shadow-xl">
        <CardHeader className="text-center">
          <div className="mx-auto w-12 h-12 rounded-xl bg-primary text-primary-foreground grid place-items-center font-bold text-xl">
            M
          </div>
          <CardTitle className="mt-2 text-xl">{PANEL_NAME_PLACEHOLDER}</CardTitle>
          <CardDescription>Войдите чтобы управлять сообществом</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(e) => {
                  setEmail(e.target.value);
                  setRequiresTotp(false);
                }}
                disabled={requiresTotp}
                required
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Пароль</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => {
                  setPassword(e.target.value);
                  setRequiresTotp(false);
                }}
                disabled={requiresTotp}
                required
              />
            </div>
            {requiresTotp && (
              <div className="space-y-1.5">
                <Label htmlFor="totp">Код 2FA</Label>
                <Input
                  id="totp"
                  autoComplete="one-time-code"
                  value={totpCode}
                  onChange={(e) => setTotpCode(e.target.value)}
                  required
                  autoFocus
                />
              </div>
            )}
            {error && <div className="text-sm text-destructive">{error}</div>}
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? "Вход…" : "Войти"}
            </Button>
            {requiresTotp && (
              <Button
                type="button"
                variant="ghost"
                className="w-full"
                onClick={() => {
                  setRequiresTotp(false);
                  setTotpCode("");
                  setError(null);
                }}
              >
                Изменить email или пароль
              </Button>
            )}
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
