import Link from "next/link";
import { PlayCircle, User, LogOut } from "lucide-react";

// Server component — no client state needed. Logout is a plain link to the
// route handler that revokes the session and clears the cookie.
export function PortalHeader({ active }: { active?: "archive" | "me" }) {
  return (
    <header className="sticky top-0 z-10 border-b bg-background/80 backdrop-blur">
      <div className="container flex h-14 max-w-5xl items-center justify-between gap-4">
        <Link href="/archive" className="flex items-center gap-2 font-semibold">
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-primary text-primary-foreground">
            <PlayCircle className="h-4 w-4" />
          </span>
          <span className="hidden sm:inline">Архив сообщества</span>
        </Link>
        <nav className="flex items-center gap-1 text-sm">
          <Link
            href="/archive"
            className={
              "rounded-md px-3 py-1.5 transition hover:bg-accent " +
              (active === "archive" ? "font-medium text-primary" : "text-muted-foreground")
            }
          >
            Видео
          </Link>
          <Link
            href="/me"
            className={
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 transition hover:bg-accent " +
              (active === "me" ? "font-medium text-primary" : "text-muted-foreground")
            }
          >
            <User className="h-3.5 w-3.5" />
            Профиль
          </Link>
          <a
            href="/auth/logout"
            className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-muted-foreground transition hover:bg-accent hover:text-foreground"
          >
            <LogOut className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Выйти</span>
          </a>
        </nav>
      </div>
    </header>
  );
}
