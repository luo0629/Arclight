"use client";

import { useCallback, useState, type ReactNode } from "react";
import { ChoiceSelect } from "@/components/choice-select";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { EmptyState, Notice } from "@/components/ui-kit";
import { useAuth } from "@/features/auth/auth-context";
import useRenderMutation, {
  pendingCreationKey,
} from "@/features/rendering/hooks/use-render-mutation";
import { ApiError } from "@/lib/api/client";
import type { ContentVersion, Episode } from "@/types/api";
import { audioApi } from "../api";
import type { AudioLine, AudioSnapshot } from "../types";
import useAudioPage from "../use-audio-page";
import { AudioFeedback, AudioPagination } from "./audio-fields";
import AudioLinePanel from "./audio-line";

type Props = {
  episode: Episode;
  scripts: ContentVersion[];
  scriptPagination: ReactNode;
  owner: boolean;
  writable: boolean;
  onOpenRender: () => void;
};
export default function AudioWorkspace(props: Props) {
  const { user } = useAuth();
  const [denied, setDenied] = useState("");
  const [epoch, setEpoch] = useState(0);
  const unavailable = useCallback((cause: unknown) => {
    if (cause instanceof ApiError && [401, 403, 404].includes(cause.status))
      setDenied("配音来源暂不可访问。请核对当前项目权限与来源，再重新读取。");
  }, []);
  return (
    <section
      aria-label="配音工作区"
      className="flex min-h-0 min-w-0 flex-1 flex-col gap-5 overflow-y-auto pb-8"
    >
      <header className="flex flex-col gap-2">
        <p className="text-xs text-muted-foreground">这一集的声音</p>
        <h2 className="text-2xl font-semibold">逐句配音</h2>
        <p className="max-w-2xl text-sm text-muted-foreground">
          选定已批准剧本，为每句对白和旁白准备声音。逐句试听、替换与审核，保留每一次配音。
        </p>
      </header>
      {denied ? (
        <Notice tone="error">
          {denied}
          <Button
            variant="outline"
            onClick={() => {
              setDenied("");
              setEpoch((value) => value + 1);
            }}
          >
            重新核对配音来源
          </Button>
        </Notice>
      ) : (
        <AudioWorkspaceBody
          key={`${user.id}:${props.episode.id}:${props.owner}:${props.writable}:${epoch}`}
          {...props}
          userId={user.id}
          unavailable={unavailable}
        />
      )}
    </section>
  );
}

const snapshotId = (row: AudioSnapshot) => row.id;
const lineId = (row: AudioLine) => row.line_id;
function AudioWorkspaceBody({
  episode,
  scripts,
  scriptPagination,
  owner,
  writable,
  onOpenRender,
  userId,
  unavailable,
}: Props & {
  userId: string;
  unavailable: (cause: unknown) => void;
}) {
  const scope = `${userId}:${episode.project_id}:${episode.id}`;
  const fetchSnapshots = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      audioApi.snapshots(episode.id, offset, signal),
    [episode.id],
  );
  const snapshots = useAudioPage(
    scope,
    fetchSnapshots,
    snapshotId,
    unavailable,
  );
  const [selectedId, setSelectedId] = useState("");
  const [scriptId, setScriptId] = useState("");
  const selected = selectedId
    ? snapshots.rows.find((row) => row.id === selectedId)
    : snapshots.rows[0];
  const creationScope = `${scope}:snapshot:${scriptId}`;
  const mutation = useRenderMutation(creationScope, writable, unavailable);
  async function create() {
    if (
      !scripts.some(
        (row) =>
          row.id === scriptId &&
          row.episode_id === episode.id &&
          row.status === "APPROVED",
      )
    )
      return;
    const pending = pendingCreationKey(creationScope);
    await mutation.mutate(
      (signal) =>
        audioApi.createSnapshot(episode.id, scriptId, pending.key, signal),
      (row) => {
        pending.clear();
        snapshots.prepend(row);
        setSelectedId(row.id);
      },
      "已固定本次配音的剧本与台词。后续改稿不会改写这份来源。",
    );
  }
  return (
    <>
      {writable && (
        <details
          className="rounded-xl border p-4"
          open={!snapshots.loading && snapshots.rows.length === 0}
        >
          <summary className="cursor-pointer font-medium">
            从已批准剧本准备配音
          </summary>
          <div className="mt-4 flex min-w-0 flex-col gap-3">
            <Field>
              <FieldLabel htmlFor="audio-script-version">
                配音来源剧本
              </FieldLabel>
              <ChoiceSelect
                id="audio-script-version"
                value={scriptId}
                placeholder="选择已批准剧本版本"
                disabled={mutation.busy}
                options={scripts
                  .filter((row) => row.episode_id === episode.id)
                  .map((row) => ({
                    value: row.id,
                    label: `剧本 v${row.version_no}${row.status === "APPROVED" ? " · 已批准" : " · 尚不可用于配音"}`,
                    disabled: row.status !== "APPROVED",
                  }))}
                onValueChange={setScriptId}
              />
              <FieldDescription>
                只固定所选版本的对白和旁白，不自动生成或产生供应商费用。
              </FieldDescription>
            </Field>
            {scriptPagination}
            <Button
              disabled={mutation.busy || !scriptId}
              onClick={() => void create()}
            >
              {mutation.busy ? "正在准备台词…" : "固定本次配音台词"}
            </Button>
            <AudioFeedback {...mutation} />
          </div>
        </details>
      )}
      {!writable && (
        <Notice>
          当前为只读模式，可以查看音频历史和试听，不能生成、替换或审核音频。
        </Notice>
      )}
      {snapshots.rows.length > 0 && (
        <Field>
          <FieldLabel htmlFor="audio-snapshot">已保存的配音来源</FieldLabel>
          <ChoiceSelect
            id="audio-snapshot"
            value={selected?.id ?? ""}
            disabled={snapshots.loading || mutation.busy}
            options={snapshots.rows.map((row) => ({
              value: row.id,
              label: `${row.created_at} · ${row.line_count} 句`,
            }))}
            onValueChange={setSelectedId}
          />
        </Field>
      )}
      <AudioPagination list={snapshots} label="配音来源" />
      {selected ? (
        <SnapshotLines
          key={`${scope}:${selected.id}:${writable}:${owner}`}
          snapshot={selected}
          projectId={episode.project_id}
          scope={`${scope}:${selected.id}`}
          owner={owner}
          writable={writable}
          unavailable={unavailable}
          onOpenRender={onOpenRender}
        />
      ) : (
        !snapshots.loading &&
        !snapshots.error && (
          <EmptyState>
            <h3>先为这一集准备台词</h3>
            <p>从已批准剧本固定配音来源后，就可以逐句制作、试听和替换声音。</p>
          </EmptyState>
        )
      )}
    </>
  );
}

function SnapshotLines({
  snapshot,
  projectId,
  scope,
  owner,
  writable,
  unavailable,
  onOpenRender,
}: {
  snapshot: AudioSnapshot;
  projectId: string;
  scope: string;
  owner: boolean;
  writable: boolean;
  unavailable: (cause: unknown) => void;
  onOpenRender: () => void;
}) {
  const fetchLines = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      audioApi.lines(snapshot.id, offset, signal),
    [snapshot.id],
  );
  const lines = useAudioPage(scope, fetchLines, lineId, unavailable);
  const [selectedId, setSelectedId] = useState("");
  const selected = selectedId
    ? lines.rows.find((row) => row.line_id === selectedId)
    : lines.rows[0];
  return (
    <>
      <details className="min-w-0 text-xs text-muted-foreground">
        <summary>查看配音来源版本</summary>
        <p className="mt-2 break-all">剧本：{snapshot.script_version_id}</p>
        <p className="mt-1 break-all">内容哈希：{snapshot.script_sha256}</p>
      </details>
      <div className="grid min-w-0 items-start gap-6 lg:grid-cols-[minmax(12rem,19rem)_minmax(0,1fr)]">
        <aside
          aria-label="配音台词目录"
          className="flex min-w-0 flex-col gap-3 rounded-xl bg-muted/40 p-3"
        >
          <h3 className="font-semibold">台词 · {snapshot.line_count} 句</h3>
          <ol className="flex min-w-0 flex-col gap-2">
            {lines.rows.map((row, index) => (
              <li key={row.line_id}>
                <Button
                  variant={
                    row.line_id === selected?.line_id ? "secondary" : "ghost"
                  }
                  className="h-auto w-full min-w-0 flex-col items-start gap-1 py-3 text-left whitespace-normal"
                  aria-pressed={row.line_id === selected?.line_id}
                  onClick={() => setSelectedId(row.line_id)}
                >
                  <span className="text-xs">
                    {index + 1} · 场景 {row.scene_no} · {row.speaker}
                  </span>
                  <span className="line-clamp-2 break-words text-sm font-normal">
                    {row.text}
                  </span>
                </Button>
              </li>
            ))}
          </ol>
          <AudioPagination list={lines} label="台词" />
        </aside>
        {selected && (
          <AudioLinePanel
            key={`${scope}:${selected.line_id}`}
            projectId={projectId}
            snapshot={snapshot}
            line={selected}
            scope={`${scope}:${selected.line_id}`}
            owner={owner}
            writable={writable}
            unavailable={unavailable}
            onOpenRender={onOpenRender}
          />
        )}
      </div>
    </>
  );
}
