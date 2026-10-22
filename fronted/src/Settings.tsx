import { useState, type FormEvent } from 'react';
import EpisodeEditor from './EpisodeEditor';
import { api, json, message, type Episode, type Project } from './api';

export default function Settings({ project, episodes, reload, onEpisodeSaved }: { project: Project; episodes: Episode[]; onEpisodeSaved: (episode: Episode) => void; reload: () => Promise<void> }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  async function change(path: string, body: unknown) {
    setBusy(true); setError(''); setNotice('');
    try { await api(path, json('PATCH', body)); await reload(); setNotice('设置已保存'); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  function save(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const data = new FormData(event.currentTarget); void change(`/projects/${project.id}`, { name: data.get('name'), budget_limit: data.get('budget') }); }
  return <section className="page-panel"><p className="eyebrow">收好每一个小宇宙</p><h2>项目设置</h2><p className="muted">归档不会删除历史版本；归档前请先完成或取消所有进行中的任务。预算当前仅保存额度，尚未执行模型费用扣费。</p>{error && <p className="error" role="alert">{error}</p>}{notice && <p className="success" role="status">{notice}</p>}
    <form className="settings-form" onSubmit={save}><label>项目名称<input name="name" key={project.name} defaultValue={project.name} maxLength={160} required disabled={project.archived}/></label><label>预算额度<input name="budget" type="number" defaultValue={project.budget_limit} min="0" max="9999999999.99" step="0.01" required disabled={project.archived}/></label><button className="primary" disabled={busy || project.archived}>保存设置</button></form>
    <section className="settings-section"><h3>{project.archived ? '恢复这本创作手帐' : '归档项目'}</h3><p>归档期间，所有成员只能查看内容。</p><button disabled={busy} onClick={() => { if (window.confirm(project.archived ? '恢复项目并允许继续编辑？' : '归档此项目并将全部内容设为只读？')) void change(`/projects/${project.id}`, { archived: !project.archived }); }}>{project.archived ? '恢复项目' : '归档项目'}</button></section>
    <section className="settings-section"><h3>剧集管理</h3><ul className="episode-settings">{episodes.map(episode => <li key={episode.id}><span>第 {episode.episode_no} 集 · {episode.title}<small className="muted">{episode.target_duration_ms / 1000} 秒目标时长</small></span><div className="episode-settings-actions"><EpisodeEditor episode={episode} disabled={busy || project.archived || episode.status === 'ARCHIVED'} onSaved={onEpisodeSaved}/><button disabled={busy || project.archived} onClick={() => { if (window.confirm(episode.status === 'ARCHIVED' ? '恢复这集？' : '归档这集并设为只读？')) void change(`/episodes/${episode.id}`, { archived: episode.status !== 'ARCHIVED' }); }}>{episode.status === 'ARCHIVED' ? '恢复剧集' : '归档剧集'}</button></div></li>)}</ul></section>
  </section>;
}
