"use client";

import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Notice } from "@/components/ui-kit";
import { useSessionDraft } from "@/providers/draft-provider";
import {
  subtitleApi,
  type SubtitleChange,
  type SubtitleTrack,
} from "../subtitle-api";
import type { Timeline, TimelineClip } from "../types";
import useRenderList from "../hooks/use-render-list";
import useRenderDetail from "../hooks/use-render-detail";
import useRenderMutation, {
  pendingCreationKey,
} from "../hooks/use-render-mutation";
import { NumberField } from "./timeline-clip-fields";
import { selectClass } from "./render-format";

const cursor = (track: SubtitleTrack) => track.next_offset ?? 0;
export default function TimelineSubtitles({
  timeline,
  projectId,
  owner,
  writable,
  unavailable,
  onApply,
}: {
  timeline: Timeline;
  projectId: string;
  owner: boolean;
  writable: boolean;
  unavailable: (cause: unknown) => void;
  onApply: (clips: TimelineClip[]) => void;
}) {
  const episode = timeline.snapshot.episode_id;
  const fetchPage = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      subtitleApi.list(episode, offset, signal),
    [episode],
  );
  const list = useRenderList<SubtitleTrack, number>(
    episode,
    fetchPage,
    cursor,
    unavailable,
  );
  const sourceId =
    timeline.snapshot.tracks.find((track) => track.kind === "SUBTITLE")
      ?.clips[0]?.source_asset_version_id ?? "";
  const [selectedId, select] = useState("");
  const selected =
    selectedId ||
    list.rows.find((row) => row.id === sourceId)?.id ||
    list.rows[0]?.id ||
    "";
  const fetchDetail = useCallback(
    (signal: AbortSignal) =>
      subtitleApi.detail(projectId, episode, selected, signal),
    [projectId, episode, selected],
  );
  const detail = useRenderDetail(
    `${episode}:${selected}`,
    fetchDetail,
    unavailable,
    !!selected,
  );
  const mutation = useRenderMutation(
    `subtitle-generate:${timeline.id}`,
    writable,
    unavailable,
  );
  async function generate() {
    const key = pendingCreationKey(`subtitle-generate:${timeline.id}`);
    await mutation.mutate(
      (signal) => subtitleApi.generate(episode, timeline.id, key.key, signal),
      (track) => {
        key.clear();
        list.prepend(track);
        select(track.id);
      },
      "字幕候选已生成。请核对、批准后加入时间轴草稿。",
    );
  }
  return (
    <section
      aria-label="字幕版本与编辑"
      className="flex min-w-0 flex-col gap-3 border-t pt-4"
    >
      <h4>字幕文字与时点</h4>
      <p className="text-sm text-muted-foreground">
        沿用句级字幕生产与审核。文字或时点修改另存字幕版本，批准后替换草稿中的字幕轨；保存时间轴后再渲染。
      </p>
      {list.rows.length > 0 && (
        <label className="flex flex-col gap-1 text-sm">
          字幕版本
          <select
            aria-label="字幕版本"
            className={selectClass}
            value={selected}
            onChange={(e) => select(e.target.value)}
          >
            {selected && !list.rows.some((row) => row.id === selected) && (
              <option value={selected}>时间轴精确字幕来源</option>
            )}
            {list.rows.map((row) => (
              <option key={row.id} value={row.id}>
                字幕 v{row.version_no} · {row.status}
              </option>
            ))}
          </select>
        </label>
      )}
      <div className="flex flex-wrap gap-2">
        {writable && (
          <Button
            variant="outline"
            disabled={mutation.busy || timeline.stale}
            onClick={() => void generate()}
          >
            从保存版本配音生成字幕
          </Button>
        )}
        <Button
          variant="outline"
          disabled={list.loading || detail.loading}
          onClick={() => {
            void list.load();
            if (selected) void detail.load();
          }}
        >
          刷新字幕版本
        </Button>
        {list.hasMore && (
          <Button
            variant="outline"
            disabled={list.loading}
            onClick={() => void list.load(true)}
          >
            加载更多字幕版本
          </Button>
        )}
      </div>
      {(list.loading || detail.loading) && (
        <p role="status">正在核对字幕来源…</p>
      )}
      {(list.error || detail.error || mutation.error) && (
        <Notice tone="error">
          {list.error || detail.error || mutation.error}
        </Notice>
      )}
      {mutation.notice && <Notice tone="success">{mutation.notice}</Notice>}
      {detail.data && (
        <SubtitleEdit
          key={detail.data.track.id}
          {...detail.data}
          episode={episode}
          projectId={projectId}
          owner={owner}
          writable={writable}
          unavailable={unavailable}
          onRefresh={() => void detail.load()}
          onApply={onApply}
          onCreated={(track) => {
            list.prepend(track);
            select(track.id);
          }}
        />
      )}
    </section>
  );
}

function SubtitleEdit({
  track,
  asset,
  version,
  episode,
  projectId,
  owner,
  writable,
  unavailable,
  onRefresh,
  onApply,
  onCreated,
}: Awaited<ReturnType<typeof subtitleApi.detail>> & {
  episode: string;
  projectId: string;
  owner: boolean;
  writable: boolean;
  unavailable: (cause: unknown) => void;
  onRefresh: () => void;
  onApply: (clips: TimelineClip[]) => void;
  onCreated: (track: SubtitleTrack) => void;
}) {
  const initial = track.cues.map((cue) => ({
    cue_no: cue.cue_no,
    text: cue.text,
    start_ms: cue.start_ms,
    end_ms: cue.start_ms + cue.duration_ms,
  }));
  const draft = useSessionDraft({
    id: `subtitle:${track.id}:${track.checksum}`,
    episodeId: episode,
    label: `字幕 v${track.version_no}`,
    initial,
    validate: (value): value is SubtitleChange[] =>
      Array.isArray(value) &&
      value.length === initial.length &&
      value.every(
        (cue, i) =>
          cue &&
          cue.cue_no === initial[i].cue_no &&
          typeof cue.text === "string" &&
          Number.isFinite(cue.start_ms) &&
          Number.isFinite(cue.end_ms),
      ),
  });
  const [index, setIndex] = useState(0);
  const cue = draft.value[index];
  const scope = `subtitle-edit:${track.id}:${JSON.stringify(draft.value)}`;
  const mutation = useRenderMutation(scope, writable, unavailable);
  const readOnly =
    !writable ||
    [asset.status, version.status].some((s) =>
      ["LOCKED", "ARCHIVED"].includes(s),
    ) ||
    version.stale ||
    track.stale;
  const invalid = draft.value.some(
    (row, i, rows) =>
      !row.text.trim() ||
      row.text.length > 2000 ||
      !Number.isInteger(row.start_ms) ||
      !Number.isInteger(row.end_ms) ||
      row.start_ms < 0 ||
      row.end_ms <= row.start_ms ||
      (i > 0 && row.start_ms < rows[i - 1].end_ms),
  );
  async function save() {
    const changes = draft.value.filter(
      (row, i) => JSON.stringify(row) !== JSON.stringify(initial[i]),
    );
    const key = pendingCreationKey(scope);
    await mutation.mutate(
      (signal) =>
        subtitleApi.edit(track.id, asset.revision, changes, key.key, signal),
      (saved) => {
        key.clear();
        draft.clear();
        onCreated(saved);
      },
      "字幕修改已另存候选，原文字、配音和字幕历史保留。",
    );
  }
  async function state(status: string) {
    await mutation.mutate(
      (signal) =>
        subtitleApi.state(projectId, track, version.revision, status, signal),
      onRefresh,
      "字幕审核状态已更新。",
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <p className="text-sm">
        字幕 v{track.version_no} · {version.status}
        {track.stale ? " · 已失效" : ""}
      </p>
      {draft.recovered && <Notice>已恢复此字幕版本的本机修改。</Notice>}
      {draft.invalid && (
        <Notice tone="error">字幕草稿损坏，请放弃后重新编辑。</Notice>
      )}
      <select
        aria-label="编辑字幕句"
        className={selectClass}
        value={index}
        onChange={(e) => setIndex(Number(e.target.value))}
      >
        {draft.value.map((row, i) => (
          <option key={row.cue_no} value={i}>
            {row.cue_no} · {row.text.slice(0, 30)}
          </option>
        ))}
      </select>
      {cue && (
        <fieldset
          className="flex min-w-0 flex-col gap-3"
          disabled={readOnly || mutation.busy}
        >
          <label className="text-sm">
            字幕显示文字
            <Textarea
              aria-label="字幕显示文字"
              value={cue.text}
              maxLength={2000}
              onChange={(e) =>
                draft.update(
                  draft.value.map((row, i) =>
                    i === index ? { ...row, text: e.target.value } : row,
                  ),
                )
              }
            />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <NumberField
              label="字幕出现（毫秒）"
              value={cue.start_ms}
              onChange={(start_ms) =>
                draft.update(
                  draft.value.map((row, i) =>
                    i === index ? { ...row, start_ms } : row,
                  ),
                )
              }
            />
            <NumberField
              label="字幕消失（毫秒）"
              value={cue.end_ms}
              min={1}
              onChange={(end_ms) =>
                draft.update(
                  draft.value.map((row, i) =>
                    i === index ? { ...row, end_ms } : row,
                  ),
                )
              }
            />
          </div>
        </fieldset>
      )}
      {invalid && (
        <Notice tone="error">
          字幕文字不能为空；起止须为有效毫秒，结束晚于开始且相邻句不能重叠。
        </Notice>
      )}
      <div className="flex flex-wrap gap-2">
        {writable && (
          <>
            <Button
              disabled={
                readOnly ||
                mutation.busy ||
                !draft.dirty ||
                invalid ||
                draft.invalid
              }
              onClick={() => void save()}
            >
              另存字幕修改
            </Button>
            <Button
              variant="outline"
              disabled={mutation.busy || !draft.dirty}
              onClick={draft.clear}
            >
              放弃字幕本机修改
            </Button>
            {version.status === "DRAFT" && (
              <Button
                variant="outline"
                disabled={mutation.busy || draft.dirty || readOnly}
                onClick={() => void state("READY_FOR_REVIEW")}
              >
                提交字幕审核
              </Button>
            )}
            {owner && version.status === "READY_FOR_REVIEW" && (
              <Button
                disabled={mutation.busy || draft.dirty || readOnly}
                onClick={() => void state("APPROVED")}
              >
                批准此字幕版本
              </Button>
            )}
            <Button
              variant="outline"
              disabled={
                mutation.busy ||
                draft.dirty ||
                track.stale ||
                version.stale ||
                !["APPROVED", "LOCKED"].includes(version.status)
              }
              onClick={() => onApply(track.clips)}
            >
              将此字幕版本加入草稿
            </Button>
          </>
        )}
      </div>
      {mutation.error && (
        <Notice tone="error">
          {mutation.error} 本机修改已保留，请刷新字幕版本后核对。
        </Notice>
      )}
      {mutation.notice && <Notice tone="success">{mutation.notice}</Notice>}
      <details>
        <summary>字幕文字与原配音来源</summary>
        <pre>
          {JSON.stringify(
            {
              id: track.id,
              checksum: track.checksum,
              cues: track.source_cues ?? track.cues,
            },
            null,
            2,
          )}
        </pre>
      </details>
    </div>
  );
}
