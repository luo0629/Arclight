"use client";

import { Button } from "@/components/ui/button";

export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center gap-4 p-6 text-center">
      <h1>页面暂时没有打开</h1>
      <p role="alert">请重试；本机保存的创作草稿会保留。</p>
      <Button onClick={reset}>重新打开</Button>
    </main>
  );
}
