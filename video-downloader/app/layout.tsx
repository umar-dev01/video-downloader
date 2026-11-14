import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Video Downloader",
  description: "Download videos from YouTube, Instagram, TikTok and more",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
