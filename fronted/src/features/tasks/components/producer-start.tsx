"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FormLabel, Notice, PagePanel } from "@/components/ui-kit";
import { useAuth } from "@/features/auth/auth-context";
import { episodePath } from "@/lib/routes";
import { message } from "@/lib/api/client";
import useProducerIntent from "../hooks/use-producer-intent";
import type { ProducerAccepted, ProducerStartPayload } from "../producer-types";

type Props = {
  projectId: string;
  episodeId: string;
  writable: boolean;
  busy: boolean;
  reload: () => Promise<void>;
};

export default function ProducerStart(props: Props) {
  const { user } = useAuth();
  return (
    <StartForm
      key={`${user.id}:${props.projectId}:${props.episodeId}:${props.writable}`}
      {...props}
      userId={user.id}
    />
  );
}

function StartForm({
  userId,
  projectId,
  episodeId,
  writable,
  busy,
  reload,
}: Props & { userId: string }) {
  const router = useRouter();
  const [goal, setGoal] = useState("");
  const [maximumCost, setMaximumCost] = useState("");
  const [modelCalls, setModelCalls] = useState("32");
  const [delegations, setDelegations] = useState("8");
  const [refreshError, setRefreshError] = useState("");
  const intent = useProducerIntent({
    userId,
    projectId,
    episodeId,
    writable,
  });
  const original = intent.pending
    ? (JSON.parse(intent.pending.body) as ProducerStartPayload)
    : null;
  const locked =
    !writable || busy || !intent.ready || intent.mutating || !!intent.pending;

  async function accepted(result: ProducerAccepted | null) {
    if (!result) return;
    setGoal("");
    router.replace(
      `${episodePath(projectId, episodeId, "tasks")}&run=${result.run_id}`,
      { scroll: false },
    );
    try {
      await reload();
    } catch (cause) {
      setRefreshError(`生产任务已创建，列表刷新失败：${message(cause)}`);
    }
  }

  return (
    <PagePanel
      eyebrow="从目标到专业分工"
      title="Producer 生产协作"
      description="描述本集生产目标，由 Producer 调查现有版本并提出计划，再按依赖委派专业 Agent。正式版本继续沿用现有审核与批准流程。"
    >
      {!writable && <Notice>当前项目角色只可查看生产计划与进度。</Notice>}
      {intent.error && <Notice tone="error">{intent.error}</Notice>}
      {intent.notice && <Notice tone="success">{intent.notice}</Notice>}
      {refreshError && <Notice tone="error">{refreshError}</Notice>}
      {original && (
        <Notice>
          <p>
            尚有一项发起请求待核验。生产目标与预算已固定，刷新页面后仍会保留。
          </p>
          <p className="whitespace-pre-wrap break-words">
            原生产目标：{original.goal}
          </p>
          <p>
            原费用上限：{original.limits?.maximum_cost ?? "未单独设置"} USD ·
            原模型调用上限：{original.limits?.max_model_calls ?? 32}·
            原委派上限：{original.limits?.max_delegations ?? 8}
          </p>
          <Button
            type="button"
            variant="outline"
            disabled={!writable || busy || intent.mutating || !intent.ready}
            onClick={() => void intent.replay().then(accepted)}
          >
            重发原发起请求
          </Button>
        </Notice>
      )}
      <form
        className="flex min-w-0 flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (locked || !goal.trim()) return;
          setRefreshError("");
          void intent
            .submit({
              kind: "START",
              payload: {
                goal,
                limits: {
                  maximum_cost: maximumCost.trim() || null,
                  max_model_calls: Number(modelCalls),
                  max_delegations: Number(delegations),
                  max_duration_seconds: 3600,
                },
              },
            })
            .then(accepted);
        }}
      >
        <FormLabel>
          生产目标
          <Textarea
            value={original?.goal ?? goal}
            onChange={(event) => setGoal(event.target.value)}
            disabled={locked}
            maxLength={4000}
            required
            placeholder="例如：根据本集已批准大纲完成剧本、分镜与美术提案，并检查画面连续性。"
          />
        </FormLabel>
        <div className="grid gap-4 sm:grid-cols-3">
          <FormLabel>
            费用上限（USD，可选）
            <Input
              type="number"
              min="0"
              max="999999999999.999999"
              step="0.000001"
              value={
                original ? (original.limits?.maximum_cost ?? "") : maximumCost
              }
              onChange={(event) => setMaximumCost(event.target.value)}
              disabled={locked}
              placeholder="沿用项目预算"
            />
          </FormLabel>
          <FormLabel>
            模型调用上限
            <Input
              type="number"
              min="0"
              max="4096"
              step="1"
              required
              value={
                original ? (original.limits?.max_model_calls ?? 32) : modelCalls
              }
              onChange={(event) => setModelCalls(event.target.value)}
              disabled={locked}
            />
          </FormLabel>
          <FormLabel>
            专业 Agent 委派上限
            <Input
              type="number"
              min="0"
              max="128"
              step="1"
              required
              value={
                original ? (original.limits?.max_delegations ?? 8) : delegations
              }
              onChange={(event) => setDelegations(event.target.value)}
              disabled={locked}
            />
          </FormLabel>
        </div>
        <p className="text-xs leading-relaxed text-muted-foreground">
          本次协作最长 60
          分钟，费用与调用限制由服务端统一检查。未设置本次费用上限时，项目既有预算仍然生效。
        </p>
        <div>
          <Button type="submit" disabled={locked || !goal.trim()}>
            {intent.mutating ? "正在发起生产计划…" : "发起生产计划"}
          </Button>
        </div>
      </form>
    </PagePanel>
  );
}
