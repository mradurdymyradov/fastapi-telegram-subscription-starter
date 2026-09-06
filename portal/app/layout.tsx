import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Гипно-Коучинг",
  description: "Закрытое сообщество Павла Дмитриева — архив 3000+ уроков Гипно-Коучинга",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru" suppressHydrationWarning>
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
