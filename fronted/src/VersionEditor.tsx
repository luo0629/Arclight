import { useId, useState } from 'react';
import { api, json, statusText, type ContentVersion, type Episode } from './api';

export type Act = (action: () => Promise<unknown>) => Promise<boolean>;
type Script = { title: string; summary: string; characters: string[]; scenes: { scene_no: number; title: string; action: string; dialogue: { speaker: string; text: string }[] }[] };
type Board = { shots: { shot_no: number; scene_no: number; duration_ms: number; shot_size: string; visual_description: string; characters: string[]; camera_movement: string; subtitle: string }[] };

export default function VersionEditor({ version, kind, episode, writable, busy, act, reload, latestNo, active, onCreated, sourceScript }: { sourceScript?: ContentVersion; version?: ContentVersion; onCreated?: () => void; kind: string; episode: Episode; writable: boolean; busy: boolean; act: Act; reload: () => Promise<void>; latestNo: number; active: boolean }) {
  const editorId = useId();
  const [editing, setEditing] = useState(!version);
  const source = sourceScript?.content_json as Script | undefined;
  function emptyShot(shotNo: number): Board['shots'][number] {
    return { shot_no: shotNo, scene_no: source?.scenes[0]?.scene_no ?? 1, duration_ms: 3000, shot_size: 'medium', visual_description: '', characters: [], camera_movement: 'static', subtitle: '' };
  }
  const [draft, setDraft] = useState<Record<string, unknown>>(() => version ? structuredClone(version.content_json) : kind === 'storyboard' ? { shots: [emptyShot(1)] } : {
    title: '', summary: '', characters: ['主角'], scenes: [{ scene_no: 1, title: '', action: '', dialogue: [] }],
  });
  const [note, setNote] = useState('');
  const script = draft as unknown as Script;
  const board = draft as unknown as Board;
  function mutate(change: (value: Script & Board) => void) {
    const copy = structuredClone(draft); change(copy as unknown as Script & Board); setDraft(copy);
  }
  const invalidBoard = kind === 'storyboard' && board.shots.some(shot => !shot.visual_description.trim() || !Number.isFinite(shot.duration_ms) || shot.duration_ms < 500 || shot.duration_ms > 30000);
  async function save() {
    if (busy || active || !writable || invalidBoard || (kind === 'storyboard' && !version && !sourceScript)) return;
    const ok = await act(async () => {
      await api(`/episodes/${episode.id}/${kind === 'script' ? 'scripts' : 'storyboards'}`, json('POST', {
        expected_version_no: latestNo, content: draft,
        ...(kind === 'storyboard' ? { script_version_id: version?.script_version_id ?? sourceScript?.id } : {}),
      }));
      await reload();
    });
    if (ok) { onCreated?.(); setEditing(false); setNote('已保存为新版本；原版本保持不变。请在画布或列表中选择新版本。'); }
  }
  return <>
    <div className="section-heading"><h3>{kind === 'script' ? '剧本' : '分镜'} {version ? `v${version.version_no}` : '· 人工起稿'}</h3>{version && <span className={`status ${version.status.toLowerCase()}`}>{statusText[version.status]}</span>}</div>
    {note && <p className="success" role="status">{note}</p>}
    {active && writable && <p className="hint">工作流进行中，先在任务中心完成或取消任务，再另存人工修改版本。</p>}
    {writable && version && <button disabled={busy || active} onClick={() => { setEditing(!editing); setDraft(structuredClone(version.content_json)); }}>{editing ? '放弃本次编辑' : '编辑并另存新版本'}</button>}
    {kind === 'script' ? <div className="document">
      {editing ? <><label>剧本标题<input value={script.title} maxLength={160} onChange={event => mutate(value => { value.title = event.target.value; })}/></label><label>梗概<textarea value={script.summary} maxLength={3000} onChange={event => mutate(value => { value.summary = event.target.value; })}/></label><label>角色（用中文逗号分隔）<input value={script.characters.join('，')} onChange={event => mutate(value => { value.characters = event.target.value.split(/[，,]/).map(x => x.trim()); })}/></label></> : <><h3>{script.title}</h3><p>{script.summary}</p><p className="muted">登场角色：{script.characters.join('、')}</p></>}
      {script.scenes.map((scene, index) => <section className="scene" key={index}>
        {editing ? <><label>场景 {scene.scene_no}<input value={scene.title} maxLength={200} onChange={event => mutate(value => { value.scenes[index].title = event.target.value; })}/></label><label>场景动作<textarea value={scene.action} maxLength={5000} onChange={event => mutate(value => { value.scenes[index].action = event.target.value; })}/></label></> : <><h4>{scene.scene_no.toString().padStart(2, '0')} · {scene.title}</h4><p>{scene.action}</p></>}
        {scene.dialogue.map((line, lineIndex) => editing ? <div className="dialogue-edit" key={lineIndex}><div><label htmlFor={`${editorId}-speaker-${index}-${lineIndex}`}>说话角色</label><select id={`${editorId}-speaker-${index}-${lineIndex}`} value={line.speaker} onChange={event => mutate(value => { value.scenes[index].dialogue[lineIndex].speaker = event.target.value; })}>{[...new Set([...script.characters, '旁白'])].map(name => <option key={name}>{name}</option>)}</select></div><label>对白<textarea value={line.text} maxLength={2000} onChange={event => mutate(value => { value.scenes[index].dialogue[lineIndex].text = event.target.value; })}/></label><button onClick={() => mutate(value => { value.scenes[index].dialogue.splice(lineIndex, 1); })}>移除对白</button></div> : <p className="dialogue" key={lineIndex}><strong>{line.speaker}</strong>{line.text}</p>)}
        {editing && <div className="button-row"><button onClick={() => mutate(value => { value.scenes[index].dialogue.push({ speaker: '旁白', text: '' }); })}>添加对白</button><button disabled={script.scenes.length === 1} onClick={() => mutate(value => { value.scenes.splice(index, 1); value.scenes.forEach((row, i) => { row.scene_no = i + 1; }); })}>移除此场景</button></div>}
      </section>)}
      {editing && <button disabled={script.scenes.length >= 50} onClick={() => mutate(value => { value.scenes.push({ scene_no: value.scenes.length + 1, title: '', action: '', dialogue: [] }); })}>添加场景</button>}
    </div> : <div className="document">{board.shots.map((shot, index) => <section className="scene" key={index}><h4>镜头 {shot.shot_no.toString().padStart(2, '0')}</h4>
      {editing ? <><div className="editor-field"><label htmlFor={`${editorId}-visual-${index}`}>画面描述</label><textarea id={`${editorId}-visual-${index}`} value={shot.visual_description} maxLength={3000} onChange={event => mutate(value => { value.shots[index].visual_description = event.target.value; })}/></div><div className="field-pair"><label>时长（秒）<input type="number" min="0.5" max="30" step="0.1" value={shot.duration_ms / 1000} onChange={event => mutate(value => { value.shots[index].duration_ms = Math.round(Number(event.target.value) * 1000); })}/></label><div className="editor-field"><label htmlFor={`${editorId}-scene-${index}`}>场景编号</label>{source ? <select id={`${editorId}-scene-${index}`} value={shot.scene_no} onChange={event => mutate(value => { value.shots[index].scene_no = Number(event.target.value); })}>{source.scenes.map(scene => <option key={scene.scene_no} value={scene.scene_no}>{scene.scene_no} · {scene.title}</option>)}</select> : <input id={`${editorId}-scene-${index}`} type="number" min="1" value={shot.scene_no} onChange={event => mutate(value => { value.shots[index].scene_no = Number(event.target.value); })}/>}</div></div><div className="editor-field"><label htmlFor={`${editorId}-size-${index}`}>景别</label><select id={`${editorId}-size-${index}`} value={shot.shot_size} onChange={event => mutate(value => { value.shots[index].shot_size = event.target.value; })}><option value="wide">远景</option><option value="full">全景</option><option value="medium">中景</option><option value="close_up">特写</option></select></div><div className="editor-field"><label htmlFor={`${editorId}-movement-${index}`}>运镜</label><select id={`${editorId}-movement-${index}`} value={shot.camera_movement} onChange={event => mutate(value => { value.shots[index].camera_movement = event.target.value; })}><option value="static">固定</option><option value="push_in">推近</option><option value="pull_out">拉远</option><option value="pan">摇镜</option></select></div><label>出场角色（用中文逗号分隔）<input value={shot.characters.join("，")} onChange={event => mutate(value => { value.shots[index].characters = event.target.value ? event.target.value.split(/[，,]/).map(name => name.trim()) : []; })}/></label>{source && <p className="hint">可用角色：{source.characters.join("、")}；留空表示没有角色出镜。</p>}<div className="editor-field"><label htmlFor={`${editorId}-subtitle-${index}`}>字幕</label><textarea id={`${editorId}-subtitle-${index}`} value={shot.subtitle} maxLength={2000} onChange={event => mutate(value => { value.shots[index].subtitle = event.target.value; })}/></div></> : <><p>{shot.visual_description}</p><p className="muted">场景 {shot.scene_no} · {shot.duration_ms / 1000} 秒 · {shot.shot_size}</p>{shot.subtitle && <blockquote>{shot.subtitle}</blockquote>}</>}
      {editing && <button disabled={board.shots.length === 1} onClick={() => mutate(value => { value.shots.splice(index, 1); value.shots.forEach((row, i) => { row.shot_no = i + 1; }); })}>移除此镜头</button>}
    </section>)}{editing && <button disabled={board.shots.length >= 100} onClick={() => mutate(value => { value.shots.push(emptyShot(value.shots.length + 1)); })}>添加镜头</button>}</div>}
    {editing && invalidBoard && <p className="hint">每个镜头都需要画面描述，时长须在 0.5–30 秒之间。</p>}
    {editing && <button className="primary wide" disabled={busy || active || !writable || invalidBoard} onClick={() => void save()}>{busy ? '保存中…' : version ? '保存为新版本' : kind === 'script' ? '保存人工剧本' : '保存人工分镜'}</button>}
  </>;
}
