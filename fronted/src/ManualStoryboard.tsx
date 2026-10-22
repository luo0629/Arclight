import { useEffect, useId, useState } from 'react';
import * as Select from '@radix-ui/react-select';
import { api, message, type ContentVersion, type Episode } from './api';
import VersionEditor, { type Act } from './VersionEditor';

type Props = {
  episode: Episode; writable: boolean; busy: boolean; active: boolean;
  act: Act; reload: () => Promise<void>; latestNo: number; onCreated: () => void;
};

export default function ManualStoryboard(props: Props) {
  const labelId = useId();
  const [scripts, setScripts] = useState<ContentVersion[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [source, setSource] = useState<ContentVersion | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const disabled = props.busy || props.active || !props.writable;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setScripts([]); setSelectedId('');
    void (async () => {
      try {
        const approved: ContentVersion[] = [];
        // 起稿选择器独立读取完整剧本历史，不把工作台的前 100 条当作全部版本。
        for (let offset = 0; ; offset += 100) {
          const rows = await api<ContentVersion[]>(
            `/episodes/${props.episode.id}/scripts?limit=100&offset=${offset}`,
            { signal: controller.signal },
          );
          approved.push(...rows.filter(row => row.status === 'APPROVED'));
          if (rows.length < 100) break;
        }
        if (!controller.signal.aborted) setScripts(approved);
      } catch (error) {
        if (!controller.signal.aborted) setError(message(error));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [props.episode.id, attempt]);

  if (source) return <section className="manual-storyboard" aria-label="人工分镜起稿">
    <p className="hint">引用剧本 v{source.version_no} · {String(source.content_json.title)}（已批准）</p>
    <p className="hint source-version">精确版本：{source.id}。保存后为独立待审核版本，不会启动导演或覆盖旧稿。</p>
    <fieldset className="draft-fields" disabled={disabled}>
      <VersionEditor {...props} kind="storyboard" sourceScript={source}/>
    </fieldset>
  </section>;

  return <section className="manual-storyboard" aria-label="选择分镜来源剧本">
    <h3>先选择已批准的剧本</h3>
    <p className="hint">只使用本剧集的精确剧本版本。开始起稿后来源固定；收起人工分镜可重新选择。</p>
    {loading ? <p role="status">正在读取已批准的剧本…</p> : error ? <div role="alert" className="error">{error}<button onClick={() => setAttempt(value => value + 1)}>重试加载剧本</button></div> : scripts.length === 0 ? <p role="status" className="hint">本剧集还没有已批准的剧本，请先创建并批准一个剧本版本。</p> : <>
      <label id={`${labelId}-label`} htmlFor={labelId}>已批准的剧本版本</label>
      <Select.Root value={selectedId} onValueChange={setSelectedId} disabled={disabled}>
        <Select.Trigger id={labelId} className="script-select-trigger" aria-labelledby={`${labelId}-label`}>
          <Select.Value placeholder="请选择精确剧本版本"/><Select.Icon aria-hidden="true">▾</Select.Icon>
        </Select.Trigger>
        <Select.Portal><Select.Content className="script-select-content" position="popper" sideOffset={6} collisionPadding={12}>
          <Select.ScrollUpButton className="script-select-scroll">向上滚动</Select.ScrollUpButton>
          <Select.Viewport>{scripts.map(script => <Select.Item className="script-select-item" key={script.id} value={script.id}>
            <Select.ItemText>v{script.version_no} · {String(script.content_json.title)}</Select.ItemText>
            <Select.ItemIndicator aria-hidden="true">✓</Select.ItemIndicator>
          </Select.Item>)}</Select.Viewport>
          <Select.ScrollDownButton className="script-select-scroll">向下滚动</Select.ScrollDownButton>
        </Select.Content></Select.Portal>
      </Select.Root>
      <button disabled={disabled || !selectedId} onClick={() => setSource(scripts.find(script => script.id === selectedId) ?? null)}>使用此剧本起稿</button>
    </>}
  </section>;
}
