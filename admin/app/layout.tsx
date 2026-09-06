import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/providers";
import { PANEL_NAME_PLACEHOLDER } from "@/lib/branding";

export const metadata: Metadata = {
  title: PANEL_NAME_PLACEHOLDER,
  description: "Админ-панель закрытого сообщества",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru" suppressHydrationWarning>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
