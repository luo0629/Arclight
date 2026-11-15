"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { ApiError, apiEventStream } from "@/lib/api/client";
import { readEventStream } from "@/lib/api/event-stream";

type Listener = () => void;
type Status = "connecting" | "connected" | "reconnecting";
const EventContext = createContext<{
  status: Status;
  subscribe: (listener: Listener) => () => void;
  reconnect: () => void;
} | null>(null);
const cursorPattern = /^v1:[aw]:[0-9a-f-]{36}:[1-9][0-9]{0,15}$/;

function wait(milliseconds: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, milliseconds);
    signal.addEventListener("abort", done, { once: true });
    if (signal.aborted) done();
  });
}

export default function ProjectEvents({
  projectId,
  onProjectChange,
  children,
}: {
  projectId: string;
  onProjectChange: () => Promise<unknown>;
  children: ReactNode;
}) {
  const [status, setStatus] = useState<Status>("connecting");
  const [denied, setDenied] = useState(false);
  const [generation, setGeneration] = useState(0);
  const listeners = useRef(new Set<Listener>());
  const projectChange = useRef(onProjectChange);
  projectChange.current = onProjectChange;
  const subscribe = useCallback((listener: Listener) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);
  const reconnect = useCallback(() => setGeneration((value) => value + 1), []);

  useEffect(() => {
    const abort = new AbortController();
    let notification: ReturnType<typeof setTimeout> | undefined;
    let lastId = "";
    let failures = 0;
    let retry = 1000;
    const seen = new Set<string>();
    let projectDirty = false;
    let projectRefreshing = false;
    async function reloadProject() {
      if (!projectDirty || projectRefreshing || abort.signal.aborted) return;
      projectDirty = false;
      projectRefreshing = true;
      try {
        await projectChange.current();
      } catch {
        // The authoritative query owns its error state.
      } finally {
        projectRefreshing = false;
        // Keep one pending invalidation, rather than queueing every event.
        if (projectDirty) void reloadProject();
      }
    }
    const notify = (projectChanged = false) => {
      if (abort.signal.aborted) return;
      projectDirty ||= projectChanged;
      if (notification) return;
      notification = setTimeout(() => {
        notification = undefined;
        if (!abort.signal.aborted) {
          listeners.current.forEach((listener) => listener());
          void reloadProject();
        }
      }, 150);
    };
    async function connect() {
      setDenied(false);
      setStatus("connecting");
      while (!abort.signal.aborted) {
        const connectedAt = Date.now();
        let graceful = false;
        try {
          const response = await apiEventStream(
            `/projects/${projectId}/events/stream`,
            lastId,
            abort.signal,
          );
          if (abort.signal.aborted) {
            await response.body?.cancel();
            return;
          }
          await readEventStream(response, abort.signal, (event) => {
            if (abort.signal.aborted) return;
            if (event.retry !== undefined)
              retry = Math.max(1000, Math.min(event.retry, 30000));
            const data = JSON.parse(event.data);
            if (event.event === "stream.error") {
              const status = Number(data.status);
              throw new ApiError(
                Number.isInteger(status) ? status : 503,
                "EVENT_STREAM_INTERRUPTED",
                "实时更新连接已中断。",
              );
            }
            if (event.event === "stream.reconnect") {
              graceful = true;
              return;
            }
            if (data.project_id !== projectId)
              throw new Error("实时更新不属于当前项目。");
            if (event.id !== undefined) {
              if (event.id && !cursorPattern.test(event.id))
                throw new Error("实时更新续传标记无效。");
              lastId = event.id;
            }
            if (event.event === "stream.ready") {
              setStatus("connected");
              // Reconcile even when no event was missed (e.g. a changed role).
              notify(true);
              return;
            }
            if (event.event === "stream.reset") {
              lastId = "";
              seen.clear();
              notify();
              return;
            }
            if (
              !event.id ||
              !["a", "w"].includes(data.source) ||
              typeof data.source_id !== "string"
            )
              throw new Error("实时更新内容无效。");
            failures = 0;
            const identity = `${data.source}:${data.source_id}`;
            if (seen.has(identity)) return;
            seen.add(identity);
            if (seen.size > 512) {
              const oldest = seen.values().next().value;
              if (oldest !== undefined) seen.delete(oldest);
            }
            notify(event.event === "project.changed");
          });
        } catch (error) {
          if (abort.signal.aborted) return;
          if (error instanceof ApiError && [403, 404].includes(error.status)) {
            setDenied(true);
            notify(true);
            return;
          }
          if (
            error instanceof ApiError &&
            error.code === "EVENT_CURSOR_INVALID"
          ) {
            lastId = "";
            notify();
          }
        }
        if (abort.signal.aborted) return;
        setStatus("reconnecting");
        if (graceful || Date.now() - connectedAt >= 10000) failures = 0;
        await wait(
          Math.min(30000, retry * 2 ** Math.min(failures, 5)),
          abort.signal,
        );
        failures++;
      }
    }
    const visible = () => {
      if (!document.hidden) notify();
    };
    document.addEventListener("visibilitychange", visible);
    void connect();
    return () => {
      abort.abort();
      clearTimeout(notification);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [projectId, generation]);

  if (denied)
    return (
      <main className="mx-auto flex max-w-xl flex-col gap-4 p-6">
        <Notice tone="error">
          当前项目不可访问，请检查权限或重新选择项目。
        </Notice>
        <Link href="/" className="text-primary underline">
          返回我的项目
        </Link>
      </main>
    );
  return (
    <EventContext.Provider value={{ status, subscribe, reconnect }}>
      {children}
    </EventContext.Provider>
  );
}

export function useProjectEvents(onChange: Listener) {
  const context = useContext(EventContext);
  const latest = useRef(onChange);
  latest.current = onChange;
  const subscribe = context?.subscribe;
  useEffect(() => subscribe?.(() => latest.current()), [subscribe]);
  return context?.status ?? "reconnecting";
}

export function ProjectStreamStatus() {
  const context = useContext(EventContext);
  if (!context) return null;
  return (
    <div
      aria-label="实时更新连接"
      className="flex min-w-0 max-w-full flex-wrap items-center gap-2 text-xs text-muted-foreground"
    >
      <span role="status">
        {context.status === "connected"
          ? "实时更新已连接"
          : context.status === "connecting"
            ? "正在连接实时更新…"
            : "实时更新连接中断，正在自动重连；可手动刷新当前页面。"}
      </span>
      {context.status === "reconnecting" && (
        <Button variant="ghost" size="sm" onClick={context.reconnect}>
          重新连接
        </Button>
      )}
    </div>
  );
}
