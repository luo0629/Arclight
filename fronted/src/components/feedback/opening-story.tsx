import { Brand } from "@/components/brand-art";
import { Skeleton } from "@/components/ui/skeleton";

export default function OpeningStory() {
  return (
    <main
      className="boot-screen flex min-h-dvh flex-col items-center justify-center gap-4 text-sm text-muted-foreground"
      role="status"
    >
      <Brand />
      <p>正在打开你的故事…</p>
      <div className="flex w-52 flex-col items-center gap-3" aria-hidden="true">
        <Skeleton className="h-2 w-full" />
        <Skeleton className="h-2 w-32" />
      </div>
    </main>
  );
}
