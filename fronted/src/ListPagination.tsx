import * as Select from '@radix-ui/react-select';

type Props = {
  label: string; count: number; loaded: boolean; loading: boolean; hasMore: boolean; error: string;
  batchSize: number; setBatchSize: (size: number) => void;
  more: () => Promise<unknown>; reload: () => Promise<unknown>;
};
export default function ListPagination({ label, count, loaded, loading, hasMore, error, batchSize, setBatchSize, more, reload }: Props) {
  return <section className="list-pagination" aria-label={label + '分页'} aria-busy={loading}>
    <p className="hint" role="status">{loaded ? '已加载 ' + count + ' 条' + (hasMore ? ' · 可继续加载' : ' · 已全部加载') : '尚未加载'}{loading && ' · 加载中…'}</p>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="pagination-actions">
      {hasMore && <><Select.Root value={String(batchSize)} onValueChange={value => setBatchSize(Number(value))} disabled={loading}>
        <Select.Trigger className="script-select-trigger" aria-label={label + '每次追加条数'}><Select.Value/><Select.Icon aria-hidden="true">▾</Select.Icon></Select.Trigger>
        <Select.Portal><Select.Content className="script-select-content" position="popper" sideOffset={6} collisionPadding={12}><Select.Viewport>{[20, 50, 100].map(size => <Select.Item key={size} value={String(size)} className="script-select-item"><Select.ItemText>每次追加 {size} 条</Select.ItemText><Select.ItemIndicator aria-hidden="true">✓</Select.ItemIndicator></Select.Item>)}</Select.Viewport></Select.Content></Select.Portal>
      </Select.Root><button disabled={loading} onClick={() => void (loaded ? more() : reload()).catch(() => {})}>{error ? '重试加载' : '加载更多'}{label}</button></>}
      <button disabled={loading} onClick={() => void reload().catch(() => {})}>刷新{label}</button>
    </div>
  </section>;
}
