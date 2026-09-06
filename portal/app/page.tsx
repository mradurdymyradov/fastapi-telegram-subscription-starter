import { Send, Lock, PlayCircle } from "lucide-react";

import { botDeepLink } from "@/lib/backend";

export const dynamic = "force-dynamic";

const NOTICES: Record<string, { tone: "info" | "warn"; text: string }> = {
  login: { tone: "info", text: "Войдите через Telegram, чтобы открыть архив." },
  link: { tone: "warn", text: "Ссылка недействительна или уже использована. Запросите новую в боте." },
  inactive: { tone: "warn", text: "Подписка неактивна. Продлите её в боте, чтобы вернуть доступ." },
  server: { tone: "warn", text: "Не удалось войти. Попробуйте ещё раз чуть позже." },
  loggedout: { tone: "info", text: "Вы вышли из архива. До встречи!" },
};

export default function Landing({
  searchParams,
}: {
  searchParams: { error?: string };
}) {
  const notice = searchParams.error ? NOTICES[searchParams.error] : undefined;

  return (
    <main className="relative min-h-screen overflow-hidden">
      {/* Ambient gradient backdrop */}
      <div className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(60%_50%_at_50%_0%,hsl(var(--accent))_0%,transparent_70%)]" />

      <div className="container flex min-h-screen max-w-2xl flex-col items-center justify-center py-16 text-center">
        <div className="mb-6 grid h-16 w-16 place-items-center rounded-2xl bg-primary text-primary-foreground shadow-lg shadow-primary/20">
          <PlayCircle className="h-8 w-8" />
        </div>

        <h1 className="text-balance text-3xl font-semibold tracking-tight sm:text-4xl">
          Гипно-Коучинг
        </h1>
        <p className="mt-3 max-w-md text-balance text-muted-foreground">
          Закрытое сообщество Павла Дмитриева. Архив 3000+ уроков Гипно-Коучинга —
          доступ для активных участников по входу через Telegram.
        </p>

        {notice && (
          <div
            className={
              "mt-6 w-full rounded-lg border px-4 py-3 text-sm " +
              (notice.tone === "warn"
                ? "border-amber-300/60 bg-amber-50 text-amber-800"
                : "border-border bg-accent text-accent-foreground")
            }
          >
            {notice.text}
          </div>
        )}

        <a
          href={botDeepLink()}
          className="mt-8 inline-flex items-center gap-2 rounded-xl bg-primary px-6 py-3 text-sm font-medium text-primary-foreground shadow-sm transition hover:opacity-90"
        >
          <Send className="h-4 w-4" />
          Открыть архив через Telegram
        </a>

        <div className="mt-10 flex items-center gap-2 text-xs text-muted-foreground">
          <Lock className="h-3.5 w-3.5" />
          Вход по одноразовой ссылке из бота. Доступ привязан к вашей подписке.
        </div>
      </div>
    </main>
  );
}
