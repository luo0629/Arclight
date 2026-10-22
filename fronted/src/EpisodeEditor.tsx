import { useId, useRef, useState, type FormEvent } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { api, json, message, type Episode } from './api';

export default function EpisodeEditor({ episode, disabled = false, onSaved }: {
  episode: Episode; disabled?: boolean; onSaved: (episode: Episode) => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [duration, setDuration] = useState('');
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  function changeOpen(next: boolean) {
    if (saving.current || (next && disabled)) return;
    if (next) {
      setTitle(episode.title); setDuration(String(episode.target_duration_ms / 1000));
      setError(''); setNotice('');
    }
    setOpen(next);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving.current || disabled) return;
    const cleanTitle = title.trim();
    const milliseconds = Number(duration) * 1000;
    if (!cleanTitle || cleanTitle.length > 160) { setError('请输入 1–160 个字符的剧集标题，不能只填空格。'); return; }
    if (!Number.isFinite(milliseconds) || milliseconds < 2500 || milliseconds > 1800000 || Math.abs(milliseconds - Math.round(milliseconds)) > 0.000001) {
      setError('目标时长应为 2.5–1800 秒，最多保留三位小数。'); return;
    }
    saving.current = true; setBusy(true); setError('');
    try {
      const updated = await api<Episode>('/episodes/' + episode.id, json('PATCH', {
        title: cleanTitle, target_duration_ms: Math.round(milliseconds),
      }));
      // 使用服务端返回值同步目录和工作区，避免保存成功却因二次读取失败而误报失败。
      onSaved(updated); setOpen(false); setNotice('剧集信息已保存');
    } catch (error) { setError(error instanceof TypeError ? '网络连接失败，请检查连接后重试。输入内容已保留。' : message(error)); }
    finally { saving.current = false; setBusy(false); }
  }

  return <div className="episode-editor">
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Trigger asChild><button disabled={disabled}>编辑剧集</button></Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="episode-dialog-overlay"/>
        <Dialog.Content className="episode-dialog" onInteractOutside={event => event.preventDefault()} onEscapeKeyDown={event => { if (saving.current) event.preventDefault(); }}>
          <Dialog.Title>编辑剧集信息</Dialog.Title>
          <Dialog.Description>修改第 {episode.episode_no} 集的标题与目标时长，不会改写已有剧本或分镜。进行中的任务须先结束或取消。</Dialog.Description>
          <form onSubmit={save} aria-busy={busy}>
            <fieldset className="draft-fields" disabled={busy || disabled}>
              <div className="editor-field"><label htmlFor={id + '-title'}>剧集标题</label><input id={id + '-title'} value={title} onChange={event => setTitle(event.target.value)} maxLength={160} required/></div>
              <div className="editor-field"><label htmlFor={id + '-duration'}>目标时长（秒）</label><input id={id + '-duration'} type="number" min="2.5" max="1800" step="0.001" value={duration} onChange={event => setDuration(event.target.value)} aria-describedby={id + '-hint'} required/><small id={id + '-hint'} className="muted">2.5–1800 秒，最多三位小数。</small></div>
            </fieldset>
            {error && <p className="error" role="alert">{error}</p>}
            <div className="button-row"><Dialog.Close asChild><button type="button" disabled={busy}>取消</button></Dialog.Close><button className="primary" disabled={busy || disabled}>{busy ? '保存中…' : '保存剧集'}</button></div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
    {notice && <small className="episode-saved" role="status">{notice}</small>}
  </div>;
}
