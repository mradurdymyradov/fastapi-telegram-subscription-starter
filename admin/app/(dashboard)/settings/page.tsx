"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileSpreadsheet, Play, RefreshCw, ShieldCheck, ShieldOff } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { QueryError } from "@/components/query-error";
import { api, ApiError } from "@/lib/api";

interface Plan {
  id: number;
  code: string;
  name: string;
  description: string | null;
  price_rub: number;
  price_usd: number;
  duration_days: number;
  is_active: boolean;
  sort_order: number;
}

interface GoogleSheetsConfig {
  spreadsheet_id_configured: boolean;
  service_account_configured: boolean;
  // GK-479: "Configured" only ever meant "the variable is not empty", and it
  // stayed green for ten weeks while the file it named did not exist. This is
  // what happens when the credential is actually loaded.
  service_account_error?: string | null;
  manual_notes_sheet: string;
  sheets: string[];
}

function sheetsErrorDetail(error: ApiError): string {
  const body = error.body;
  const detail =
    body && typeof body === "object" && "detail" in body
      ? (body as { detail?: unknown }).detail
      : undefined;
  if (typeof detail === "string" && detail.trim()) {
    return `${error.status} — ${detail}`;
  }
  return `${error.status} — ${body ? JSON.stringify(body) : error.message || "Google Sheets export failed."}`;
}

interface GoogleSheetsExportResult {
  spreadsheet_id: string;
  dry_run: boolean;
  manual_notes_sheet: string;
  manual_notes_initialized: boolean;
  rows: number;
  updated: number;
  appended: number;
  sheets: Array<{
    sheet: string;
    rows: number;
    updated: number;
    appended: number;
    duplicate_existing_ids: string[];
  }>;
  operations: string[];
}

interface TotpStatus {
  enabled: boolean;
  recovery_codes_remaining: number;
}

interface TotpEnrollOut {
  secret: string;
  otpauth_uri: string;
}

interface TotpConfirmOut {
  enabled: boolean;
  recovery_codes: string[];
}

function ChangePasswordCard() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [status, setStatus] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const mut = useMutation({
    mutationFn: () =>
      api("/auth/change-password", {
        method: "POST",
        body: JSON.stringify({ current_password: current, new_password: next }),
      }),
    onSuccess: () => {
      setStatus({ kind: "ok", text: "Пароль обновлён. Следующий вход — с новым паролем." });
      setCurrent("");
      setNext("");
      setConfirm("");
    },
    onError: (err) => {
      const msg =
        err instanceof ApiError && err.status === 401
          ? "Текущий пароль введён неверно."
          : err instanceof ApiError && err.status === 400
            ? "Новый пароль не подходит (минимум 12 символов и должен отличаться от текущего)."
            : "Не удалось обновить пароль.";
      setStatus({ kind: "err", text: msg });
    },
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setStatus(null);
    if (next.length < 12) {
      setStatus({ kind: "err", text: "Новый пароль должен быть минимум 12 символов." });
      return;
    }
    if (next !== confirm) {
      setStatus({ kind: "err", text: "Новый пароль и подтверждение не совпадают." });
      return;
    }
    mut.mutate();
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>🔑 Пароль администратора</CardTitle>
        <CardDescription>
          Меняйте регулярно. Текущая сессия останется активной — выйдите и войдите снова после смены.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="space-y-3 max-w-md">
          <div className="space-y-1.5">
            <Label>Текущий пароль</Label>
            <Input
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label>Новый пароль (мин. 12 символов)</Label>
            <Input
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              required
              minLength={12}
            />
          </div>
          <div className="space-y-1.5">
            <Label>Подтверждение</Label>
            <Input
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
              minLength={12}
            />
          </div>
          {status && (
            <div className={status.kind === "ok" ? "text-sm text-emerald-600" : "text-sm text-destructive"}>
              {status.text}
            </div>
          )}
          <Button type="submit" disabled={mut.isPending}>
            {mut.isPending ? "Сохранение…" : "Сменить пароль"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function TotpCard() {
  const qc = useQueryClient();
  const status = useQuery<TotpStatus>({
    queryKey: ["totp-status"],
    queryFn: () => api<TotpStatus>("/auth/totp/status"),
  });
  const [currentPassword, setCurrentPassword] = useState("");
  const [confirmCode, setConfirmCode] = useState("");
  const [disablePassword, setDisablePassword] = useState("");
  const [disableCode, setDisableCode] = useState("");
  const [setup, setSetup] = useState<TotpEnrollOut | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const apiMessage = (err: unknown, fallback: string) => {
    if (err instanceof ApiError && err.status === 401) return "Неверный пароль или код.";
    if (err instanceof ApiError && err.status === 409) return "2FA уже включена.";
    return fallback;
  };

  const enroll = useMutation({
    mutationFn: () =>
      api<TotpEnrollOut>("/auth/totp/enroll", {
        method: "POST",
        body: JSON.stringify({ current_password: currentPassword }),
      }),
    onSuccess: (data) => {
      setSetup(data);
      setConfirmCode("");
      setRecoveryCodes([]);
      setMessage(null);
    },
    onError: (err) => setMessage({ kind: "err", text: apiMessage(err, "Не удалось начать настройку 2FA.") }),
  });

  const confirm = useMutation({
    mutationFn: () =>
      api<TotpConfirmOut>("/auth/totp/confirm", {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword,
          secret: setup?.secret,
          code: confirmCode,
        }),
      }),
    onSuccess: (data) => {
      setRecoveryCodes(data.recovery_codes);
      setSetup(null);
      setCurrentPassword("");
      setConfirmCode("");
      setMessage({ kind: "ok", text: "2FA включена. Сохраните коды восстановления сейчас." });
      qc.invalidateQueries({ queryKey: ["totp-status"] });
    },
    onError: (err) => setMessage({ kind: "err", text: apiMessage(err, "Не удалось подтвердить настройку 2FA.") }),
  });

  const disable = useMutation({
    mutationFn: () =>
      api("/auth/totp/disable", {
        method: "POST",
        body: JSON.stringify({ current_password: disablePassword, code: disableCode }),
      }),
    onSuccess: () => {
      setDisablePassword("");
      setDisableCode("");
      setRecoveryCodes([]);
      setMessage({ kind: "ok", text: "2FA отключена." });
      qc.invalidateQueries({ queryKey: ["totp-status"] });
    },
    onError: (err) => setMessage({ kind: "err", text: apiMessage(err, "Не удалось отключить 2FA.") }),
  });

  const enabled = Boolean(status.data?.enabled);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck className="h-5 w-5" />
          Двухфакторная аутентификация
        </CardTitle>
        <CardDescription>
          {enabled
            ? `Осталось кодов восстановления: ${status.data?.recovery_codes_remaining ?? 0}.`
            : "Подключение приложения-аутентификатора к этой учётной записи."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* GK-468: `enabled` defaults to false, so a failed status fetch shows
            the enrollment form as if 2FA were definitely off — on the account
            security card of a panel with four owner admins. */}
        {status.isError && (
          <QueryError
            error={status.error}
            onRetry={() => status.refetch()}
            retrying={status.isFetching}
            title="Состояние 2FA не загрузилось"
            description="Форма ниже показана по умолчанию — это не подтверждение того, что 2FA выключена."
          />
        )}
        {!enabled && (
          <form
            className="space-y-3 max-w-xl"
            onSubmit={(e) => {
              e.preventDefault();
              if (setup) confirm.mutate();
              else enroll.mutate();
            }}
          >
            <div className="space-y-1.5">
              <Label>Текущий пароль</Label>
              <Input
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                required
              />
            </div>
            {setup && (
              <div className="space-y-3">
                <div className="space-y-1.5">
                  <Label>Секретный ключ TOTP</Label>
                  <Input readOnly value={setup.secret} />
                </div>
                <div className="space-y-1.5">
                  <Label>Ссылка для аутентификатора</Label>
                  <Textarea readOnly rows={3} value={setup.otpauth_uri} />
                </div>
                <div className="space-y-1.5">
                  <Label>Код из аутентификатора</Label>
                  <Input
                    autoComplete="one-time-code"
                    value={confirmCode}
                    onChange={(e) => setConfirmCode(e.target.value)}
                    required
                  />
                </div>
              </div>
            )}
            <Button type="submit" disabled={enroll.isPending || confirm.isPending}>
              <ShieldCheck className="h-4 w-4" />
              {setup ? "Включить 2FA" : "Начать настройку 2FA"}
            </Button>
          </form>
        )}

        {enabled && (
          <form
            className="space-y-3 max-w-xl"
            onSubmit={(e) => {
              e.preventDefault();
              disable.mutate();
            }}
          >
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>Текущий пароль</Label>
                <Input
                  type="password"
                  autoComplete="current-password"
                  value={disablePassword}
                  onChange={(e) => setDisablePassword(e.target.value)}
                  required
                />
              </div>
              <div className="space-y-1.5">
                <Label>Код 2FA или код восстановления</Label>
                <Input
                  autoComplete="one-time-code"
                  value={disableCode}
                  onChange={(e) => setDisableCode(e.target.value)}
                  required
                />
              </div>
            </div>
            <Button type="submit" variant="outline" disabled={disable.isPending}>
              <ShieldOff className="h-4 w-4" />
              Отключить 2FA
            </Button>
          </form>
        )}

        {message && (
          <div className={message.kind === "ok" ? "text-sm text-emerald-600" : "text-sm text-destructive"}>
            {message.text}
          </div>
        )}

        {recoveryCodes.length > 0 && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 max-w-xl">
            {recoveryCodes.map((code) => (
              <div key={code} className="rounded-md border bg-muted/30 px-3 py-2 text-center font-mono text-sm">
                {code}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}


export default function SettingsPage() {
  const qc = useQueryClient();
  const plans = useQuery<Plan[]>({ queryKey: ["plans"], queryFn: () => api<Plan[]>("/plans") });
  const sheetsConfig = useQuery<GoogleSheetsConfig>({
    queryKey: ["google-sheets-config"],
    queryFn: () => api<GoogleSheetsConfig>("/crm-export/google-sheets/config"),
  });
  const [edits, setEdits] = useState<Record<number, Partial<Plan>>>({});
  const [sheetsResult, setSheetsResult] = useState<GoogleSheetsExportResult | null>(null);

  useEffect(() => {
    if (plans.data && Object.keys(edits).length === 0) {
      const init: Record<number, Partial<Plan>> = {};
      plans.data.forEach((p) => (init[p.id] = { ...p }));
      setEdits(init);
    }
  }, [plans.data]);

  const update = useMutation({
    mutationFn: (p: Plan) =>
      api(`/plans/${p.id}`, {
        method: "PUT",
        body: JSON.stringify({
          code: p.code,
          name: p.name,
          description: p.description,
          price_rub: Number(p.price_rub),
          price_usd: Number(p.price_usd),
          duration_days: Number(p.duration_days),
          is_active: p.is_active,
          sort_order: Number(p.sort_order),
        }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["plans"] }),
  });

  const runSheetsExport = useMutation({
    mutationFn: (dryRun: boolean) =>
      api<GoogleSheetsExportResult>("/crm-export/google-sheets", {
        method: "POST",
        body: JSON.stringify({ dry_run: dryRun }),
      }),
    onSuccess: (result) => setSheetsResult(result),
  });

  const sheetsReady =
    Boolean(sheetsConfig.data?.spreadsheet_id_configured) &&
    Boolean(sheetsConfig.data?.service_account_configured);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">⚙️ Настройки</h1>
        <p className="text-sm text-muted-foreground">Тарифы и базовые параметры сообщества</p>
      </div>

      <ChangePasswordCard />
      <TotpCard />

      <Card>
        <CardHeader>
          <CardTitle>Тарифы</CardTitle>
          <CardDescription>Редактируются на лету, юзеры увидят изменения сразу</CardDescription>
        </CardHeader>
        <CardContent>
          {plans.isError && (
            <QueryError
              error={plans.error}
              onRetry={() => plans.refetch()}
              retrying={plans.isFetching}
              title="Тарифы не загрузились"
              description="Пустая таблица ниже — это отсутствие ответа, а не отсутствие тарифов."
              className="mb-4"
            />
          )}
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Код</TableHead>
                <TableHead>Название</TableHead>
                <TableHead>₽</TableHead>
                <TableHead>$</TableHead>
                <TableHead>Дней</TableHead>
                <TableHead></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {plans.data?.map((p) => {
                const e = edits[p.id] ?? p;
                const set = (patch: Partial<Plan>) => setEdits((prev) => ({ ...prev, [p.id]: { ...e, ...patch } }));
                return (
                  <TableRow key={p.id}>
                    <TableCell className="font-mono text-xs">{e.code}</TableCell>
                    <TableCell><Input value={String(e.name ?? "")} onChange={(ev) => set({ name: ev.target.value })} /></TableCell>
                    <TableCell><Input className="w-24" type="number" value={Number(e.price_rub ?? 0)} onChange={(ev) => set({ price_rub: Number(ev.target.value) })} /></TableCell>
                    <TableCell><Input className="w-20" type="number" value={Number(e.price_usd ?? 0)} onChange={(ev) => set({ price_usd: Number(ev.target.value) })} /></TableCell>
                    <TableCell><Input className="w-20" type="number" value={Number(e.duration_days ?? 0)} onChange={(ev) => set({ duration_days: Number(ev.target.value) })} /></TableCell>
                    <TableCell><Button size="sm" onClick={() => update.mutate(e as Plan)}>Сохранить</Button></TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Реквизиты ручных платежей</CardTitle>
          <CardDescription>Редактируются через переменные окружения (.env на VPS). Указано для справки.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>Zelle получатель</Label>
              <Input disabled value="ZELLE_RECIPIENT в .env" />
            </div>
            <div className="space-y-1.5">
              <Label>USDT TRC20 адрес</Label>
              <Input disabled value="USDT_TRC20_ADDRESS в .env" />
            </div>
            <div className="space-y-1.5">
              <Label>USDT ERC20 адрес</Label>
              <Input disabled value="USDT_ERC20_ADDRESS в .env" />
            </div>
            <div className="space-y-1.5">
              <Label>Бонус-дни рефереру</Label>
              <Input disabled value="REFERRAL_BONUS_DAYS в .env" />
            </div>
          </div>
          <Textarea
            disabled
            rows={3}
            value={`Все приватные ключи (BOT_TOKEN, STRIPE_*, OPENAI_API_KEY, JWT_SECRET) хранятся в /deploy/.env на VPS. Никогда не попадают в репозиторий.`}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FileSpreadsheet className="h-5 w-5" />
            Google Sheets CRM export
          </CardTitle>
          <CardDescription>
            Stable-id export for users, subscriptions, payments, referrals, payouts, and reconciliation.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* GK-468: both tiles below say "Missing" when the config simply
              didn't load, which sends someone off to fix a non-problem. */}
          {sheetsConfig.isError && (
            <QueryError
              error={sheetsConfig.error}
              onRetry={() => sheetsConfig.refetch()}
              retrying={sheetsConfig.isFetching}
              title="Настройки экспорта не загрузились"
              description="«Missing» ниже означает «неизвестно»."
            />
          )}
          <div className="grid gap-3 md:grid-cols-3">
            <div className="rounded-md border p-3">
              <div className="text-xs uppercase text-muted-foreground">Spreadsheet</div>
              <div className="mt-1 text-sm font-medium">
                {sheetsConfig.data?.spreadsheet_id_configured ? "Configured" : "Missing"}
              </div>
            </div>
            <div className="rounded-md border p-3">
              <div className="text-xs uppercase text-muted-foreground">Service account</div>
              <div className="mt-1 text-sm font-medium">
                {sheetsConfig.data?.service_account_configured
                  ? sheetsConfig.data?.service_account_error
                    ? "Broken"
                    : "Configured"
                  : "Missing"}
              </div>
              {sheetsConfig.data?.service_account_error && (
                <div className="mt-1 text-xs text-destructive">
                  {sheetsConfig.data.service_account_error}
                </div>
              )}
            </div>
            <div className="rounded-md border p-3">
              <div className="text-xs uppercase text-muted-foreground">Manual notes</div>
              <div className="mt-1 text-sm font-medium">
                {sheetsConfig.data?.manual_notes_sheet ?? "Manual Notes"}
              </div>
            </div>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={() => runSheetsExport.mutate(true)}
              disabled={runSheetsExport.isPending}
            >
              <RefreshCw className="h-4 w-4" />
              Dry run
            </Button>
            <Button
              onClick={() => runSheetsExport.mutate(false)}
              disabled={!sheetsReady || runSheetsExport.isPending}
            >
              <Play className="h-4 w-4" />
              Export now
            </Button>
          </div>

          {/* GK-479: this used to print JSON.stringify(body), so the whole of
              what Grant saw was {"detail":"Internal Server Error"}. The backend
              now names the failure; show the name, not the envelope. */}
          {runSheetsExport.error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
              {runSheetsExport.error instanceof ApiError
                ? sheetsErrorDetail(runSheetsExport.error)
                : "Google Sheets export failed."}
            </div>
          )}

          {sheetsResult && (
            <div className="space-y-3">
              <div className="rounded-md border bg-muted/30 p-3 text-sm">
                {sheetsResult.dry_run ? "Dry run" : "Export"} complete: {sheetsResult.rows} rows,{" "}
                {sheetsResult.updated} updated, {sheetsResult.appended} appended.
              </div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Sheet</TableHead>
                    <TableHead>Rows</TableHead>
                    <TableHead>Updated</TableHead>
                    <TableHead>Appended</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sheetsResult.sheets.map((sheet) => (
                    <TableRow key={sheet.sheet}>
                      <TableCell>{sheet.sheet}</TableCell>
                      <TableCell>{sheet.rows}</TableCell>
                      <TableCell>{sheet.updated}</TableCell>
                      <TableCell>{sheet.appended}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Флаги запуска</CardTitle>
          <CardDescription>
            Поверхности, скрытые по умолчанию для production-запуска. Управляются через .env, не из UI.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Textarea
            disabled
            rows={6}
            value={`ENABLE_ZELLE=false                  — Zelle в боте скрыт, существующие платежи остаются.
ENABLE_WEBHOOK_INTEGRATIONS=false  — AmoCRM/Make/Zapier выключены; новые подключения и тестовые отправки 403.
ENABLE_AI_SUPPORT=false            — поддержка отвечает только из локального mock-FAQ, без OpenAI/Anthropic.

Включается по согласованию с владельцем продукта после live-валидации соответствующей интеграции.`}
          />
        </CardContent>
      </Card>
    </div>
  );
}
