import { PagePanel } from "@/components/ui-kit";
import { FieldGroup } from "@/components/ui/field";
import { FormLabel, Notice } from "@/components/ui-kit";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useState, type FormEvent } from "react";
import { api, json, message, type Session } from "./api";

export default function Login({
  onLogin,
}: {
  onLogin: (session: Session) => void;
}) {
  const [register, setRegister] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const fields = new FormData(event.currentTarget);
    try {
      const session = await api<Session>(
        `/auth/${register ? "register" : "login"}`,
        json("POST", {
          email: fields.get("email"),
          password: fields.get("password"),
          ...(register ? { display_name: fields.get("name") } : {}),
        }),
      );
      onLogin(session);
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-page mx-auto grid min-h-dvh max-w-6xl items-center gap-12 px-5 py-10 lg:grid-cols-[1.2fr_1fr] lg:gap-20 lg:px-10">
      <section className="login-story flex flex-col gap-6 [&>h1]:text-3xl [&>h1]:leading-relaxed sm:[&>h1]:text-4xl [&>p]:text-sm [&>p]:leading-loose [&>p]:text-muted-foreground">
        <span
          className="brand-mark flex size-10 shrink-0 items-center justify-center rounded-2xl bg-primary font-serif text-3xl italic text-primary-foreground shadow-sm"
          aria-hidden="true"
        >
          a
        </span>
        <p className="eyebrow text-[11px] font-semibold tracking-widest text-primary">
          Arclight · 创作手帐
        </p>
        <h1>
          把脑海里的小宇宙，
          <br />
          一页页拍成故事。
        </h1>
        <p>
          写下灵感，和编剧、导演一起打磨剧本与分镜。
          <br />
          每一次修改都留有版本，每个重要决定由你确认。
        </p>
        <div
          className="sample-papers flex flex-wrap gap-3 py-5 [&>span]:rounded-lg [&>span]:border [&>span]:bg-accent [&>span]:px-5 [&>span]:py-8 [&>span]:text-sm [&>span]:text-primary [&>span:nth-child(2)]:-rotate-6 [&>span:nth-child(2)]:bg-secondary [&>span:nth-child(3)]:rotate-3"
          aria-hidden="true"
        >
          <span>一段灵感</span>
          <span>一本剧本</span>
          <span>一格分镜</span>
        </div>
        <p className="honest-note max-w-sm rounded-lg border border-dashed p-3">
          目前开放文本创作与审核；图片生成、连续性审查和成片功能仍在开发。
        </p>
      </section>
      <PagePanel
        className="login-card relative shadow-lg shadow-primary/5"
        title={register ? "开启一本创作手帐" : "欢迎回来，小创作家"}
        description="你的故事，值得被认真记录。"
      >
        <form onSubmit={submit} className="flex flex-col gap-4">
          <FieldGroup>
            {register && (
              <FormLabel>
                怎么称呼你
                <Input
                  name="name"
                  autoComplete="nickname"
                  maxLength={80}
                  required
                  placeholder="给自己取个创作昵称"
                />
              </FormLabel>
            )}
            <FormLabel>
              邮箱
              <Input
                name="email"
                type="email"
                autoComplete="email"
                maxLength={254}
                required
                placeholder="you@example.com"
              />
            </FormLabel>
            <FormLabel>
              密码
              <Input
                name="password"
                type="password"
                autoComplete={register ? "new-password" : "current-password"}
                minLength={10}
                maxLength={128}
                required
                placeholder="至少 10 个字符"
              />
            </FormLabel>
            {error && <Notice tone="error">{error}</Notice>}
            <Button size="lg" disabled={busy}>
              {busy
                ? "正在打开手帐…"
                : register
                  ? "注册并开始创作"
                  : "登录创作手帐"}
            </Button>
          </FieldGroup>
        </form>
        <Button
          variant="ghost"
          size="lg"
          disabled={busy}
          onClick={() => {
            setRegister(!register);
            setError("");
          }}
        >
          {register ? "已有账号？去登录" : "第一次来？创建账号"}
        </Button>
      </PagePanel>
    </main>
  );
}
