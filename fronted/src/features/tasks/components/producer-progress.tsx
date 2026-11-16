"use client";

import Link from "next/link";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { FormLabel, Notice, StatusBadge } from "@/components/ui-kit";
import { useAuth } from "@/features/auth/auth-context";
import { episodePath, isResourceId } from "@/lib/routes";
import { message } from "@/lib/api/client";
import type { Run } from "@/types/api";
import AgentAnswer from "./agent-answer";
import useProducerIntent from "../hooks/use-producer-intent";
import useProducerProgress from "../hooks/use-producer-progress";
import type {
  ProducerAnswerPayload,
  ProducerCommandPayload,
  ProducerOperation,
} from "../producer-types";

const agents = {
  screenwriter: "编剧",
  director: "导演",
  art_director: "美术总监",
  continuity_reviewer: "连续性审查",
};
const commandNames: Record<string, string> = {
  PLAN: "初始规划",
  DELEGATE: "委派专业 Agent",
  REPLAN: "重新规划",
  RECOVER: "恢复原操作",
  ANSWER: "响应创作问题",
  CANCEL: "停止生产协作",
};

type Props = {
  projectId: string;
  run: Run;
  writable: boolean;
  busy: boolean;
  reload: () => Promise<void>;
};

export default function ProducerProgress(props: Props) {
  const { user } = useAuth();
  return (
    <Progress
      key={`${user.id}:${props.projectId}:${props.run.episode_id}:${props.run.id}:${props.writable}`}
      {...props}
      userId={user.id}
    />
  );
}

function Progress({
  userId,
  projectId,
  run,
  writable,
  busy,
  reload,
}: Props & { userId: string }) {
  const progress = useProducerProgress({
    projectId,
    episodeId: run.episode_id,
    runId: run.id,
    revision: run.revision,
    status: run.status,
  });
  const intent = useProducerIntent({
    userId,
    projectId,
    episodeId: run.episode_id,
    runId: run.id,
    writable,
  });
  const [reason, setReason] = useState("");
  const [trigger, setTrigger] = useState<
    "FAILURE" | "STALE" | "BUDGET_CHANGED" | "RECHECK"
  >("RECHECK");
  const [refreshError, setRefreshError] = useState("");
  const data = progress.data;
  const locked =
    !writable ||
    busy ||
    !intent.ready ||
    intent.mutating ||
    !!intent.pending ||
    !progress.fresh ||
    !data ||
    data.revision < run.revision;
  const latestPlan = data?.plans.at(-1);

  async function refreshAfterWrite() {
    setRefreshError("");
    await progress.refresh();
    try {
      await reload();
    } catch (cause) {
      setRefreshError(`操作已保存，任务列表刷新失败：${message(cause)}`);
    }
  }

  async function command(payload: ProducerCommandPayload) {
    if (locked) return false;
    const accepted = await intent.submit({ kind: "COMMAND", payload });
    if (!accepted) {
      await progress.refresh();
      return false;
    }
    await refreshAfterWrite();
    return true;
  }

  async function answer(operation: ProducerOperation, value?: string) {
    if (
      locked ||
      !data?.can_operate ||
      !operation.can_answer ||
      !operation.question_id
    )
      return false;
    const payload: ProducerAnswerPayload = {
      question_id: operation.question_id,
      input_sha256: operation.input_sha256,
      ...(value === undefined
        ? { decision: "STOP" as const }
        : { decision: "ANSWER" as const, answer: value }),
    };
    const accepted = await intent.submit({
      kind: "ANSWER",
      loopId: operation.loop_id,
      payload,
    });
    if (!accepted) {
      await progress.refresh();
      return false;
    }
    await refreshAfterWrite();
    return true;
  }

  const originalAnswer =
    intent.pending?.kind === "ANSWER"
      ? (JSON.parse(intent.pending.body) as ProducerAnswerPayload)
      : null;

  return (
    <section
      className="flex min-w-0 flex-col gap-4"
      aria-label="Producer 生产进度"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          计划、委派与问题处理以最新服务端状态为准。
        </p>
        <Button
          variant="outline"
          disabled={progress.loading || intent.mutating}
          onClick={() => void progress.refresh()}
        >
          刷新生产状态
        </Button>
      </div>
      {progress.loading && !data && <p role="status">正在读取生产计划…</p>}
      {progress.error && <Notice tone="error">{progress.error}</Notice>}
      {intent.error && <Notice tone="error">{intent.error}</Notice>}
      {intent.notice && <Notice tone="success">{intent.notice}</Notice>}
      {refreshError && <Notice tone="error">{refreshError}</Notice>}
      {intent.pending && (
        <Notice>
          <p>
            原操作结果尚待核验。请先刷新生产状态；重发会沿用同一请求、版本和幂等键。
          </p>
          {originalAnswer && (
            <p className="whitespace-pre-wrap break-words">
              原问题响应：
              {originalAnswer.decision === "ANSWER"
                ? originalAnswer.answer
                : "停止本次创作"}
            </p>
          )}
          <Button
            variant="outline"
            disabled={
              !writable ||
              busy ||
              !intent.ready ||
              intent.mutating ||
              !progress.fresh
            }
            onClick={() => {
              void intent.replay().then(async (accepted) => {
                if (accepted) await refreshAfterWrite();
                else await progress.refresh();
              });
            }}
          >
            重发原操作
          </Button>
        </Notice>
      )}
      {data && (
        <>
          <div className="flex flex-wrap items-start justify-between gap-3 border-t pt-4">
            <div className="min-w-0 flex-1">
              <h4 className="font-medium">生产目标</h4>
              <p className="mt-1 whitespace-pre-wrap break-words text-sm">
                {data.goal}
              </p>
            </div>
            <StatusBadge status={data.status} />
          </div>
          {(!writable || !data.can_operate) && (
            <Notice>
              {!writable
                ? "当前项目角色只可查看生产计划与进度。"
                : data.readonly_reason === "PRODUCER_INITIATOR_REQUIRED"
                  ? "仅原发起者可响应问题、委派或恢复本次生产协作。"
                  : "当前生产协作已暂停或不可执行新操作，请检查状态、预算与执行记录。"}
              {data.readonly_reason && (
                <span className="text-xs">
                  状态依据：{data.readonly_reason}
                </span>
              )}
            </Notice>
          )}
          <section className="border-y py-4" aria-label="Producer 预算与用量">
            <h4 className="font-medium">预算与用量</h4>
            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 text-sm sm:grid-cols-3">
              <Budget
                label="已知实际费用"
                value={`${data.budget.actual_cost} USD`}
              />
              <Budget
                label="待结算预估"
                value={`${data.budget.pending_estimate} USD`}
              />
              <Budget
                label="本次费用上限"
                value={
                  data.budget.limits.maximum_cost === undefined
                    ? "未提供"
                    : data.budget.limits.maximum_cost === null
                      ? "未单独设置"
                      : `${data.budget.limits.maximum_cost} USD`
                }
              />
              <Budget
                label="模型调用"
                value={`${data.budget.model_calls} / ${data.budget.limits.max_model_calls ?? "—"}`}
              />
              <Budget
                label="工具调用"
                value={`${data.budget.tool_calls} / ${data.budget.limits.max_tool_calls ?? "—"}`}
              />
              <Budget
                label="专业 Agent 委派"
                value={`${data.budget.delegations} / ${data.budget.limits.max_delegations ?? "—"}`}
              />
              <Budget
                label="总调用上限"
                value={String(data.budget.limits.max_calls ?? "—")}
              />
              <Budget
                label="步骤上限"
                value={String(data.budget.limits.max_steps ?? "—")}
              />
              <Budget
                label="委派深度上限"
                value={String(data.budget.limits.max_delegation_depth ?? "—")}
              />
              <Budget
                label="执行截止"
                value={
                  data.budget.deadline_at
                    ? new Date(data.budget.deadline_at).toLocaleString("zh-CN")
                    : "未提供"
                }
              />
              <Budget
                label="费用状态"
                value={
                  data.budget.unknown_cost
                    ? "存在未知费用，须先核验"
                    : "按已知用量记录"
                }
              />
            </dl>
            <p className="mt-3 text-xs text-muted-foreground">
              预算与调用数为本次生产协作的服务端汇总；正式交付完成情况仍须检查子任务与审核结果。
            </p>
          </section>
          {data.pending_command && (
            <Notice>
              <p>
                待处理操作：
                {commandNames[data.pending_command.action] ??
                  data.pending_command.action}{" "}
                · {data.pending_command.status}
              </p>
              {data.pending_command.status === "WAITING_EXTERNAL" && (
                <p>
                  外部结果尚未确认，恢复会继续核验原操作，不能跳过未知结果另行发起。
                </p>
              )}
              <Button
                variant="outline"
                disabled={locked || !data.can_recover}
                onClick={() =>
                  void command({
                    action: "RECOVER",
                    expected_revision: data.revision,
                    command_id: data.pending_command!.command_id,
                  })
                }
              >
                恢复待处理操作
              </Button>
            </Notice>
          )}
          <div className="flex min-w-0 flex-col gap-3">
            <h4 className="font-medium">生产计划</h4>
            {!data.plans.length && (
              <p className="text-sm text-muted-foreground">
                尚未提交生产计划，请关注规划进度和待回答问题。
              </p>
            )}
            {data.plans.map((plan) => (
              <details
                key={plan.plan_id}
                open={plan.plan_id === latestPlan?.plan_id}
                className="min-w-0 rounded-lg border p-3"
              >
                <summary className="cursor-pointer font-medium">
                  计划 v{plan.version_no}
                  {plan.plan_id === latestPlan?.plan_id
                    ? " · 当前计划"
                    : " · 历史版本"}
                </summary>
                <p className="mt-3 whitespace-pre-wrap text-sm">
                  {plan.summary}
                </p>
                {plan.reason && (
                  <p className="mt-2 text-sm text-muted-foreground">
                    修订原因：{plan.reason}
                  </p>
                )}
                {plan.previous_plan_id && (
                  <p className="mt-2 break-all text-xs text-muted-foreground">
                    承接计划：{plan.previous_plan_id}
                  </p>
                )}
                <ol className="mt-4 flex flex-col divide-y">
                  {plan.tasks.map((task) => (
                    <li
                      key={task.task_key}
                      className="flex min-w-0 flex-col gap-2 py-3 first:pt-0 last:pb-0"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <strong className="break-words text-sm">
                          {task.task_key} · {agents[task.kind]}
                        </strong>
                        <StatusBadge status={task.status ?? "NOT_STARTED"} />
                      </div>
                      <p className="whitespace-pre-wrap text-sm">
                        {task.reason}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        前置任务：
                        {task.depends_on.length
                          ? task.depends_on.join("、")
                          : "无"}
                      </p>
                      {task.error_code && (
                        <Notice tone="error">
                          任务受阻：{task.error_code}
                        </Notice>
                      )}
                      {task.unknown_external_result && (
                        <Notice>
                          此子任务存在未知外部结果，请在原任务中核验后再继续。
                        </Notice>
                      )}
                      <div className="flex flex-wrap gap-2">
                        {task.child_run_id &&
                          isResourceId(task.child_run_id) && (
                            <Button asChild variant="outline">
                              <Link
                                href={`${episodePath(projectId, run.episode_id, "tasks")}&run=${task.child_run_id}`}
                              >
                                查看子任务 {task.task_key}
                              </Link>
                            </Button>
                          )}
                        {!task.child_run_id &&
                          plan.plan_id === latestPlan?.plan_id && (
                            <Button
                              variant="outline"
                              disabled={
                                locked ||
                                !data.can_operate ||
                                !task.can_delegate
                              }
                              onClick={() =>
                                void command({
                                  action: "DELEGATE",
                                  expected_revision: data.revision,
                                  plan_id: plan.plan_id,
                                  plan_sha256: plan.plan_sha256,
                                  task_key: task.task_key,
                                })
                              }
                            >
                              委派 {task.task_key}
                            </Button>
                          )}
                      </div>
                    </li>
                  ))}
                </ol>
              </details>
            ))}
          </div>
          <div
            className="flex flex-col gap-3"
            aria-label="Producer 规划进度与问题"
          >
            {data.operations.map((operation) => (
              <section
                key={operation.loop_id}
                className="flex flex-col gap-3 rounded-lg border p-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h4 className="text-sm font-medium">
                    {operation.kind === "replan" ? "重新规划" : "初始规划"} ·
                    模型轮次 {operation.model_turns}
                  </h4>
                  <StatusBadge status={operation.status} />
                </div>
                {operation.stop_code && (
                  <p className="text-sm text-muted-foreground">
                    停止原因：{operation.stop_code}
                  </p>
                )}
                {operation.status === "WAITING_HUMAN" &&
                  operation.human_request && (
                    <>
                      <p className="text-sm text-muted-foreground">
                        {operation.human_request.reason}
                      </p>
                      <AgentAnswer
                        key={operation.question_id ?? operation.loop_id}
                        operation={operation}
                        run={run}
                        disabled={
                          locked || !data.can_operate || !operation.can_answer
                        }
                        canStop={operation.can_answer}
                        submit={(value) => answer(operation, value)}
                        stop={() => answer(operation)}
                      />
                      {!operation.can_answer && (
                        <p className="text-xs text-muted-foreground">
                          问题已处理或当前只可查看。
                        </p>
                      )}
                    </>
                  )}
              </section>
            ))}
          </div>
          {latestPlan && (
            <form
              className="flex flex-col gap-3 border-t pt-4"
              onSubmit={(event) => {
                event.preventDefault();
                if (locked || !data.can_replan || !reason.trim()) return;
                void command({
                  action: "REPLAN",
                  expected_revision: data.revision,
                  observation: {
                    base_plan_id: latestPlan.plan_id,
                    base_plan_sha256: latestPlan.plan_sha256,
                    trigger,
                    reason,
                  },
                }).then((saved) => {
                  if (saved) setReason("");
                });
              }}
            >
              <FormLabel>
                重新观察原因
                <NativeSelect
                  value={trigger}
                  onChange={(event) =>
                    setTrigger(event.target.value as typeof trigger)
                  }
                  disabled={locked || !data.can_replan}
                >
                  <NativeSelectOption value="RECHECK">
                    重新检查现状
                  </NativeSelectOption>
                  <NativeSelectOption value="FAILURE">
                    子任务失败
                  </NativeSelectOption>
                  <NativeSelectOption value="STALE">
                    输入或版本已失效
                  </NativeSelectOption>
                  <NativeSelectOption value="BUDGET_CHANGED">
                    预算条件变化
                  </NativeSelectOption>
                </NativeSelect>
              </FormLabel>
              <FormLabel>
                重规划说明
                <Textarea
                  maxLength={2000}
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  disabled={locked || !data.can_replan}
                  required
                />
              </FormLabel>
              <div>
                <Button
                  type="submit"
                  variant="outline"
                  disabled={locked || !data.can_replan || !reason.trim()}
                >
                  重新观察并规划
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                基于当前精确计划检查实际状态，保留已成功且有效的工作；原调用结果未知时先核验。
              </p>
            </form>
          )}
          <div className="flex flex-col gap-2 border-t pt-4">
            <div>
              <Button
                variant="outline"
                disabled={locked || !data.can_cancel}
                onClick={() => {
                  if (
                    window.confirm(
                      "停止本次 Producer 生产协作及后续调用？已委派任务和历史记录会保留，已提交的外部请求仍须进入子任务核验结果。",
                    )
                  )
                    void command({
                      action: "CANCEL",
                      expected_revision: data.revision,
                    });
                }}
              >
                停止生产协作
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              停止后，本次协作及子任务不再发起新的模型或工具调用。
              已提交的外部请求仍须进入对应任务核验结果，历史记录会保留。
            </p>
          </div>
          {!!data.commands.length && (
            <details className="border-t pt-3">
              <summary className="cursor-pointer text-sm font-medium">
                生产操作记录
              </summary>
              <ol className="mt-3 flex flex-col gap-2 text-sm">
                {data.commands.map((item) => (
                  <li
                    key={item.command_id}
                    className="flex flex-wrap gap-x-3 gap-y-1"
                  >
                    <span>
                      修订 {item.revision} ·{" "}
                      {commandNames[item.action] ?? item.action}
                    </span>
                    <span>{item.status}</span>
                    {item.error_code && <span>{item.error_code}</span>}
                  </li>
                ))}
              </ol>
            </details>
          )}
        </>
      )}
    </section>
  );
}

function Budget({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-words font-medium tabular-nums">{value}</dd>
    </div>
  );
}
