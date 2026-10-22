import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { api, configureAuth, json, message, refreshSession, statusText, type ContentVersion, type Episode, type Graph, type GraphNode, type Project, type Run, type Session, type User } from './api';
import Canvas from './Canvas';
import Inspector from './Inspector';
import Login from './Login';
import Members from './Members';
import Tasks from './Tasks';
import Settings from './Settings';
import EpisodeEditor from './EpisodeEditor';
import Dialog from './Dialog';
import usePagedList from './usePagedList';
import ListPagination from './ListPagination';

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [sessionError, setSessionError] = useState('');
  useEffect(() => {
    let live = true;
    configureAuth('', () => { setUser(null); setSessionError('登录已失效，请重新登录。'); });
    refreshSession().then(session => { if (live) setUser(session.user); })
      .catch(error => { if (live && error.status !== 401) setSessionError(message(error)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);
  function login(session: Session) { configureAuth(session.access_token); setUser(session.user); setSessionError(''); }
  async function logout() {
    try { await api('/auth/logout', json('POST')); configureAuth(''); setUser(null); }
    catch (error) { setSessionError(message(error)); }
  }
  if (loading) return <main className="boot-screen" role="status"><span className="brand-mark">a</span><p>正在打开你的创作手帐…</p></main>;
  return <>{sessionError && <div className="global-error" role="alert">{sessionError}<button onClick={() => setSessionError('')}>关闭提示</button></div>}{user ? <Workbench key={user.id} user={user} logout={logout}/> : <Login onLogin={login}/>}</>;
}

function Workbench({ user, logout }: { user: User; logout: () => Promise<void> }) {
  const projectList = usePagedList<Project>('/projects');
  const { rows: projects, reload: loadProjects } = projectList;
  const [pinnedProject, setPinnedProject] = useState<Project | null>(null);
  const [projectId, setProjectId] = useState(() => localStorage.getItem('arclight-project') ?? '');
  const currentProjectId = useRef(projectId); currentProjectId.current = projectId;
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const reload = useCallback(async () => {
    const rows = await loadProjects();
    const id = currentProjectId.current;
    if (id && !rows.some(row => row.id === id)) {
      try { const selected = await api<Project>('/projects/' + id); if (currentProjectId.current === id) setPinnedProject(selected); }
      catch (error) { if (![403, 404].includes((error as { status?: number }).status ?? 0)) throw error; if (currentProjectId.current === id) { setPinnedProject(rows[0] ?? null); setProjectId(rows[0]?.id ?? ''); } }
    } else { setPinnedProject(rows.find(row => row.id === id) ?? rows[0] ?? null); if (!id) setProjectId(rows[0]?.id ?? ''); }
    setLoaded(true); setError('');
  }, [loadProjects]);
  useEffect(() => { void reload().catch(error => setError(message(error))); }, [reload]);
  const project = projects.find(row => row.id === projectId) ?? (pinnedProject?.id === projectId ? pinnedProject : null);
  function updateProject(updated: Project) { projectList.update(updated); setPinnedProject(updated); }
  function select(id: string) { setPinnedProject(projects.find(row => row.id === id) ?? null); setProjectId(id); localStorage.setItem('arclight-project', id); }
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(''); const data = new FormData(event.currentTarget);
    try { const row = await api<Project>('/projects', json('POST', { name: data.get('name') })); await reload(); select(row.id); setPinnedProject(row); setCreating(false); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  const projectPicker = <><label className="project-picker">我的创作本<select value={projectId} onChange={event => select(event.target.value)} aria-label="选择项目"><option value="" disabled>选择一个项目</option>{(project && !projects.some(row => row.id === project.id) ? [project, ...projects] : projects).map(row => <option key={row.id} value={row.id}>{row.name}{row.archived ? '（归档）' : ''}</option>)}</select></label><ListPagination {...projectList} reload={reload} label="项目" count={projects.length}/><button className="new-project" onClick={() => setCreating(true)}>＋ 新建项目</button></>;
  return <div className="application"><header className="app-header"><a className="brand" href="/" aria-label="Arclight 创作手帐首页"><span className="brand-mark" aria-hidden="true">a</span><span>Arclight<small>创作手帐</small></span></a><p className="header-note">让好故事，一格一格发生。</p><div className="account"><span className="avatar">{user.display_name.slice(0, 1)}</span><span>{user.display_name}</span><button onClick={() => void logout()}>退出登录</button></div></header>
    {error && <div className="global-error" role="alert">{error}<button onClick={() => void reload().catch(error => setError(message(error)))}>重试载入项目</button></div>}
    {project ? <ProjectWorkspace key={project.id} project={project} projectPicker={projectPicker} reloadProjects={reload} onProjectSaved={updateProject}/> : <div className="app-shell"><aside className="sidebar">{projectPicker}</aside><main className="empty welcome-empty"><span className="big-flower" aria-hidden="true">✿</span><p className="eyebrow">故事从这里开始</p><h1>{loaded ? '新的一页，等你落笔' : '正在载入你的创作本…'}</h1><p>为一个小宇宙新建项目，再添加第一集。</p><button className="primary" onClick={() => setCreating(true)}>创建第一个项目</button></main></div>}
    {creating && <Dialog labelledBy="project-title" busy={busy} onDismiss={() => setCreating(false)}><h2 id="project-title">给新故事起个名字</h2><form onSubmit={create}><label>项目名称<input name="name" required maxLength={160} autoFocus placeholder="例如：雨巷里的小小奇遇"/></label>{error && <p className="error" role="alert">{error}</p>}<div className="button-row"><button type="button" disabled={busy} onClick={() => setCreating(false)}>先不创建</button><button className="primary" disabled={busy}>{busy ? '创建中…' : '创建项目'}</button></div></form></Dialog>}
  </div>;
}

function ProjectWorkspace({ project, projectPicker, reloadProjects, onProjectSaved }: { project: Project; projectPicker: React.ReactNode; reloadProjects: () => Promise<void>; onProjectSaved: (project: Project) => void }) {
  const episodeList = usePagedList<Episode>(`/projects/${project.id}/episodes`);
  const { rows: episodes, reload: loadEpisodes } = episodeList;
  const [pinnedEpisode, setPinnedEpisode] = useState<Episode | null>(null);
  const [episodeId, setEpisodeId] = useState('');
  const [role, setRole] = useState('viewer');
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState('canvas');
  const reload = useCallback(async () => {
    const [rows, access] = await Promise.all([loadEpisodes(), api<{ role: string }>(`/projects/${project.id}/access`)]);
    setRole(access.role); setPinnedEpisode(current => current ? rows.find(row => row.id === current.id) ?? current : rows[0] ?? null); setEpisodeId(current => current || rows[0]?.id || ''); setLoaded(true); setError('');
  }, [project.id, loadEpisodes]);
  useEffect(() => { let live = true; void (async () => { try { await reload(); } catch (error) { if (live) setError(message(error)); } })(); return () => { live = false; }; }, [reload]);
  const owner = role === 'owner'; const writable = role !== 'viewer' && !project.archived;
  const episode = episodes.find(row => row.id === episodeId) ?? (pinnedEpisode?.id === episodeId ? pinnedEpisode : null);
  function updateEpisode(updated: Episode) { episodeList.update(updated); setPinnedEpisode(updated); }
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(''); const data = new FormData(event.currentTarget);
    try { const row = await api<Episode>(`/projects/${project.id}/episodes`, json('POST', { episode_no: Number(data.get('number')), title: data.get('title'), target_duration_ms: Number(data.get('duration')) * 1000 })); await reload(); setPinnedEpisode(row); setEpisodeId(row.id); setAdding(false); setPage('canvas'); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <div className="app-shell"><aside className="sidebar">{projectPicker}<div className="sidebar-rule"/><div className="section-heading"><h2>剧集目录</h2><span className="count" title="已加载剧集数，不代表总数">{episodes.length}{episodeList.hasMore ? '+' : ''}</span></div>
    <nav aria-label="剧集列表" className="episode-nav">{(episode && !episodes.some(row => row.id === episode.id) ? [...episodes, episode] : episodes).map(row => <button className={row.id === episodeId ? 'active' : ''} key={row.id} onClick={() => { setPinnedEpisode(row); setEpisodeId(row.id); setPage('canvas'); }}><span className="episode-number">{String(row.episode_no).padStart(2, '0')}</span><span>{row.title}<small>{row.status === 'ARCHIVED' ? '已归档' : `${row.target_duration_ms / 1000} 秒目标时长`}</small></span></button>)}</nav><ListPagination {...episodeList} reload={reload} label="剧集" count={episodes.length}/>
    {writable && <button className="dashed-button" onClick={() => setAdding(true)}>＋ 添加一集</button>}
    <div className="sidebar-bottom"><p className="hint">{owner ? '你是项目拥有者' : role === 'editor' ? '你可以编辑与创建任务' : '你当前拥有只读权限'}</p><button className={page === 'members' ? 'active' : ''} onClick={() => setPage('members')}>创作伙伴</button>{owner && <button onClick={() => setPage('settings')}>项目设置与归档</button>}<p className="version-note">文本工作台 · 图片与成片开发中</p></div>
  </aside><main className="workspace"><div className="workspace-top"><div><p className="breadcrumb">创作本 / {project.name}{project.archived ? ' / 已归档' : ''}</p><h1>{page === 'members' ? '创作伙伴' : page === 'settings' ? '项目设置' : episode ? `第 ${episode.episode_no} 集 · ${episode.title}` : '你的故事，准备开场'}</h1>{page === 'canvas' && episode && role !== 'viewer' && <EpisodeEditor key={episode.id} episode={episode} disabled={!writable || episode.status === 'ARCHIVED'} onSaved={updateEpisode}/>}</div><span className="notebook-stamp" aria-hidden="true">MAKE A<br/>LITTLE MAGIC</span></div>
    {error && <p className="error workspace-error" role="alert">{error}<button onClick={() => void reload().catch(error => setError(message(error)))}>重试</button></p>}
    {page === 'members' ? <Members key={project.id} projectId={project.id} owner={owner} writable={writable}/> : page === 'settings' ? <Settings project={project} episodes={episode && !episodes.some(row => row.id === episode.id) ? [...episodes, episode] : episodes} pagination={<ListPagination {...episodeList} reload={reload} label="剧集" count={episodes.length}/>} onProjectSaved={onProjectSaved} onEpisodeSaved={updateEpisode} reload={async () => { await reloadProjects(); await reload(); }}/> : episode ? <EpisodeWorkspace key={episode.id} episode={episode} owner={owner} writable={writable && episode.status !== 'ARCHIVED'}/> : <div className="empty"><span className="big-flower" aria-hidden="true">✿</span><h2>{loaded ? '写下第一集的名字吧' : '正在载入剧集…'}</h2><p>每一集都有独立的剧本、分镜、画布与任务记录。</p>{writable && <button className="primary" onClick={() => setAdding(true)}>添加第一集</button>}</div>}
  </main>
  {adding && <Dialog labelledBy="episode-title" busy={busy} onDismiss={() => setAdding(false)}><h2 id="episode-title">开始新的一集</h2><form onSubmit={create}><label>剧集标题<input name="title" required maxLength={160} autoFocus/></label><div className="field-pair"><label>集数{episodeList.hasMore && <small className="hint">目录尚未全部加载，请核对集数，避免重复。</small>}<input name="number" type="number" min="1" defaultValue={Math.max(0, ...episodes.map(row => row.episode_no)) + 1} required/></label><label>目标时长（秒）<input name="duration" type="number" min="2.5" max="1800" step="0.5" defaultValue="30" required/></label></div>{error && <p className="error" role="alert">{error}</p>}<div className="button-row"><button type="button" disabled={busy} onClick={() => setAdding(false)}>取消</button><button className="primary" disabled={busy}>{busy ? '创建中…' : '创建剧集'}</button></div></form></Dialog>}
  </div>;
}

type EpisodeData = { graph: Graph };
function EpisodeWorkspace({ episode, owner, writable }: { episode: Episode; owner: boolean; writable: boolean }) {
  const [data, setData] = useState<EpisodeData | null>(null);
  const prefix = `/episodes/${episode.id}`;
  const scripts = usePagedList<ContentVersion>(prefix + '/scripts');
  const boards = usePagedList<ContentVersion>(prefix + '/storyboards');
  const runs = usePagedList<Run>(prefix + '/runs');
  const { reload: loadScripts, ensure: ensureScript } = scripts;
  const { reload: loadBoards, ensure: ensureBoard } = boards;
  const { reload: loadRuns } = runs;
  const [selectedId, setSelectedId] = useState('');
  const [view, setView] = useState(() => window.matchMedia('(max-width: 900px)').matches ? 'list' : 'canvas');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const mounted = useRef(true); const sequence = useRef(0);
  const reload = useCallback(async () => {
    const ticket = ++sequence.current;
    const prefix = `/episodes/${episode.id}`;
    const [, , , graph] = await Promise.all([loadScripts(), loadBoards(), loadRuns(), api<Graph>(prefix + '/canvas')]);
    if (mounted.current && ticket === sequence.current) setData({ graph });
  }, [episode.id, episode.title, episode.target_duration_ms, loadScripts, loadBoards, loadRuns]);
  useEffect(() => { mounted.current = true; void reload().catch(error => { if (mounted.current) setError(message(error)); }); return () => { mounted.current = false; sequence.current++; }; }, [reload]);
  const active = runs.rows.some(run => ['QUEUED', 'RUNNING', 'WAITING_REVIEW'].includes(run.status));
  useEffect(() => {
    if (!active) return;
    let polling = false;
    const timer = window.setInterval(() => { if (polling || document.hidden) return; polling = true; void reload().catch(error => { if (mounted.current) setError(message(error)); }).finally(() => { polling = false; }); }, 3000);
    return () => window.clearInterval(timer);
  }, [active, reload]);
  async function act(action: () => Promise<unknown>) {
    if (busy) return false; setBusy(true); setError('');
    try { await action(); return true; } catch (error) { setError(message(error)); return false; }
    finally { setBusy(false); }
  }
  async function refresh() { setRefreshing(true); await act(reload); setRefreshing(false); }
  const selected = data?.graph.nodes.find(node => node.id === selectedId) ?? data?.graph.nodes[0] ?? null;
  // 画布仍是完整依赖图；点选未加载的历史节点时读取到该精确版本。
  const selectedResource = selected?.resource_id; const selectedKind = selected?.kind;
  const versionMissing = !!selectedResource && (selectedKind === 'script' ? !scripts.rows.some(row => row.id === selectedResource) : selectedKind === 'storyboard' && !boards.rows.some(row => row.id === selectedResource));
  useEffect(() => {
    if (!selectedResource || !versionMissing) return;
    void (selectedKind === 'script' ? ensureScript(selectedResource) : ensureBoard(selectedResource)).catch(() => {});
  }, [selectedResource, selectedKind, versionMissing, ensureScript, ensureBoard]);
  const loadedIds = new Set([...scripts.rows.map(row => 'script:' + row.id), ...boards.rows.map(row => 'storyboard:' + row.id)]);
  data?.graph.edges.forEach(edge => { if (loadedIds.has(edge.source) && edge.target.startsWith('shot:')) loadedIds.add(edge.target); });
  const visibleNodes = data?.graph.nodes.filter(node => node.kind === 'episode' || loadedIds.has(node.id)) ?? [];
  const versionPagination = <><ListPagination {...scripts} label="剧本版本" count={scripts.rows.length}/><ListPagination {...boards} label="分镜版本" count={boards.rows.length}/></>;
  return <><div className="workspace-toolbar"><div className="segmented" aria-label="工作区视图">{[['canvas', '创作画布'], ['list', '版本列表'], ['tasks', '任务与审核']].map(([key, label]) => <button key={key} aria-pressed={view === key} className={view === key ? 'active' : ''} onClick={() => setView(key)}>{label}{key === 'tasks' && runs.rows.some(run => run.pending_review) && <span className="review-dot"/>}</button>)}</div><button disabled={busy || refreshing} onClick={() => void refresh()}>{refreshing ? '刷新中…' : '刷新内容'}</button></div>
    {error && <div className="error workspace-error" role="alert">{error}<button onClick={() => setError('')}>收起提示</button></div>}
    {!writable && <p className="read-only" role="status">当前为只读模式：项目／剧集已归档，或你的角色是查看者。</p>}
    {!data ? <div className="empty" role="status"><h2>正在整理这一集的便签…</h2><p>如果载入失败，请检查 API 服务并点击刷新内容。</p></div> : view === 'tasks' ? <Tasks runs={runs.rows} pagination={<ListPagination {...runs} label="任务" count={runs.rows.length}/>} owner={owner} writable={writable} busy={busy} act={act} reload={reload}/> : <div className="editor-layout"><section className="creation-area" aria-label="创作内容">
      {view === 'canvas' ? <Canvas graph={data.graph} episodeId={episode.id} writable={writable} onSelect={(node: GraphNode) => setSelectedId(node.id)}/> : <div className="version-list"><div className="section-heading"><h2>故事的每一稿</h2><span className="muted">选择一项查看与编辑</span></div>{visibleNodes.map(node => <button key={node.id} className={`version-row ${selected?.id === node.id ? 'active' : ''}`} onClick={() => setSelectedId(node.id)}><span className={`kind-dot ${node.kind}`} aria-hidden="true"/><span><strong>{node.title}</strong><small>{node.version_no ? `版本 ${node.version_no}` : node.kind === 'episode' ? '故事起点' : '镜头'}</small></span><span className={`status ${node.status.toLowerCase()}`}>{statusText[node.status]}</span></button>)}{versionPagination}</div>}
      {view === 'canvas' && versionPagination}
    </section><Inspector node={selected} episode={episode} scripts={scripts.rows} boards={boards.rows} runs={runs.rows} owner={owner} writable={writable} busy={busy} act={act} reload={reload} versionLoading={scripts.loading || boards.loading} retryVersion={() => selectedResource ? (selectedKind === 'script' ? ensureScript(selectedResource) : ensureBoard(selectedResource)) : Promise.resolve()} showTasks={() => setView('tasks')}/></div>}
  </>;
}
