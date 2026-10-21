import { useState, type FormEvent } from 'react';
import { api, json, message, type Session } from './api';

export default function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [register, setRegister] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('');
    const fields = new FormData(event.currentTarget);
    try {
      const session = await api<Session>(`/auth/${register ? 'register' : 'login'}`, json('POST', {
        email: fields.get('email'), password: fields.get('password'),
        ...(register ? { display_name: fields.get('name') } : {}),
      }));
      onLogin(session);
    } catch (error) { setError(message(error)); }
    finally { setBusy(false); }
  }
  return <main className="login-page">
    <section className="login-story"><span className="brand-mark" aria-hidden="true">a</span><p className="eyebrow">Arclight · 创作手帐</p>
      <h1>把脑海里的小宇宙，<br/>一页页拍成故事。</h1><p>写下灵感，和编剧、导演一起打磨剧本与分镜。<br/>每一次修改都留有版本，每个重要决定由你确认。</p>
      <div className="sample-papers" aria-hidden="true"><span>一段灵感</span><span>一本剧本</span><span>一格分镜</span></div>
      <p className="honest-note">目前开放文本创作与审核；图片生成、连续性审查和成片功能仍在开发。</p>
    </section>
    <section className="login-card"><span className="tape" aria-hidden="true"/><h2>{register ? '开启一本创作手帐' : '欢迎回来，小创作家'}</h2>
      <p>你的故事，值得被认真记录。</p>
      <form onSubmit={submit}>
        {register && <label>怎么称呼你<input name="name" autoComplete="nickname" maxLength={80} required placeholder="给自己取个创作昵称"/></label>}
        <label>邮箱<input name="email" type="email" autoComplete="email" maxLength={254} required placeholder="you@example.com"/></label>
        <label>密码<input name="password" type="password" autoComplete={register ? 'new-password' : 'current-password'} minLength={10} maxLength={128} required placeholder="至少 10 个字符"/></label>
        {error && <p role="alert" className="error">{error}</p>}
        <button className="primary wide" disabled={busy}>{busy ? '正在打开手帐…' : register ? '注册并开始创作' : '登录创作手帐'}</button>
      </form>
      <button className="text-button wide" disabled={busy} onClick={() => { setRegister(!register); setError(''); }}>{register ? '已有账号？去登录' : '第一次来？创建账号'}</button>
    </section>
  </main>;
}
