import { useRef, useState } from 'react';
import { api, json, statusText, type ContentVersion, type Episode, type GraphNode, type Run } from './api';

type Act = (action: () => Promise<unknown>) => Promise<boolean>;
type Script = { title: string; summary: string; characters: string[]; scenes: { scene_no: number; title: string; action: string; dialogue: { speaker: string; text: string }[] }[] };
type Board = { shots: { shot_no: number; scene_no: number; duration_ms: number; shot_size: string; visual_description: string; characters: string[]; camera_movement: string; subtitle: string }[] };

function VersionEditor({ version, kind, episode, writable, busy, act, reload, latestNo, active }: { version: ContentVersion; kind: string; episode: Episode; writable: boolean; busy: boolean; act: Act; reload: () => Promise<void>; latestNo: number; active: boolean }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(structuredClone(version.content_json));
  const [note, setNote] = useState('');
  const script = draft as unknown as Script;
  const board = draft as unknown as Board;
  function mutate(change: (value: Script & Board) => void) {
    const copy = structuredClone(draft); change(copy as unknown as Script & Board); setDraft(copy);
  }
  async function save() {
    const ok = await act(async () => {
      await api(`/episodes/${episode.id}/${kind === 'script' ? 'scripts' : 'storyboards'}`, json('POST', {
        expected_version_no: latestNo, content: draft,
        ...(kind === 'storyboard' ? { script_version_id: version.script_version_id } : {}),
      }));
      await reload();
    });
    if (ok) { setEditing(false); setNote('已保存为新版本；原版本保持不变。请在画布或列表中选择新版本。'); }
  }
  return <>
    <div className="section-heading"><h3>{kind === 'script' ? '剧本' : '分镜'} v{version.version_no}</h3><span className={`status ${version.status.toLowerCase()}`}>{statusText[version.status]}</span></div>
    {note && <p className="success" role="status">{note}</p>}
    {active && writable && <p className="hint">工作流进行中，先在任务中心完成或取消任务，再另存人工修改版本。</p>}
    {writable && <button disabled={busy || active} onClick={() => { setEditing(!editing); setDraft(structuredClone(version.content_json)); }}>{editing ? '放弃本次编辑' : '编辑并另存新版本'}</button>}
    {kind === 'script' ? <div className="document">
      {editing ? <><label>剧本标题<input value={script.title} maxLength={160} onChange={event => mutate(value => { value.title = event.target.value; })}/></label><label>梗概<textarea value={script.summary} maxLength={3000} onChange={event => mutate(value => { value.summary = event.target.value; })}/></label><label>角色（用中文逗号分隔）<input value={script.characters.join('，')} onChange={event => mutate(value => { value.characters = event.target.value.split(/[，,]/).map(x => x.trim()); })}/></label></> : <><h3>{script.title}</h3><p>{script.summary}</p><p className="muted">登场角色：{script.characters.join('、')}</p></>}
      {script.scenes.map((scene, index) => <section className="scene" key={index}>
        {editing ? <><label>场景 {scene.scene_no}<input value={scene.title} maxLength={200} onChange={event => mutate(value => { value.scenes[index].title = event.target.value; })}/></label><label>场景动作<textarea value={scene.action} maxLength={5000} onChange={event => mutate(value => { value.scenes[index].action = event.target.value; })}/></label></> : <><h4>{scene.scene_no.toString().padStart(2, '0')} · {scene.title}</h4><p>{scene.action}</p></>}
        {scene.dialogue.map((line, lineIndex) => editing ? <div className="dialogue-edit" key={lineIndex}><label>说话角色<select value={line.speaker} onChange={event => mutate(value => { value.scenes[index].dialogue[lineIndex].speaker = event.target.value; })}>{[...new Set([...script.characters, '旁白'])].map(name => <option key={name}>{name}</option>)}</select></label><label>对白<textarea value={line.text} maxLength={2000} onChange={event => mutate(value => { value.scenes[index].dialogue[lineIndex].text = event.target.value; })}/></label><button onClick={() => mutate(value => { value.scenes[index].dialogue.splice(lineIndex, 1); })}>移除对白</button></div> : <p className="dialogue" key={lineIndex}><strong>{line.speaker}</strong>{line.text}</p>)}
        {editing && <div className="button-row"><button onClick={() => mutate(value => { value.scenes[index].dialogue.push({ speaker: '旁白', text: '' }); })}>添加对白</button><button disabled={script.scenes.length === 1} onClick={() => mutate(value => { value.scenes.splice(index, 1); value.scenes.forEach((row, i) => { row.scene_no = i + 1; }); })}>移除此场景</button></div>}
      </section>)}
      {editing && <button disabled={script.scenes.length >= 50} onClick={() => mutate(value => { value.scenes.push({ scene_no: value.scenes.length + 1, title: '', action: '', dialogue: [] }); })}>添加场景</button>}
    </div> : <div className="document">{board.shots.map((shot, index) => <section className="scene" key={index}><h4>镜头 {shot.shot_no.toString().padStart(2, '0')}</h4>
      {editing ? <><label>画面描述<textarea value={shot.visual_description} maxLength={3000} onChange={event => mutate(value => { value.shots[index].visual_description = event.target.value; })}/></label><div className="field-pair"><label>时长（秒）<input type="number" min="0.5" max="30" step="0.1" value={shot.duration_ms / 1000} onChange={event => mutate(value => { value.shots[index].duration_ms = Math.round(Number(event.target.value) * 1000); })}/></label><label>场景编号<input type="number" min="1" value={shot.scene_no} onChange={event => mutate(value => { value.shots[index].scene_no = Number(event.target.value); })}/></label></div><label>景别<select value={shot.shot_size} onChange={event => mutate(value => { value.shots[index].shot_size = event.target.value; })}><option value="wide">远景</option><option value="full">全景</option><option value="medium">中景</option><option value="close_up">特写</option></select></label><label>运镜<select value={shot.camera_movement} onChange={event => mutate(value => { value.shots[index].camera_movement = event.target.value; })}><option value="static">固定</option><option value="push_in">推近</option><option value="pull_out">拉远</option><option value="pan">摇镜</option></select></label><label>字幕<textarea value={shot.subtitle} maxLength={2000} onChange={event => mutate(value => { value.shots[index].subtitle = event.target.value; })}/></label></> : <><p>{shot.visual_description}</p><p className="muted">场景 {shot.scene_no} · {shot.duration_ms / 1000} 秒 · {shot.shot_size}</p>{shot.subtitle && <blockquote>{shot.subtitle}</blockquote>}</>}
    </section>)}</div>}
    {editing && <button className="primary wide" disabled={busy} onClick={() => void save()}>{busy ? '保存中…' : '保存为新版本'}</button>}
  </>;
}

export default function Inspector({ node, episode, scripts, boards, runs, owner, writable, busy, act, reload, showTasks }: { node: GraphNode | null; episode: Episode; scripts: ContentVersion[]; boards: ContentVersion[]; runs: Run[]; owner: boolean; writable: boolean; busy: boolean; act: Act; reload: () => Promise<void>; showTasks: () => void }) {
  const [story, setStory] = useState('');
  const requestKey = useRef({ story: '', key: '' });
  const active = runs.some(run => ['QUEUED', 'RUNNING', 'WAITING_REVIEW'].includes(run.status));
  const version = node?.kind === 'script' ? scripts.find(row => row.id === node.resource_id) : boards.find(row => row.id === node?.resource_id);
  async function generate() {
    if (requestKey.current.story !== story || !requestKey.current.key) requestKey.current = { story, key: crypto.randomUUID() };
    await act(async () => { await api(`/episodes/${episode.id}/script-runs`, { ...json('POST', { story }), headers: { 'Idempotency-Key': requestKey.current.key } }); await reload(); showTasks(); });
  }
  return <aside className="inspector" aria-label="内容与属性"><div className="inspector-heading"><span className="eyebrow">这一页的细节</span><span className="mini-flower" aria-hidden="true">✿</span></div>
    {!node || node.kind === 'episode' ? <><h2>先写下你的灵感</h2><p className="muted">一场相遇、一段对白，或者一个小小的“如果”。编剧会把它展开成可审核的剧本。</p><label>故事灵感<textarea className="story-input" value={story} onChange={event => setStory(event.target.value)} maxLength={20000} placeholder="雨夜，一位修伞的女孩遇到了一只会说话的小猫……" disabled={!writable}/></label><p className="counter">{story.length} / 20000</p><button className="primary wide" disabled={!writable || busy || active || !story.trim()} onClick={() => void generate()}>{busy ? '正在提交…' : '请编剧开始创作'}</button>{active && <p className="hint">已有进行中的创作任务。<button className="text-button" onClick={showTasks}>前往任务与审核</button></p>}<p className="hint">流程：生成剧本 → 你审核 → 生成分镜 → 你审核。真实生成需要配置模型，任务执行需要启动 Worker。</p></> : node.kind === 'shot' ? <><h2>{node.title}</h2><span className={`status ${node.status.toLowerCase()}`}>{statusText[node.status]}</span><p>{node.description}</p><p className="muted">时长：{(node.duration_ms ?? 0) / 1000} 秒</p><p className="hint">修改镜头请选择所属分镜，再另存分镜新版本。图片生成链路尚未开放。</p></> : version ? <><VersionEditor key={version.id} version={version} kind={node.kind} episode={episode} writable={writable} busy={busy} act={act} reload={reload} latestNo={(node.kind === 'script' ? scripts : boards)[0]?.version_no ?? 0} active={active}/>
      {version.status === 'READY_FOR_REVIEW' && (version.run_id ? <button className="primary wide" onClick={showTasks}>前往任务审核此版本</button> : owner && writable && <div className="manual-review"><p>该版本由人工编辑创建，可在这里独立审核。</p><div className="button-row">{['APPROVE', 'REJECT'].map(decision => <button key={decision} disabled={busy} onClick={() => void act(async () => { await api(`/episodes/${episode.id}/${node.kind === 'script' ? 'scripts' : 'storyboards'}/${version.id}/review`, json('POST', { decision })); await reload(); })}>{decision === 'APPROVE' ? '批准该版本' : '驳回该版本'}</button>)}</div></div>)}
    </> : <p className="hint">此版本不在最近 100 个版本中，请通过 API 查询完整历史。</p>}
  </aside>;
}
