import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import "@xyflow/react/dist/style.css";
import "@/styles/globals.css";
import "@/styles/studio.css";

// A fresh CSP nonce is supplied for each document request by proxy.ts.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Arclight · 让灵感长成好故事",
  description:
    "和 Arclight 一起，让一个小灵感长成好故事。创作剧本、打磨文字分镜，认真记录每一次天马行空。",
};

export const viewport: Viewport = { themeColor: "#fdfdf9" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <div id="root">{children}</div>
      </body>
    </html>
  );
}
