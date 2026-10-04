import type { Metadata, Viewport } from "next";
import { Big_Shoulders, Big_Shoulders_Stencil, IBM_Plex_Mono, IBM_Plex_Sans_Condensed } from "next/font/google";
import "./globals.css";

const display = Big_Shoulders({ subsets: ["latin"], variable: "--font-display" });
const stencil = Big_Shoulders_Stencil({ subsets: ["latin"], weight: ["800"], variable: "--font-stencil" });
const sans = IBM_Plex_Sans_Condensed({ subsets: ["latin"], weight: ["400", "500", "600"], variable: "--font-sans" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], weight: ["400", "500", "600"], variable: "--font-mono" });

export const metadata: Metadata = {
  title: "GridRoute Dispatch",
  description: "Live H3 ride-dispatch console: supply, demand and every match, with zero double-bookings.",
};

export const viewport: Viewport = { themeColor: "#0b0f14", colorScheme: "dark" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${display.variable} ${stencil.variable} ${sans.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
