"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
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
          <p>尚有一项发起请求待核验。生产目标已固定，刷新页面后仍会保留。</p>
          <p className="whitespace-pre-wrap break-words">
            原生产目标：{original.goal}
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
              payload: { goal },
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
        <div>
          <Button type="submit" disabled={locked || !goal.trim()}>
            {intent.mutating ? "正在发起生产计划…" : "发起生产计划"}
          </Button>
        </div>
      </form>
    </PagePanel>
  );
}
