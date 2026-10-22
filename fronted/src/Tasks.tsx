import { useEffect, useState } from 'react';
import usePagedList from './usePagedList';
import ListPagination from './ListPagination';
import { api, json, statusText, type Run } from './api';

type Act = (action: () => Promise<unknown>) => Promise<boolean>;
export default function Tasks({ runs, owner, writable, busy, act, reload, pagination }: { runs: Run[]; pagination: React.ReactNode; owner: boolean; writable: boolean; busy: boolean; act: Act; reload: () => Promise<void> }) {
  const [eventRun, setEventRun] = useState('');
  const [comment, setComment] = useState('');
  async function action(run: Run, command: string, decision?: string) {
    const payload = command === 'resume' ? { ...run.pending_review, decision, comment, expected_revision: run.revision } : undefined;
    await act(async () => { await api(`/runs/${run.id}/${command}`, json('POST', payload)); await reload(); setComment(''); });
  }
  return <section className="page-panel"><p className="eyebrow">每一步都有迹可循</p><h2>创作任务与审核</h2>
    <p className="muted">生成在后台 Worker 中执行。等待审核时，只有项目 Owner 可以批准或驳回对应的具体版本。</p>
    {runs.length === 0 ? <div className="empty compact"><h3>还没有创作任务</h3><p>回到创作画布，在故事起点写下灵感并生成剧本。</p></div> : <div className="task-list">{runs.map(run => <article className="task" key={run.id}>
      <div className="task-heading"><strong>剧本与分镜创作</strong><span className={`status ${run.status.toLowerCase()}`}>{statusText[run.status] ?? run.status}</span></div>
      <p className="muted">{new Date(run.created_at).toLocaleString('zh-CN')} · 修订 {run.revision}</p><code className="task-id">{run.id}</code>
      {run.error_code && <p className="error">错误代码：{run.error_code}。请检查服务配置和 Worker 日志。</p>}
      {run.pending_review && <p>等待审核：{run.pending_review.resource_type === 'script' ? '剧本' : '分镜'}版本 <code>{run.pending_review.version_id}</code></p>}
      {owner && writable && run.pending_review && <><label>审核意见<textarea value={comment} onChange={event => setComment(event.target.value)} maxLength={2000} placeholder="记录这次创作决定的原因"/></label><div className="button-row"><button className="primary" disabled={busy} onClick={() => void action(run, 'resume', 'APPROVE')}>批准并继续</button><button disabled={busy} onClick={() => void action(run, 'resume', 'REJECT')}>驳回该版本</button></div></>}
      <div className="button-row">{writable && ['QUEUED', 'WAITING_REVIEW'].includes(run.status) && <button disabled={busy} onClick={() => { if (window.confirm('取消此任务？已有版本会保留。')) void action(run, 'cancel'); }}>取消任务</button>}
      {writable && run.status === 'FAILED' && <button disabled={busy} onClick={() => void action(run, 'retry')}>重试任务</button>}
      <button disabled={busy} onClick={() => setEventRun(run.id)}>查看执行记录</button></div>
    </article>)}</div>}
    {pagination}
    {eventRun && <EventHistory key={eventRun} runId={eventRun} close={() => setEventRun('')}/>}
  </section>;
}

type RunEvent = { id: string; event_type: string; created_at: string; payload_json: unknown };
function EventHistory({ runId, close }: { runId: string; close: () => void }) {
  const events = usePagedList<RunEvent>('/runs/' + runId + '/events');
  const { reload } = events;
  useEffect(() => { void reload().catch(() => {}); }, [reload]);
  return <section className="event-panel" aria-label="执行记录"><div className="section-heading"><h3>执行记录</h3><button onClick={close}>收起记录</button></div><p className="muted">任务 {runId} · 按时间从早到晚</p>
    {events.loaded && !events.rows.length && <p className="hint">暂无执行记录</p>}
    <ol>{events.rows.map(event => <li key={event.id}><time>{new Date(event.created_at).toLocaleTimeString('zh-CN')}</time><strong>{event.event_type}</strong><details><summary>事件数据</summary><pre>{JSON.stringify(event.payload_json, null, 2)}</pre></details></li>)}</ol>
    <ListPagination {...events} label="事件" count={events.rows.length}/>
  </section>;
}
