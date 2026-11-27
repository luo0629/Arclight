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
import { api, message } from "@/lib/api/client";
import useShotProduction from "../hooks/use-shot-production";
import ShotImageCandidates from "./shot-image-candidates";
import ShotVideoCandidates from "@/features/video/components/shot-video-candidates";
import {
  batchActions,
  budgetTotal,
  type BatchAction,
  type ImageConfiguration,
  type ProductionShot,
} from "../shot-production-types";

function ShotJobStatus({ shot }: { shot: ProductionShot }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      {shot.job ? (
        <>
          <StatusBadge status={shot.job.status} />
          <span>已尝试 {shot.job.attempt_count} 次</span>
        </>
      ) : (
        <span className="text-muted-foreground">尚未提交镜头图像任务</span>
      )}
      {shot.blocker && (
        <p className="basis-full text-destructive">{shot.blocker.message}</p>
      )}
    </div>
  );
}

export function ProductionBody({
  projectId,
  episodeId,
  storyboardId,
  shotId,
  onChanged,
}: {
  projectId: string;
  episodeId: string;
  storyboardId: string;
  shotId?: string;
  onChanged?: () => Promise<void>;
}) {
  const id = useId();
  const [offset, setOffset] = useState(0);
  const {
    data,
    batch,
    pendingOtherShots,
    busy,
    error,
    storageBlocked,
    prepare,
    execute,
    refresh,
    unavailable,
  } = useShotProduction(projectId, episodeId, storyboardId, offset, shotId);
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(shotId ? [shotId] : []),
  );
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
  async function refreshMedia() {
    await refresh();
    await onChanged?.();
  }
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
      if (!next.capabilities) throw new Error("图像服务没有声明批量生成能力。");
      const maximum = next.capabilities.max_batch_size;
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
  const exactShot = shotId ? data.shots[0] : undefined;
  const exactCandidate =
    exactShot &&
    (candidates[exactShot.id] ??
      exactShot.candidates.find(
        (row) => row.version_id === exactShot.selected_image_version_id,
      )?.id ??
      exactShot.candidates[0]?.id ??
      "");

  function imageCandidates(shot: ProductionShot, selectedId: string) {
    return (
      <ShotImageCandidates
        projectId={projectId}
        episodeId={episodeId}
        storyboardId={storyboardId}
        shot={shot}
        selectedId={selectedId}
        disabled={disabled}
        writable={data?.writable ?? false}
        onSelect={(value) =>
          setCandidates((previous) => ({ ...previous, [shot.id]: value }))
        }
        onChanged={refreshMedia}
        onUnavailable={unavailable}
      />
    );
  }
  return (
    <section
      className="flex min-w-0 flex-col gap-5"
      aria-label={exactShot ? undefined : "镜头批量生产工作区"}
    >
      {!exactShot && (
        <p className="text-sm text-muted-foreground">
          从已保存的分镜读取镜头与精确候选。生成结果先进入连续性审查，批准后仍须单独正式选用。只重试明确失败的原任务，已成功镜头保留结果；未知结果请在任务中心先对账。
        </p>
      )}
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      {!data.writable && <Notice>当前项目角色或归档状态只允许查看。</Notice>}
      <div className="flex flex-wrap items-center gap-3">
        {exactShot ? (
          <ShotJobStatus shot={exactShot} />
        ) : (
          <p className="text-sm">
            共 {data.total} 个镜头 · 本页已选择 {selectedCount} 个
          </p>
        )}
        <Button
          variant="outline"
          disabled={disabled}
          onClick={() => void refreshMedia()}
        >
          刷新生产状态
        </Button>
      </div>
      {exactShot ? (
        <section aria-label="图片候选" className="flex min-w-0 flex-col gap-3">
          <h4 className="font-semibold">图片候选</h4>
          {imageCandidates(exactShot, exactCandidate ?? "")}
        </section>
      ) : (
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
                          selectedCount > 0 &&
                          selectedCount < data.shots.length;
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
              {data.shots.map((shot) => (
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
                  </th>
                  <td className="min-w-44">
                    <ShotJobStatus shot={shot} />
                  </td>
                  <td className="min-w-56">
                    {imageCandidates(shot, candidates[shot.id] ?? "")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <section aria-label="镜头视频候选列表" className="min-w-0">
        {data.shots.map((shot) => (
          <ShotVideoCandidates
            key={shot.id}
            projectId={projectId}
            episodeId={episodeId}
            storyboardId={storyboardId}
            shotId={shot.id}
            shotNo={shot.shot_no}
            description={shot.description}
            writable={data.writable}
            candidates={shot.video_candidates}
            truncated={shot.video_candidates_truncated}
            onChanged={refreshMedia}
            unavailable={unavailable}
          />
        ))}
      </section>
      {!exactShot && (
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
      )}
      {pendingOtherShots && (
        <Notice>
          此分镜有其他镜头的提交待确认。请打开所属分镜，在批量生产中核对原提交，再开始新的图片操作。
        </Notice>
      )}
      {(!exactShot || data.writable) && (
        <fieldset
          className="flex min-w-0 flex-col gap-4 border-t pt-4"
          disabled={disabled || !data.writable || frozen || pendingOtherShots}
        >
          <legend className="font-semibold">
            {exactShot
              ? "镜头图片生成与审核"
              : `批量操作 · 已选择 ${selectedCount} 个镜头`}
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
                  disabled={
                    !data.owner && ["approve", "reject"].includes(value)
                  }
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
                    我的图像服务
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
              {exactShot ? "审核意见（必填）" : "批量审核意见（必填）"}
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
              void prepare(
                action,
                selected,
                exactShot
                  ? { [exactShot.id]: exactCandidate ?? "" }
                  : candidates,
                config,
                comment,
              )
            }
          >
            预览数量与费用
          </Button>
        </fieldset>
      )}
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
            预计费用：
            {total === null ? "部分项目暂无报价" : `${total} USD`}
          </p>
          <p className="text-sm text-muted-foreground">
            估算对应本次请求。失败任务重试可能产生供应商费用。
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
                  <p className="text-xs text-muted-foreground">
                    候选版本 v{item.candidate.version_no}
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
            onClick={() => void execute().then(() => onChanged?.())}
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
