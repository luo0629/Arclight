import { useCallback, useEffect, useRef, useState } from 'react';
import { api, message } from './api';

// 调用组件以资源 ID 作为 key 隔离列表生命周期；path 在该生命周期内固定。
// offset 按服务端返回条数推进，不能用去重后的可见条数代替。
export default function usePagedList<T extends { id: string }>(path: string) {
  const [rows, setRows] = useState<T[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [error, setError] = useState('');
  const [batchSize, setBatchSize] = useState(100);
  const state = useRef({ rows: [] as T[], offset: 0, hasMore: true });
  const size = useRef(batchSize); size.current = batchSize;
  const operation = useRef<Promise<T[]> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; controller.current?.abort(); }; }, [path]);

  const request = useCallback((refresh: boolean, findId?: string): Promise<T[]> => {
    if (operation.current) {
      // 写入后的刷新必须等追加结束，再重新读取，不能复用写入前的响应。
      return refresh ? operation.current.catch(() => []).then(() => request(true, findId)) : operation.current;
    }
    if (!live.current) return Promise.resolve(state.current.rows);
    if (!refresh && !state.current.hasMore) return Promise.resolve(state.current.rows);
    const abort = new AbortController(); controller.current = abort;
    setLoading(true); setError('');
    const work = (async () => {
      const previous = state.current;
      let offset = refresh ? 0 : previous.offset;
      let merged = refresh ? [] as T[] : [...previous.rows];
      let more = true;
      const target = refresh ? Math.max(previous.offset, size.current) : offset + size.current;
      try {
        do {
          const limit = refresh || findId ? 100 : size.current;
          const page = await api<T[]>(path + '?limit=' + limit + '&offset=' + offset, { signal: abort.signal });
          offset += page.length; more = page.length === limit;
          const unique = new Map(merged.map(row => [row.id, row]));
          page.forEach(row => unique.set(row.id, row)); merged = [...unique.values()];
        } while (more && (findId ? !merged.some(row => row.id === findId) : offset < target));
        if (live.current && !abort.signal.aborted) {
          state.current = { rows: merged, offset, hasMore: more };
          setRows(merged); setHasMore(more); setLoaded(true);
        }
        return merged;
      } catch (error) {
        if (!abort.signal.aborted && live.current) { setError(error instanceof TypeError ? '连接失败，请重试加载。' : message(error)); throw error; }
        return previous.rows;
      } finally {
        if (live.current && !abort.signal.aborted) setLoading(false);
        operation.current = null;
      }
    })();
    operation.current = work; return work;
  }, [path]);
  const reload = useCallback(() => request(true), [request]);
  const more = useCallback(() => request(false), [request]);
  const ensure = useCallback(async (id: string) => {
    if (operation.current) await operation.current;
    return state.current.rows.some(row => row.id === id) ? state.current.rows : request(false, id);
  }, [request]);
  const update = useCallback((row: T) => {
    const updated = state.current.rows.map(item => item.id === row.id ? row : item);
    state.current.rows = updated; setRows(updated);
  }, []);
  return { rows, loading, loaded, hasMore, error, batchSize, setBatchSize, reload, more, ensure, update };
}
