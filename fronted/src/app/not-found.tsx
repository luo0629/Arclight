import Link from "next/link";

export default function NotFoundPage() {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center gap-4 p-6 text-center">
      <h1>没有找到这个页面</h1>
      <p>回到首页，继续你的故事。</p>
      <Link className="text-primary underline" href="/">
        返回首页
      </Link>
    </main>
  );
}
