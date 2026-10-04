import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RendaFutura | Perfil do investidor",
  description: "Questionário de perfil do investidor para o planejamento da aposentadoria.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="pt-BR">
      <body className="antialiased">{children}</body>
    </html>
  );
}
