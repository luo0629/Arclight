"use client";

import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import {
  FormLabel,
  LoadingState,
  Notice,
  StatusBadge,
} from "@/components/ui-kit";
import Dialog from "@/components/shared/app-dialog";
import VersionImage from "@/features/assets/components/version-image";
import ContinuityComparisonButton from "@/features/continuity/components/continuity-comparison";
import { api, message } from "@/lib/api/client";
import useShotProduction from "../hooks/use-shot-production";
import {
  batchActions,
  budgetTotal,
  type BatchAction,
  type ImageConfiguration,
} from "../shot-production-types";

function ProductionBody({
  projectId,
  episodeId,
  storyboardId,
}: {
  projectId: string;
  episodeId: string;
  storyboardId: string;
}) {
  const id = useId();
  const [offset, setOffset] = useState(0);
  const {
    data,
    batch,
    busy,
    error,
    storageBlocked,
    prepare,
    execute,
    refresh,
    unavailable,
  } = useShotProduction(projectId, episodeId, storyboardId, offset);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [candidates, setCandidates] = useState<Record<string, string>>({});
  const [action, setAction] = useState<BatchAction>("generate");
  const [comment, setComment] = useState("");
  const [provider, setProvider] = useState("mock");
  const [config, setConfig] = useState<ImageConfiguration>({
    provider: "mock",
    model: "mock-image-v1",
    model_config_version: "shot-production-ui/1",
    batch_size: 1,
    output_spec: { width: 1024, height: 1024, format: "png" },
  });
  const [configLoading, setConfigLoading] = useState(false);
  const [configError, setConfigError] = useState("");
  const [maximumBatch, setMaximumBatch] = useState(4);
  const frozen = batch?.items.some((item) => item.phase === "unknown") ?? false;
  const disabled = busy || configLoading || storageBlocked;
  async function chooseProvider(value: string) {
    setProvider(value);
    setConfigError("");
    if (value === "mock") {
      setMaximumBatch(4);
      setConfig((current) => ({
        ...current,
        provider: "mock",
        model: "mock-image-v1",
        model_config_version: "shot-production-ui/1",
        batch_size: Math.min(current.batch_size, 4),
      }));
      return;
    }
    setConfigLoading(true);
    try {
      const next = await api<{
        enabled: boolean;
        provider: string;
        model: string;
        configuration_version: string;
        message?: string;
        capabilities?: { max_batch_size: number };
      }>(`/projects/${projectId}/generation-jobs/services/image/configuration`);
      if (!next.enabled)
        throw new Error(next.message ?? "图像服务未启用或缺少可信报价。");
      const maximum = next.capabilities?.max_batch_size ?? 1;
      setMaximumBatch(maximum);
      setConfig((current) => ({
        ...current,
        provider: next.provider,
        model: next.model,
        model_config_version: next.configuration_version,
        batch_size: Math.min(current.batch_size, maximum),
      }));
    } catch (cause) {
      setConfigError(message(cause));
      unavailable(cause);
    } finally {
      setConfigLoading(false);
    }
  }
  if (!data)
    return (
      <div className="flex flex-col gap-3">
        {error ? (
          <Notice tone="error" role="alert">
            {error}
          </Notice>
        ) : (
          <LoadingState>读取精确镜头生产状态</LoadingState>
        )}
        <Button variant="outline" onClick={() => void refresh()}>
          重新读取生产状态
        </Button>
      </div>
    );
  const selectedCount = data.shots.filter((shot) =>
    selected.has(shot.id),
  ).length;
  const ready =
    batch?.items.filter((item) =>
      ["ready", "unknown", "failed"].includes(item.phase),
    ).length ?? 0;
  const accepted =
    batch?.items.filter((item) => item.phase === "accepted").length ?? 0;
  const total = batch ? budgetTotal(batch.items) : null;
  return (
    <section
      className="flex min-w-0 flex-col gap-5"
      aria-label="镜头批量生产工作区"
    >
      <p className="text-sm text-muted-foreground">
        从已保存的分镜读取镜头与精确候选。生成结果先进入连续性审查，批准后仍须单独正式选用。只重试明确失败的原任务，已成功镜头保留结果；未知结果请在任务中心先对账。
      </p>
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      {!data.writable && <Notice>当前项目角色或归档状态只允许查看。</Notice>}
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm">
          共 {data.total} 个镜头 · 本页已选择 {selectedCount} 个
        </p>
        <Button
          variant="outline"
          disabled={disabled}
          onClick={() => void refresh()}
        >
          刷新生产状态
        </Button>
      </div>
      <div
        className="max-w-full overflow-x-auto rounded-lg border"
        role="region"
        aria-label="镜头生产列表滚动区域"
        tabIndex={0}
      >
        <table
          className="w-full min-w-[44rem] text-left text-sm"
          aria-label="镜头生产列表"
        >
          <thead className="border-b bg-secondary/50">
            <tr className="[&>th]:p-3">
              <th scope="col">
                <Input
                  type="checkbox"
                  className="size-5"
                  aria-label="选择本页全部生产镜头"
                  disabled={disabled}
                  checked={
                    selectedCount > 0 && selectedCount === data.shots.length
                  }
                  ref={(element) => {
                    if (element)
                      element.indeterminate =
                        selectedCount > 0 && selectedCount < data.shots.length;
                  }}
                  onChange={(event) =>
                    setSelected(
                      event.target.checked
                        ? new Set(data.shots.map((shot) => shot.id))
                        : new Set(),
                    )
                  }
                />
              </th>
              <th scope="col">镜头与画面</th>
              <th scope="col">生成任务</th>
              <th scope="col">精确候选与审查</th>
            </tr>
          </thead>
          <tbody>
            {data.shots.map((shot) => {
              const candidate = shot.candidates.find(
                (row) => row.id === candidates[shot.id],
              );
              return (
                <tr
                  key={shot.id}
                  data-production-shot={shot.id}
                  className="border-b align-top last:border-0 [&>td]:p-3 [&>th]:p-3"
                >
                  <td>
                    <Input
                      type="checkbox"
                      className="size-5"
                      aria-label={`选择生产镜头 ${shot.shot_no}`}
                      disabled={disabled}
                      checked={selected.has(shot.id)}
                      onChange={(event) =>
                        setSelected((previous) => {
                          const next = new Set(previous);
                          if (event.target.checked) next.add(shot.id);
                          else next.delete(shot.id);
                          return next;
                        })
                      }
                    />
                  </td>
                  <th scope="row" className="max-w-64 font-normal">
                    <strong>
                      镜头 {String(shot.shot_no).padStart(2, "0")}
                    </strong>
                    <p className="mt-2 whitespace-pre-wrap">
                      {shot.description}
                    </p>
                    <p className="mt-2 break-all text-xs text-muted-foreground">
                      {shot.id}
                    </p>
                  </th>
                  <td className="min-w-44">
                    {shot.job ? (
                      <div className="flex flex-col gap-2">
                        <StatusBadge status={shot.job.status} />
                        <p>已尝试 {shot.job.attempt_count} 次</p>
                        <p className="break-all text-xs text-muted-foreground">
                          {shot.job.id}
                        </p>
                      </div>
                    ) : (
                      <span>尚未生成</span>
                    )}
                    {shot.blocker && (
                      <p className="mt-2 text-destructive">
                        {shot.blocker.message}
                      </p>
                    )}
                  </td>
                  <td className="min-w-56">
                    {shot.candidates.length ? (
                      <div className="flex flex-col gap-3">
                        <FormLabel htmlFor={`${id}-candidate-${shot.id}`}>
                          镜头 {shot.shot_no} 审核候选
                          <NativeSelect
                            id={`${id}-candidate-${shot.id}`}
                            aria-label={`镜头 ${shot.shot_no} 审核候选`}
                            value={candidates[shot.id] ?? ""}
                            disabled={disabled}
                            onChange={(event) =>
                              setCandidates((previous) => ({
                                ...previous,
                                [shot.id]: event.target.value,
                              }))
                            }
                          >
                            <NativeSelectOption value="">
                              选择并查看精确候选
                            </NativeSelectOption>
                            {shot.candidates.map((row) => (
                              <NativeSelectOption key={row.id} value={row.id}>
                                v{row.version_no} · {row.status} ·{" "}
                                {row.version_id.slice(0, 8)}
                              </NativeSelectOption>
                            ))}
                          </NativeSelect>
                        </FormLabel>
                        {candidate && (
                          <>
                            <VersionImage
                              key={candidate.version_id}
                              path={candidate.image_path}
                              compact
                              alt={`镜头 ${shot.shot_no} 精确候选 ${candidate.version_id}`}
                              onUnavailable={unavailable}
                            />
                            <StatusBadge status={candidate.status} />
                            <p className="break-all text-xs text-muted-foreground">
                              精确版本：{candidate.version_id}
                            </p>
                            {candidate.blocker && (
                              <p className="text-destructive">
                                {candidate.blocker.message}
                              </p>
                            )}
                            <ContinuityComparisonButton
                              projectId={projectId}
                              assetId={candidate.asset_id}
                              versionId={candidate.version_id}
                              onChanged={refresh}
                            />
                          </>
                        )}
                        {shot.candidates_truncated && (
                          <p className="text-xs text-muted-foreground">
                            此处显示最近16张；完整历史请从逐图比较和资产中心查看。
                          </p>
                        )}
                      </div>
                    ) : (
                      <span className="text-muted-foreground">
                        暂无可用图片候选
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <nav
        className="flex flex-wrap items-center gap-3"
        aria-label="生产镜头分页"
      >
        <Button
          variant="outline"
          disabled={disabled || offset === 0}
          onClick={() => {
            setSelected(new Set());
            setOffset(Math.max(0, offset - 50));
          }}
        >
          上一页镜头
        </Button>
        <span className="text-sm">第 {Math.floor(offset / 50) + 1} 页</span>
        <Button
          variant="outline"
          disabled={disabled || data.next_offset === null}
          onClick={() => {
            setSelected(new Set());
            setOffset(data.next_offset!);
          }}
        >
          下一页镜头
        </Button>
      </nav>
      <fieldset
        className="flex min-w-0 flex-col gap-4 border-t pt-4"
        disabled={disabled || !data.writable || frozen}
      >
        <legend className="font-semibold">
          批量操作 · 已选择 {selectedCount} 个镜头
        </legend>
        <FormLabel htmlFor={`${id}-action`}>
          生产操作
          <NativeSelect
            id={`${id}-action`}
            aria-label="生产操作"
            value={action}
            onChange={(event) => setAction(event.target.value as BatchAction)}
          >
            {Object.entries(batchActions).map(([value, label]) => (
              <NativeSelectOption
                key={value}
                value={value}
                disabled={!data.owner && ["approve", "reject"].includes(value)}
              >
                {label}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </FormLabel>
        {action === "generate" && (
          <>
            <FormLabel htmlFor={`${id}-provider`}>
              镜头图像服务
              <NativeSelect
                id={`${id}-provider`}
                aria-label="镜头图像服务"
                value={provider}
                onChange={(event) => void chooseProvider(event.target.value)}
              >
                <NativeSelectOption value="mock">
                  本地 Mock（合成图片）
                </NativeSelectOption>
                <NativeSelectOption value="configured">
                  我的图像服务（或服务器默认）
                </NativeSelectOption>
              </NativeSelect>
            </FormLabel>
            {configError && (
              <Notice tone="error" role="alert">
                {configError}
              </Notice>
            )}
            <p className="break-all text-sm text-muted-foreground">
              模型：{config.model}
            </p>
            <div className="grid min-w-0 gap-3 sm:grid-cols-3">
              <FormLabel htmlFor={`${id}-count`}>
                每镜头候选数
                <Input
                  id={`${id}-count`}
                  type="number"
                  min={1}
                  max={maximumBatch}
                  value={config.batch_size}
                  onChange={(event) =>
                    setConfig({
                      ...config,
                      batch_size: Number(event.target.value),
                    })
                  }
                />
              </FormLabel>
              {(["width", "height"] as const).map((dimension) => (
                <FormLabel key={dimension} htmlFor={`${id}-${dimension}`}>
                  图片{dimension === "width" ? "宽度" : "高度"}
                  <Input
                    id={`${id}-${dimension}`}
                    type="number"
                    min={1}
                    max={8192}
                    value={config.output_spec[dimension]}
                    onChange={(event) =>
                      setConfig({
                        ...config,
                        output_spec: {
                          ...config.output_spec,
                          [dimension]: Number(event.target.value),
                        },
                      })
                    }
                  />
                </FormLabel>
              ))}
            </div>
          </>
        )}
        {["approve", "reject"].includes(action) && (
          <FormLabel htmlFor={`${id}-comment`}>
            批量审核意见（必填）
            <Textarea
              id={`${id}-comment`}
              maxLength={1000}
              value={comment}
              onChange={(event) => setComment(event.target.value)}
            />
          </FormLabel>
        )}
        <Button
          variant="outline"
          disabled={
            !selectedCount ||
            (action === "generate" && !!configError) ||
            (["approve", "reject"].includes(action) && !comment.trim())
          }
          onClick={() =>
            void prepare(action, selected, candidates, config, comment)
          }
        >
          预览数量与预算
        </Button>
      </fieldset>
      {batch && (
        <section
          className="flex min-w-0 flex-col gap-3 rounded-lg border bg-secondary/20 p-4"
          aria-label="批量提交预览与结果"
          aria-live="polite"
        >
          <h3 className="font-semibold">
            {batchActions[batch.action]} · {batch.items.length} 个镜头
          </h3>
          <p>
            本次可提交{" "}
            {batch.items.filter((item) => item.phase !== "blocked").length} 项 ·
            未提交{" "}
            {batch.items.filter((item) => item.phase === "blocked").length} 项
          </p>
          <p>
            预算上限估算：
            {total === null ? "无法完整估算，请核对受阻项目" : `${total} USD`}
          </p>
          <p className="text-sm text-muted-foreground">
            估算对应本次冻结的请求；每项提交时再次检查预算与当前状态。审核不新增生成费用，失败任务重试仍可能产生费用。
          </p>
          {batch.comment && (
            <p className="whitespace-pre-wrap">审核意见：{batch.comment}</p>
          )}
          <ul className="flex flex-col gap-2">
            {batch.items.map((item) => (
              <li
                key={item.key}
                className="min-w-0 border-t pt-2"
                data-batch-shot={item.shot_id}
              >
                <strong>镜头 {item.shot_no}</strong>
                <span>
                  {" "}
                  ·{" "}
                  {item.phase === "accepted"
                    ? "已受理"
                    : item.phase === "blocked"
                      ? "未提交"
                      : item.phase === "failed"
                        ? "提交失败"
                        : item.phase === "unknown"
                          ? "结果待确认"
                          : "等待确认"}{" "}
                  · {item.cost === null ? "无可用报价" : `${item.cost} USD`}
                </span>
                <p className="break-all text-sm">{item.result}</p>
                {item.candidate && (
                  <p className="break-all text-xs text-muted-foreground">
                    候选版本：{item.candidate.version_id}
                  </p>
                )}
              </li>
            ))}
          </ul>
          <p role="status">
            已受理 {accepted} 项 · 提交失败{" "}
            {batch.items.filter((item) => item.phase === "failed").length} 项 ·
            待确认{" "}
            {batch.items.filter((item) => item.phase === "unknown").length} 项
          </p>
          <Button
            disabled={disabled || !data.writable || !ready || total === null}
            onClick={() => void execute()}
          >
            {batch.items.some((item) => item.attempted)
              ? "继续未完成提交"
              : "确认逐项提交"}
          </Button>
        </section>
      )}
    </section>
  );
}

export default function ShotProductionButton({
  projectId,
  episodeId,
  storyboardId,
}: {
  projectId: string;
  episodeId: string;
  storyboardId: string;
}) {
  const [open, setOpen] = useState(false);
  const title = useId();
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)}>
        批量生产镜头
      </Button>
      {open && (
        <Dialog
          busy={false}
          labelledBy={title}
          onDismiss={() => setOpen(false)}
          className="w-[calc(100vw-2rem)] sm:max-w-6xl"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 id={title} className="text-lg font-semibold">
              镜头批量生成、审核与重试
            </h2>
            <Button variant="outline" onClick={() => setOpen(false)}>
              关闭批量生产
            </Button>
          </div>
          <ProductionBody
            key={`${projectId}:${episodeId}:${storyboardId}`}
            projectId={projectId}
            episodeId={episodeId}
            storyboardId={storyboardId}
          />
        </Dialog>
      )}
    </>
  );
}
