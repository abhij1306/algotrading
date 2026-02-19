import type { Metadata } from "next";
import { IBM_Plex_Sans, DM_Mono } from "next/font/google";
import "./globals.css";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { AppShell } from "@/components/layout";

// IBM Plex Sans for UI text - professional, neutral grotesque
const ibmPlexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  variable: "--font-sans",
  weight: ["400", "500", "600", "700"],
  display: "swap",
  adjustFontFallback: true,
});

// DM Mono for financial data - pure oval zero, no dot/slash, designed for data
const dmMono = DM_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  weight: ["400", "500"],
  display: "swap",
  adjustFontFallback: true,
});

export const metadata: Metadata = {
  title: "SmartTrader - AI-Powered Trading Platform",
  description: "Professional algorithmic trading platform with backtesting, screening, and AI analysis",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        <link
          href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap"
          rel="stylesheet"
        />
      </head>
      <body
        className={`${ibmPlexSans.variable} ${dmMono.variable} font-sans antialiased h-screen`}
      >
        <ErrorBoundary>
          <AppShell>
            {children}
          </AppShell>
        </ErrorBoundary>
      </body>
    </html>
  );
}
