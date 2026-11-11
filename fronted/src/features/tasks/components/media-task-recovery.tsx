"use client";

import { useState } from "react";
import { ChoiceSelect } from "@/components/choice-select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FormLabel, Notice } from "@/components/ui-kit";
import type { ResolvePayload } from "../media-types";

type ReceiptStatus =
  "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED" | "NOT_ACCEPTED";
type Props = {
  mode: "resolve" | "replay";
  disabled: boolean;
  batchSize: number | null;
  pendingReplay: { reason: string; expected_revision: number } | null;
  onSubmit: (
    action: "resolve" | "replay",
    payload: ResolvePayload | { reason: string },
  ) => Promise<boolean>;
  onClose: () => void;
};

const receiptOptions = [
  { value: "RUNNING", label: "供应商仍在执行" },
  { value: "SUCCEEDED", label: "供应商已成功" },
  { value: "FAILED", label: "供应商已明确失败" },
  { value: "CANCELLED", label: "供应商已明确取消" },
  { value: "NOT_ACCEPTED", label: "供应商明确未接收" },
];
const errorOptions = [
  { value: "INPUT_INVALID", label: "输入无效" },
  { value: "PROVIDER_REJECTED", label: "供应商拒绝" },
  { value: "PROVIDER_FAILED", label: "供应商执行失败" },
  { value: "PROVIDER_UNAVAILABLE", label: "供应商不可用" },
  { value: "STORAGE_FAILED", label: "文件存储失败" },
  { value: "BUDGET_EXCEEDED", label: "预算超限" },
  { value: "QUALITY_FAILED", label: "质量未通过" },
];
const validReference = (value: string) =>
  /^[A-Za-z0-9_.:/-]{1,200}$/.test(value) && !value.includes("://");

export default function MediaTaskRecovery({
  mode,
  disabled,
  batchSize,
  pendingReplay,
  onSubmit,
  onClose,
}: Props) {
  const [evidence, setEvidence] = useState("");
  const [status, setStatus] = useState<ReceiptStatus>("RUNNING");
  const [handles, setHandles] = useState("");
  const [errorCode, setErrorCode] = useState("PROVIDER_FAILED");
  const [cost, setCost] = useState("");
  const [latency, setLatency] = useState("");
  const [reason, setReason] = useState(pendingReplay?.reason ?? "");
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const settles = ["SUCCEEDED", "FAILED", "CANCELLED"].includes(status);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled) return;
    setError("");
    if (!confirmed) {
      setError("请先确认已核实本次操作。");
      return;
    }
    if (mode === "replay") {
      const trimmed = reason.trim();
      if (!trimmed || trimmed.length > 1000) {
        setError("请填写 1–1000 字的重新发起原因。");
        return;
      }
      if (await onSubmit("replay", { reason: trimmed })) onClose();
      return;
    }
    if (!validReference(evidence.trim())) {
      setError(
        "证据引用须为 1–200 位字母、数字或 _ . : / -，请填写工单或对象标识，不要填写网址。",
      );
      return;
    }
    const outputs = handles
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
    if (
      status === "SUCCEEDED" &&
      ((batchSize !== null && outputs.length !== batchSize) ||
        outputs.length === 0 ||
        outputs.length > 64 ||
        !outputs.every(validReference))
    ) {
      setError(
        batchSize === null
          ? "成功回执须填写全部有效输出引用（1–64 项），每行一项；不接受网址。"
          : `成功回执须填写 ${batchSize} 个有效输出引用，每行一项；不接受网址。`,
      );
      return;
    }
    if (
      settles &&
      cost &&
      (!/^\d+(\.\d{1,6})?$/.test(cost) || Number(cost) >= 1e12)
    ) {
      setError("实际费用须为小于一万亿美元的非负金额，最多六位小数。");
      return;
    }
    if (
      settles &&
      latency &&
      (!/^\d+$/.test(latency) || Number(latency) > 2147483647)
    ) {
      setError("耗时须为 0–2147483647 的整数毫秒。");
      return;
    }
    const receipt: ResolvePayload["receipt"] = {
      status,
      ...(status === "SUCCEEDED"
        ? { result: { output_handles: outputs } }
        : {}),
      ...(status === "FAILED" ? { error_code: errorCode } : {}),
      ...(settles && cost ? { cost } : {}),
      ...(settles && latency ? { latency_ms: Number(latency) } : {}),
    };
    if (
      await onSubmit("resolve", {
        evidence_reference: evidence.trim(),
        confirmed: true,
        receipt,
      })
    )
      onClose();
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label={mode === "resolve" ? "人工确认供应商结果" : "死信重新发起"}
      className="flex min-w-0 flex-col gap-4 rounded-xl border bg-muted/30 p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-semibold">
          {mode === "resolve" ? "人工确认供应商结果" : "从死信重新发起"}
        </h4>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={disabled}
          onClick={onClose}
        >
          收起恢复表单
        </Button>
      </div>
      {mode === "replay" ? (
        <>
          <p className="text-sm leading-relaxed text-muted-foreground">
            这会沿用原业务输入创建新任务，并重新校验预算。原任务与历史记录保留。
          </p>
          {pendingReplay && (
            <Notice>
              上次重新发起的结果尚未确认。已恢复原原因，再次提交会恢复同一命令并核验结果。
            </Notice>
          )}
          <FormLabel>
            重新发起原因
            <Textarea
              aria-label="重新发起原因"
              value={reason}
              maxLength={1000}
              disabled={disabled}
              readOnly={!!pendingReplay}
              onChange={(event) => {
                setReason(event.target.value);
                setConfirmed(false);
              }}
              placeholder="记录本次重新发起的原因，不要填写密钥或签名网址"
              required
            />
          </FormLabel>
        </>
      ) : (
        <>
          <p className="text-sm leading-relaxed text-muted-foreground">
            仅在已核实供应商结果后确认。查询无结果不代表未接收；本操作不会重新提交生成。
          </p>
          <FormLabel>
            证据引用
            <Input
              aria-label="证据引用"
              value={evidence}
              maxLength={200}
              disabled={disabled}
              onChange={(event) => {
                setEvidence(event.target.value);
                setConfirmed(false);
              }}
              placeholder="工单或对象标识，例如 ticket:123"
              required
            />
          </FormLabel>
          <FormLabel>
            确认结果
            <ChoiceSelect
              aria-label="确认结果"
              value={status}
              onValueChange={(value) => {
                setStatus(value as ReceiptStatus);
                setConfirmed(false);
              }}
              options={receiptOptions}
              disabled={disabled}
            />
          </FormLabel>
          {status === "SUCCEEDED" && (
            <FormLabel>
              输出引用（每行一项）
              <Textarea
                aria-label="输出引用（每行一项）"
                value={handles}
                maxLength={12864}
                disabled={disabled}
                onChange={(event) => {
                  setHandles(event.target.value);
                  setConfirmed(false);
                }}
                placeholder={
                  batchSize === null
                    ? "填写全部供应商输出对象标识，不填写网址"
                    : `填写 ${batchSize} 个供应商输出对象标识，不填写网址`
                }
                required
              />
            </FormLabel>
          )}
          {status === "FAILED" && (
            <FormLabel>
              失败类别
              <ChoiceSelect
                aria-label="失败类别"
                value={errorCode}
                onValueChange={(value) => {
                  setErrorCode(value);
                  setConfirmed(false);
                }}
                options={errorOptions}
                disabled={disabled}
              />
            </FormLabel>
          )}
          {settles && (
            <div className="grid min-w-0 gap-4 sm:grid-cols-2">
              <FormLabel>
                实际费用（USD，可选）
                <Input
                  aria-label="实际费用（USD，可选）"
                  inputMode="decimal"
                  value={cost}
                  maxLength={20}
                  disabled={disabled}
                  onChange={(event) => {
                    setCost(event.target.value);
                    setConfirmed(false);
                  }}
                  placeholder="未取得账单时留空"
                />
              </FormLabel>
              <FormLabel>
                耗时（毫秒，可选）
                <Input
                  aria-label="耗时（毫秒，可选）"
                  inputMode="numeric"
                  value={latency}
                  maxLength={10}
                  disabled={disabled}
                  onChange={(event) => {
                    setLatency(event.target.value);
                    setConfirmed(false);
                  }}
                  placeholder="未知时留空"
                />
              </FormLabel>
            </div>
          )}
          <p className="text-xs leading-relaxed text-muted-foreground">
            未提供实际账单时费用仍可能待核账；明确未接收不填写费用或耗时。确认后仍可能按原策略进入安全重试，以刷新后的任务状态为准。
          </p>
        </>
      )}
      <label className="flex items-start gap-2 text-sm leading-relaxed">
        <input
          type="checkbox"
          className="mt-1 size-4 shrink-0 accent-primary"
          checked={confirmed}
          disabled={disabled}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>
          {mode === "resolve"
            ? "我已核实证据及供应商结果，并确认提交此回执"
            : "我确认创建新任务，并接受重新校验预算"}
        </span>
      </label>
      {error && <Notice tone="error">{error}</Notice>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="lg" disabled={disabled || !confirmed}>
          {mode === "resolve" ? "提交人工确认" : "确认重新发起"}
        </Button>
      </div>
    </form>
  );
}
