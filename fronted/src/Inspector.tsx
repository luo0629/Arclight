import { useRef, useState } from 'react';
import { api, json, statusText, type ContentVersion, type Episode, type GraphNode, type Run } from './api';

import VersionEditor, { type Act } from './VersionEditor';
import ManualStoryboard from './ManualStoryboard';

export default function Inspector({ node, episode, scripts, boards, runs, owner, writable, busy, act, reload, showTasks, versionLoading, retryVersion }: { node: GraphNode | null; episode: Episode; scripts: ContentVersion[]; boards: ContentVersion[]; runs: Run[]; owner: boolean; writable: boolean; busy: boolean; act: Act; reload: () => Promise<void>; showTasks: () => void; versionLoading: boolean; retryVersion: () => Promise<unknown> }) {
  const [story, setStory] = useState('');
  const [creating, setCreating] = useState(false);
  const [creatingBoard, setCreatingBoard] = useState(false);
  const [boardNotice, setBoardNotice] = useState('');
  const directorKey = useRef({ scriptId: '', key: '' });
  const requestKey = useRef({ story: '', key: '' });
  const active = runs.some(run => ['QUEUED', 'RUNNING', 'WAITING_REVIEW'].includes(run.status));
  const version = node?.kind === 'script' ? scripts.find(row => row.id === node.resource_id) : boards.find(row => row.id === node?.resource_id);
  async function generate() {
    if (requestKey.current.story !== story || !requestKey.current.key) requestKey.current = { story, key: crypto.randomUUID() };
    await act(async () => { await api(`/episodes/${episode.id}/script-runs`, { ...json('POST', { story }), headers: { 'Idempotency-Key': requestKey.current.key } }); await reload(); showTasks(); });
  }
  async function direct(scriptId: string) {
    if (directorKey.current.scriptId !== scriptId || !directorKey.current.key) directorKey.current = { scriptId, key: crypto.randomUUID() };
    const ok = await act(async () => {
      await api(`/episodes/${episode.id}/storyboard-runs`, { ...json('POST', { script_version_id: scriptId }), headers: { 'Idempotency-Key': directorKey.current.key } });
      await reload(); showTasks();
    });
    if (ok) directorKey.current = { scriptId: '', key: '' };
  }
  return <aside className="inspector" aria-label="内容与属性"><div className="inspector-heading"><span className="eyebrow">这一页的细节</span><span className="mini-flower" aria-hidden="true">✿</span></div>
    {!node || node.kind === 'episode' ? <>{writable && <><button disabled={busy || active} onClick={() => { setCreating(!creating); setCreatingBoard(false); }}>{creating ? '收起人工起稿' : '自己写剧本'}</button>{creating && <><p className="hint">这是尚未保存的人工草稿。保存后再选择新版本审核，已有版本不会被覆盖。</p><VersionEditor kind="script" episode={episode} writable={writable} busy={busy} act={act} reload={reload} latestNo={scripts[0]?.version_no ?? 0} active={active} onCreated={() => setCreating(false)}/></>}
      <button disabled={busy || active} onClick={() => { setCreatingBoard(!creatingBoard); setCreating(false); setBoardNotice(''); }}>{creatingBoard ? '收起人工分镜' : '自己写分镜'}</button>
      {creatingBoard && <ManualStoryboard episode={episode} writable={writable} busy={busy} active={active} act={act} reload={reload} latestNo={boards[0]?.version_no ?? 0} onCreated={() => { setCreatingBoard(false); setBoardNotice('人工分镜已保存为待审核版本；请在画布或版本列表中选择新版本审核。'); }}/>}</>}
      {boardNotice && <p className="success" role="status">{boardNotice}</p>}
      <h2>先写下你的灵感</h2><p className="muted">一场相遇、一段对白，或者一个小小的“如果”。编剧会把它展开成可审核的剧本。</p><label>故事灵感<textarea className="story-input" value={story} onChange={event => setStory(event.target.value)} maxLength={20000} placeholder="雨夜，一位修伞的女孩遇到了一只会说话的小猫……" disabled={!writable}/></label><p className="counter">{story.length} / 20000</p><button className="primary wide" disabled={!writable || busy || active || !story.trim()} onClick={() => void generate()}>{busy ? '正在提交…' : '请编剧开始创作'}</button>{active && <p className="hint">已有进行中的创作任务。<button className="text-button" onClick={showTasks}>前往任务与审核</button></p>}<p className="hint">流程：生成剧本 → 你审核 → 生成分镜 → 你审核。真实生成需要配置模型，任务执行需要启动 Worker。</p></> : node.kind === 'shot' ? <><h2>{node.title}</h2><span className={`status ${node.status.toLowerCase()}`}>{statusText[node.status]}</span><p>{node.description}</p><p className="muted">时长：{(node.duration_ms ?? 0) / 1000} 秒</p><p className="hint">修改镜头请选择所属分镜，再另存分镜新版本。图片生成链路尚未开放。</p></> : version ? <><VersionEditor key={version.id} version={version} kind={node.kind} episode={episode} writable={writable} busy={busy} act={act} reload={reload} latestNo={(node.kind === 'script' ? scripts : boards)[0]?.version_no ?? 0} active={active}/>
      {node.kind === 'script' && version.status === 'APPROVED' && writable && <><button className="primary wide" disabled={busy || active} onClick={() => void direct(version.id)}>从此版本生成分镜</button><p className="hint">导演只使用此精确剧本版本，不重新调用编剧；生成后仍需审核分镜。</p></>}
      {version.status === 'READY_FOR_REVIEW' && (version.run_id ? <button className="primary wide" onClick={showTasks}>前往任务审核此版本</button> : owner && writable && <div className="manual-review"><p>该版本由人工编辑创建，可在这里独立审核。</p><div className="button-row">{['APPROVE', 'REJECT'].map(decision => <button key={decision} disabled={busy} onClick={() => void act(async () => { await api(`/episodes/${episode.id}/${node.kind === 'script' ? 'scripts' : 'storyboards'}/${version.id}/review`, json('POST', { decision })); await reload(); })}>{decision === 'APPROVE' ? '批准该版本' : '驳回该版本'}</button>)}</div></div>)}
    </> : <div className="hint" role="status">{versionLoading ? '正在读取此历史版本…' : '此版本尚未加载，请重试或继续加载版本列表。'}<button disabled={versionLoading} onClick={() => void retryVersion().catch(() => {})}>重试加载此版本</button></div>}
  </aside>;
}
