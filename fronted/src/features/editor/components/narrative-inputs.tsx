"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FormLabel, Notice } from "@/components/ui-kit";
import { api, json, message } from "@/lib/api/client";
import usePagedList from "@/hooks/use-paged-list";
import ListPagination from "@/components/shared/list-pagination";
import type { Episode, StoryEntry } from "@/types/api";
import type { LedgerHead } from "@/features/story/story-ledger";
import {
  emptyNarrativeRequest,
  narrativeRequestProblem,
  type NarrativeCapture,
  type NarrativeRequest,
} from "../script-narrative";

export default function NarrativeInputs({
  episode,
  value,
  onChange,
  disabled,
  required = false,
}: {
  episode: Episode;
  value: NarrativeRequest | null;
  onChange: (value: NarrativeRequest | null) => void;
  disabled: boolean;
  required?: boolean;
}) {
  const id = useId();
  const entries = usePagedList<StoryEntry>(
    `/projects/${episode.project_id}/story-entries`,
  );
  const [preview, setPreview] = useState<{
    signature: string;
    data: NarrativeCapture;
  } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const signature = JSON.stringify([episode.project_id, episode.id, value]);
  const current = useRef(signature);
  current.current = signature;
  useEffect(() => {
    setLoading(false);
    setError("");
    setPreview(null);
    return () => controller.current?.abort();
  }, [episode.project_id, episode.id]);
  function change(next: NarrativeRequest | null) {
    controller.current?.abort();
    setLoading(false);
    setError("");
    setPreview(null);
    onChange(next);
  }
  async function load(headOnly = false) {
    if (
      !value ||
      disabled ||
      loading ||
      (!headOnly && narrativeRequestProblem(value))
    )
      return;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    const key = signature;
    setLoading(true);
    setError("");
    try {
      if (headOnly) {
        const head = await api<LedgerHead>(
          `/projects/${episode.project_id}/story-ledger`,
          { signal: abort.signal },
        );
        if (!abort.signal.aborted && current.current === key)
          change({
            ...value,
            ledger_version_id: head.approved_version_id,
            expected_ledger_head_revision: head.revision,
          });
      } else {
        const data = await api<NarrativeCapture>(
          `/episodes/${episode.id}/writing-context`,
          { ...json("POST", value), signal: abort.signal },
        );
        if (!abort.signal.aborted && current.current === key)
          setPreview({ signature: key, data });
      }
    } catch (cause) {
      if (!abort.signal.aborted && current.current === key)
        setError(message(cause));
    } finally {
      if (!abort.signal.aborted && current.current === key) setLoading(false);
    }
  }
  const shown = preview?.signature === signature ? preview.data : null;
  return (
    <section
      aria-label="连续剧写作依据"
      className="grid min-w-0 gap-3 border-y py-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-medium">连续剧写作依据</h4>
        {!required && (
          <Button
            type="button"
            variant="outline"
            disabled={disabled}
            onClick={() => change(value ? null : emptyNarrativeRequest())}
          >
            {value ? "停用连续剧写作" : "使用全剧规划与时点事实"}
          </Button>
        )}
      </div>
      {value && (
        <>
          <p className="text-sm text-muted-foreground">
            从全剧规划与账本详情复制精确版本。未来计划仅供作者规划，人物对白与行动须遵守该时点所知；预览不会提交事实。
          </p>
          <FormLabel htmlFor={id + "plan"}>已批准全剧规划版本</FormLabel>
          <Input
            id={id + "plan"}
            value={value.series_plan_version_id}
            disabled={disabled || loading}
            onChange={(e) =>
              change({
                ...value,
                series_plan_version_id: e.target.value.trim(),
              })
            }
          />
          <FormLabel htmlFor={id + "ledger"}>
            已批准账本版本（尚无批准账本时留空）
          </FormLabel>
          <Input
            id={id + "ledger"}
            value={value.ledger_version_id ?? ""}
            disabled={disabled || loading}
            onChange={(e) =>
              change({
                ...value,
                ledger_version_id: e.target.value.trim() || null,
              })
            }
          />
          <div className="grid min-w-0 gap-3 sm:grid-cols-3">
            {(
              [
                ["账本基准修订", "expected_ledger_head_revision"],
                ["写作目标故事时点", "target_story_time"],
                ["写作目标场景编号", "target_scene_no"],
              ] as const
            ).map(([label, key]) => (
              <FormLabel key={key}>
                {label}
                <Input
                  type="number"
                  step="1"
                  value={value[key]}
                  disabled={disabled || loading}
                  onChange={(e) =>
                    change({ ...value, [key]: Number(e.target.value) })
                  }
                />
              </FormLabel>
            ))}
          </div>
          <Button
            type="button"
            variant="outline"
            className="justify-self-start"
            disabled={disabled || loading}
            onClick={() => void load(true)}
          >
            读取当前批准账本基准
          </Button>
          <fieldset disabled={disabled || loading} className="grid gap-2">
            <legend className="mb-2 text-sm font-medium">
              需要隔离知情范围的故事角色
            </legend>
            {entries.rows
              .filter((entry) => entry.kind === "CHARACTER" && !entry.archived)
              .map((entry) => (
                <label
                  key={entry.id}
                  className="flex items-start gap-2 text-sm"
                >
                  <input
                    type="checkbox"
                    checked={value.character_ids.includes(entry.id)}
                    onChange={(e) =>
                      change({
                        ...value,
                        character_ids: e.target.checked
                          ? [...value.character_ids, entry.id]
                          : value.character_ids.filter((v) => v !== entry.id),
                      })
                    }
                  />
                  {entry.name}
                </label>
              ))}
            {value.character_ids
              .filter((id) => !entries.rows.some((e) => e.id === id))
              .map((id) => (
                <p key={id} className="break-all text-xs">
                  已绑定角色 {id}（目录尚未加载或已归档，服务端会复验）
                </p>
              ))}
          </fieldset>
          <ListPagination
            {...entries}
            label="写作角色"
            count={entries.rows.length}
          />
          {narrativeRequestProblem(value) && (
            <p className="text-sm text-muted-foreground">
              {narrativeRequestProblem(value)}
            </p>
          )}
          <Button
            type="button"
            variant="outline"
            className="justify-self-start"
            disabled={disabled || loading || !!narrativeRequestProblem(value)}
            onClick={() => void load()}
          >
            {loading ? "读取中…" : "预览精确写作依据"}
          </Button>
        </>
      )}
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      {shown && (
        <div
          role="region"
          aria-label="写作依据预览"
          className="grid min-w-0 gap-3"
        >
          {shown.character_projections_truncated && (
            <Notice>
              仅预览前8位角色的知情范围；完整冻结角色以精确写作上下文为准。
            </Notice>
          )}
          {[shown.projection, ...shown.character_projections].map(
            (projection, index) => (
              <details key={index} open={index === 0} className="min-w-0">
                <summary>
                  {projection.perspective === "AUTHOR"
                    ? "作者计划与已确认事实"
                    : `角色所知 · ${entries.rows.find((e) => e.id === projection.character_id)?.name ?? projection.character_id}`}
                </summary>
                <p className="text-sm">
                  第 {projection.target.episode_no} 集 · 场景{" "}
                  {projection.target.scene_no} · 故事时点{" "}
                  {projection.target.story_time}
                </p>
                {projection.plan && (
                  <details>
                    <summary>作者全剧计划（尚未发生）</summary>
                    <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">
                      {JSON.stringify(projection.plan, null, 2)}
                    </pre>
                  </details>
                )}
                {(
                  [
                    ["已确认事实", projection.facts],
                    ["作者计划", projection.planned],
                    ["未确认提案", projection.proposals],
                  ] as const
                ).map(([label, values]) => (
                  <div key={label}>
                    <h5 className="mt-2 text-sm font-medium">{label}</h5>
                    {values.length ? (
                      values.map((entry) => (
                        <p key={entry.id} className="break-words text-sm">
                          {entry.state_key}：{entry.value}
                        </p>
                      ))
                    ) : (
                      <p className="text-sm text-muted-foreground">无记录</p>
                    )}
                  </div>
                ))}
                {projection.causal_summaries.map((row) => (
                  <p key={row.script_version_id} className="text-sm">
                    第 {row.episode_no} 集因果摘要：{row.summary}
                    {row.truncated && "（摘要已裁剪）"}
                  </p>
                ))}
                {projection.truncated && (
                  <Notice>
                    结果已按上下文上限裁剪，未展示的内容不代表不存在。
                  </Notice>
                )}
                {projection.index_truncated && (
                  <Notice>
                    全剧索引尚有未展示内容，当前预览并非完整索引。
                  </Notice>
                )}
                {projection.related_truncated && (
                  <Notice>
                    相关远集已按上下文上限裁剪，未展示的集数不代表没有因果关联。
                  </Notice>
                )}
                {!!projection.related_missing?.length && (
                  <Notice>
                    规划引用的第{" "}
                    {projection.related_missing
                      .map((row) => row.episode_no)
                      .join("、")}{" "}
                    集尚无批准剧本，不能作为已确认剧情。
                  </Notice>
                )}
                {projection.plan?.truncated === true && (
                  <Notice>
                    作者规划已按上下文上限裁剪，请结合精确规划版本核对。
                  </Notice>
                )}
                <details>
                  <summary>精确来源与摘要校验</summary>
                  <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">
                    {JSON.stringify(
                      {
                        sources: projection.sources,
                        result_sha256: projection.result_sha256,
                      },
                      null,
                      2,
                    )}
                  </pre>
                </details>
              </details>
            ),
          )}
        </div>
      )}
    </section>
  );
}
