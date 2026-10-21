import { useEffect, useState, type FormEvent } from 'react';
import { api, json, message, type Member } from './api';

export default function Members({ projectId, owner, writable }: { projectId: string; owner: boolean; writable: boolean }) {
  const [members, setMembers] = useState<Member[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const url = `/projects/${projectId}/members`;
  async function reload() { setMembers(await api<Member[]>(url)); setLoaded(true); }
  useEffect(() => { let live = true; api<Member[]>(url).then(rows => { if (live) { setMembers(rows); setLoaded(true); } }).catch(error => { if (live) setError(message(error)); }); return () => { live = false; }; }, [url]);
  async function act(task: () => Promise<unknown>) {
    setBusy(true); setError('');
    try { await task(); await reload(); } catch (error) { setError(message(error)); }
    finally { setBusy(false); }
  }
  function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    void act(async () => { await api(url, json('POST', { email: data.get('email'), role: data.get('role') })); form.reset(); });
  }
  return <section className="page-panel"><p className="eyebrow">一起创作</p><h2>给故事找几个好伙伴</h2>
    <p className="muted">Owner 批准关键内容与管理项目；编辑者可编辑和发起任务；查看者只读。添加前，请伙伴先注册账号。</p>
    {error && <p className="error" role="alert">{error}</p>}
    {owner && writable && <form className="invite-form" onSubmit={add}><label>伙伴的邮箱<input name="email" type="email" required placeholder="partner@example.com"/></label><label>项目角色<select name="role"><option value="editor">编辑者</option><option value="viewer">查看者</option></select></label><button className="primary" disabled={busy}>添加成员</button></form>}
    {!loaded ? <p role="status">正在寻找伙伴…</p> : members.length === 0 ? <div className="empty compact"><h3>现在是你的独立创作时间</h3><p>添加伙伴后，他们会出现在这里。项目拥有者不重复列入成员。</p></div> : <ul className="member-list">{members.map(member => <li key={member.user_id}><span className="avatar">{member.display_name.slice(0, 1)}</span><div><strong>{member.display_name}</strong><p>{member.email}</p></div>
      {owner && writable ? <><select aria-label={`${member.display_name}的角色`} value={member.role} disabled={busy} onChange={event => void act(() => api(`${url}/${member.user_id}`, json('PATCH', { role: event.target.value })))}><option value="editor">编辑者</option><option value="viewer">查看者</option></select><button disabled={busy} onClick={() => { if (window.confirm(`移除 ${member.display_name} 的项目访问权限？`)) void act(() => api(`${url}/${member.user_id}`, json('DELETE'))); }}>移除</button></> : <span>{member.role === 'editor' ? '编辑者' : '查看者'}</span>}
    </li>)}</ul>}
  </section>;
}
