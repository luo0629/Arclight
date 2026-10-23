import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Notice } from "@/components/ui-kit";

type Draft = { episodeId: string; label: string; value: unknown };
type Drafts = Record<string, Draft>;
type Store = {
  drafts: Drafts;
  write: (key: string, draft?: Draft, expected?: unknown) => void;
};
const DraftContext = createContext<Store | null>(null);

function read(key: string): Drafts {
  const value: unknown = JSON.parse(sessionStorage.getItem(key) ?? "{}");
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Invalid draft store");
  const result: Drafts = {};
  for (const [id, row] of Object.entries(value)) {
    if (
      !row ||
      typeof row !== "object" ||
      typeof row.episodeId !== "string" ||
      typeof row.label !== "string" ||
      !("value" in row)
    )
      throw new Error("Invalid draft entry");
    result[id] = row as Draft;
  }
  return result;
}

export function DraftProvider({
  userId,
  children,
}: {
  userId: string;
  children: ReactNode;
}) {
  const storageKey = `arclight:drafts:v1:${userId}`;
  const [initial] = useState(() => {
    try {
      return { drafts: read(storageKey), error: false };
    } catch {
      return { drafts: {}, error: true };
    }
  });
  const current = useRef<Drafts>(initial.drafts);
  const [drafts, setDrafts] = useState(initial.drafts);
  const [storageError, setStorageError] = useState(initial.error);
  function write(key: string, draft?: Draft, expected?: unknown) {
    if (
      !draft &&
      expected !== undefined &&
      JSON.stringify(current.current[key]?.value) !== JSON.stringify(expected)
    )
      return;
    const next = { ...current.current };
    if (draft) next[key] = draft;
    else delete next[key];
    // 同步写入，不等待 effect，最后一次输入后立即切换也不会丢失。
    current.current = next;
    setDrafts(next);
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(next));
      setStorageError(false);
    } catch {
      setStorageError(true);
    }
  }
  const hasDrafts = Object.keys(drafts).length > 0;
  useEffect(() => {
    if (!hasDrafts) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasDrafts]);
  return (
    <DraftContext.Provider value={{ drafts, write }}>
      {storageError && (
        <Notice tone="error" role="alert" className="mx-4 my-3">
          本标签页草稿存储不可用或已有记录损坏。新输入暂留内存，切换页面仍可恢复；刷新、关闭或退出登录前请先保存，避免丢失。
        </Notice>
      )}
      {children}
    </DraftContext.Provider>
  );
}

export function useDraftStore() {
  const store = useContext(DraftContext);
  if (!store) throw new Error("DraftProvider is required");
  return store;
}

export function useSessionDraft<T>({
  id,
  episodeId,
  label,
  initial,
  validate,
}: {
  id: string;
  episodeId: string;
  label: string;
  initial: T;
  validate: (value: unknown) => value is T;
}) {
  const { drafts, write } = useDraftStore();
  const stored = drafts[id];
  const valid = stored && validate(stored.value);
  const [value, setValue] = useState<T>(() =>
    valid ? (stored.value as T) : initial,
  );
  const [recovered, setRecovered] = useState(!!valid);
  const invalid = !!stored && !valid;
  function update(next: T) {
    setValue(next);
    if (JSON.stringify(next) === JSON.stringify(initial)) write(id);
    else write(id, { episodeId, label, value: next });
  }
  function clear() {
    write(id, undefined, stored?.value);
    setValue(initial);
    setRecovered(false);
  }
  return { value, update, clear, recovered, invalid, dirty: !!stored };
}
