"use client";

import { Notice } from "@/components/ui-kit";
import { asMachineReport } from "../api";
import type { RenderDeliveryReport } from "../types";
import { bytes, duration, renderLabel } from "./render-format";

export default function RenderReport({
  report,
}: {
  report: RenderDeliveryReport;
}) {
  const machine = asMachineReport(report.report.details);
  if (!machine)
    return (
      <section
        aria-label="历史验收回执"
        className="flex min-w-0 flex-col gap-3"
      >
        <h3>历史验收回执</h3>
        <Notice>
          此版本没有正式机器报告。历史手工回执不代表正式交付通过。
        </Notice>
      </section>
    );
  const checks = [
    ["视频完整解码", machine.checks.video_decodable ? "通过" : "未通过"],
    [
      "音轨",
      machine.checks.audio_stream_present ? "存在" : "无音轨，请核对警告",
    ],
    ["字幕时序", machine.checks.subtitle_range_valid ? "通过" : "未通过"],
    ["缺失必需镜头", String(machine.checks.missing_required_shots)],
    ["缺失必需台词音频", String(machine.checks.missing_audio_lines)],
    ["失效依赖", String(machine.checks.stale_dependencies)],
  ];
  return (
    <section aria-label="机器验收报告" className="flex min-w-0 flex-col gap-4">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3>机器验收报告</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            {renderLabel(report.acceptance.phase)}；结论以服务端当前准入为准。
          </p>
        </div>
        <span className="rounded-md bg-muted px-2 py-1 text-xs">
          {duration(machine.duration_ms)} / {bytes(machine.byte_size)}
        </span>
      </header>
      <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {checks.map(([label, value]) => (
          <div
            key={label}
            className="flex min-w-0 items-start justify-between gap-3 border-b py-2 text-sm"
          >
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="text-right font-medium">{value}</dd>
          </div>
        ))}
      </dl>
      {report.acceptance.blocking_issues.length > 0 && (
        <Notice tone="error">
          <strong>当前交付受阻</strong>
          <ul className="list-inside list-disc">
            {report.acceptance.blocking_issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </Notice>
      )}
      <section aria-label="交付警告" className="flex min-w-0 flex-col gap-2">
        <h4>交付警告（{machine.warnings.length}）</h4>
        {machine.warnings.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            本报告没有非阻塞警告。
          </p>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              {report.acceptance.warnings_confirmed
                ? "拥有者已对本报告的完整警告集合确认并恢复。"
                : "以下全部警告须由拥有者明确确认，再独立恢复。"}
            </p>
            <ul className="flex min-w-0 flex-col gap-2">
              {machine.warnings.map((warning) => (
                <li
                  key={warning.id}
                  className="min-w-0 rounded-lg border bg-muted/40 p-3 text-sm"
                >
                  <strong>{renderLabel(warning.code)}</strong>
                  <p className="mt-1 whitespace-pre-wrap break-words">
                    {warning.reason}
                  </p>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
      <p className="border-t pt-3 text-sm text-muted-foreground">
        时间轴 v{machine.timeline_version} · {machine.sources.length} 项输入素材
      </p>
    </section>
  );
}
