import {
  ArrowDown,
  ArrowUpRight,
  Check,
  Clapperboard,
  Feather,
  FileText,
  X,
} from "lucide-react";
import { Brand, StoryDrawing } from "@/components/brand-art";
import { FieldGroup } from "@/components/ui/field";
import { FormLabel, Notice } from "@/components/ui-kit";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useState, type FormEvent } from "react";
import { api, json, message, type Session } from "./api";
import Dialog from "./Dialog";

export default function Login({
  onLogin,
}: {
  onLogin: (session: Session) => void;
}) {
  const [authOpen, setAuthOpen] = useState(false);
  const [register, setRegister] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  function openAuth(createAccount: boolean) {
    setRegister(createAccount);
    setError("");
    setAuthOpen(true);
  }
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
    <div className="login-page product-page">
      <a href="#main-content" className="skip-link">
        跳至主要内容
      </a>
      <header className="product-header">
        <a href="#how-it-works" className="header-about">
          让故事发生
        </a>
        <a
          href="#main-content"
          className="product-brand"
          aria-label="Arclight 首页"
        >
          <Brand />
        </a>
        <Button
          variant="ghost"
          className="header-login"
          onClick={() => openAuth(false)}
        >
          登录 <ArrowUpRight data-icon="inline-end" />
        </Button>
      </header>
      <main id="main-content">
        <section className="story-hero" aria-labelledby="hero-title">
          <div className="story-collage" aria-hidden="true">
            <div className="collage-writer">
              <StoryDrawing />
              <span className="handwritten">一切，从一个念头开始。</span>
            </div>
            <div className="story-scrap scrap-idea">
              <span className="scrap-label">
                <Feather /> 灵感便签
              </span>
              <p>
                如果月亮，
                <br />
                开了一家深夜邮局？
              </p>
              <span className="scrap-caption">记下这个小小的「如果」</span>
            </div>
            <div className="story-scrap scrap-ready">
              <span>好故事，正在发生</span>
              <span className="scrap-check">
                <Check />
              </span>
            </div>
            <div className="story-scrap scrap-script">
              <span className="scrap-label">
                <FileText /> 故事的第一幕
              </span>
              <p>
                夜色很轻。
                <br />
                一封信，落在了窗边。
              </p>
              <div className="scrap-lines">
                <i />
                <i />
              </div>
              <span className="scrap-caption">《月亮邮局》 · 示例剧本</span>
            </div>
            <div className="story-scrap scrap-scene">
              <StoryDrawing variant="landscape" />
              <div>
                <span className="scrap-label">镜头 01 · 远景</span>
                <p>沿着月光，去远方。</p>
              </div>
            </div>
            <div className="story-scrap scrap-board">
              <Clapperboard />
              <p>
                给想象力
                <br />
                一个新视角。
              </p>
              <span className="scrap-caption">故事，逐格展开</span>
            </div>
            <div className="story-scrap scrap-note">
              <span className="scrap-check">
                <Check />
              </span>
              <span>每一稿灵感，都好好收藏。</span>
            </div>
            <StoryDrawing variant="plane" className="collage-plane" />
            <StoryDrawing variant="book" className="collage-book" />
            <span className="collage-asterisk">✳</span>
          </div>
          <div className="hero-copy">
            <p className="hero-intro">
              <span /> 和 Arclight 一起，把想象写成故事
            </p>
            <h1 id="hero-title">
              让一个小灵感，
              <br />
              <em>长成好故事。</em>
            </h1>
            <p className="hero-description">
              从脑海中的一闪而过，到跃然纸上的每一幕。
              <br />和 AI 一起打磨剧本与分镜，让创作自然发生。
            </p>
            <Button
              size="lg"
              className="hero-cta"
              onClick={() => openAuth(true)}
            >
              开始我的故事 <ArrowUpRight data-icon="inline-end" />
            </Button>
            <button className="hero-signin" onClick={() => openAuth(false)}>
              已经有账号？继续创作
            </button>
          </div>
          <a href="#how-it-works" className="hero-scroll">
            一个故事，可以这样开始 <ArrowDown aria-hidden="true" />
          </a>
          <span className="collage-disclaimer">故事卡片为创作示意</span>
        </section>
        <section
          id="how-it-works"
          className="story-journey"
          aria-labelledby="journey-title"
        >
          <div className="journey-heading">
            <p>不必一开始，就想好所有。</p>
            <h2 id="journey-title">
              你带来灵感，
              <br />
              我们陪它走远一点。
            </h2>
          </div>
          <ol className="journey-steps">
            <li>
              <span className="journey-icon">
                <Feather aria-hidden="true" />
              </span>
              <span className="journey-number">01 / 留住灵感</span>
              <h3>从一句「如果」开始</h3>
              <p>一个人物、一句对白、一个奇怪的梦。写下来，就是故事的起点。</p>
            </li>
            <li>
              <span className="journey-icon">
                <FileText aria-hidden="true" />
              </span>
              <span className="journey-number">02 / 慢慢成章</span>
              <h3>让故事有自己的模样</h3>
              <p>
                自己落笔，或和 AI
                一起打磨剧本。每次修改都留下版本，好的想法不会丢。
              </p>
            </li>
            <li>
              <span className="journey-icon">
                <Clapperboard aria-hidden="true" />
              </span>
              <span className="journey-number">03 / 逐格展开</span>
              <h3>把文字，想象成画面</h3>
              <p>
                从确认的剧本出发，整理每个镜头的描述与节奏。每个重要决定，始终由你确认。
              </p>
            </li>
          </ol>
        </section>
        <section
          className="story-invitation"
          aria-labelledby="invitation-title"
        >
          <StoryDrawing variant="book" />
          <div>
            <p>留一点时间，给想象力。</p>
            <h2 id="invitation-title">下一个故事，从你开始。</h2>
            <Button size="lg" onClick={() => openAuth(true)}>
              开始创作 <ArrowUpRight data-icon="inline-end" />
            </Button>
          </div>
          <p className="product-availability">
            现在，你可以创作剧本与文字分镜。
            <br />
            图片、配音与成片，留给接下来的旅程。
          </p>
        </section>
      </main>
      <footer className="product-footer">
        <Brand />
        <p>认真对待每一个天马行空。</p>
        <span>为故事而生</span>
      </footer>
      {authOpen && (
        <Dialog
          labelledBy="auth-title"
          busy={busy}
          onDismiss={() => setAuthOpen(false)}
          className="auth-dialog sm:max-w-md"
        >
          <div className="auth-heading">
            <Brand />
            <Button
              variant="ghost"
              size="icon"
              aria-label="关闭登录窗口"
              disabled={busy}
              onClick={() => setAuthOpen(false)}
            >
              <X />
            </Button>
          </div>
          <div>
            <h2 id="auth-title">
              {register ? "故事，就从这里开始。" : "欢迎回来，继续你的故事。"}
            </h2>
            <p className="auth-description">
              {register
                ? "创建账号，给你的灵感一个家。"
                : "那些未完待续的灵感，都在等你。"}
            </p>
          </div>
          <form onSubmit={submit} aria-busy={busy}>
            <FieldGroup>
              {register && (
                <FormLabel>
                  怎么称呼你
                  <Input
                    name="name"
                    autoComplete="nickname"
                    maxLength={80}
                    required
                    placeholder="你的创作昵称"
                    disabled={busy}
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
                  disabled={busy}
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
                  disabled={busy}
                />
              </FormLabel>
              {error && <Notice tone="error">{error}</Notice>}
              <Button size="lg" disabled={busy} type="submit">
                {busy && <Spinner aria-hidden="true" />}
                {busy
                  ? "正在打开你的故事…"
                  : register
                    ? "注册并开始创作"
                    : "登录并继续创作"}
              </Button>
            </FieldGroup>
          </form>
          <Button
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setRegister(!register);
              setError("");
            }}
          >
            {register ? "已有账号？去登录" : "第一次来？创建账号"}
          </Button>
        </Dialog>
      )}
    </div>
  );
}
