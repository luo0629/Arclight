"use client";

import { Textarea } from "@/components/ui/textarea";
import type { LineAudioDecision, LineAudioPreparation } from "../types";
import { selectClass } from "./render-format";

export type LineAudioChoice = {
  mode: "" | LineAudioDecision["mode"];
  reason: string;
};

export default function LineAudioDecisions({
  preparation,
  choices,
  disabled,
  onChange,
}: {
  preparation: LineAudioPreparation;
  choices: Record<string, LineAudioChoice>;
  disabled: boolean;
  onChange: (lineId: string, choice: LineAudioChoice) => void;
}) {
  return (
    <section
      aria-label="逐句声音处理"
      className="flex min-w-0 flex-col gap-3 border-t pt-4"
    >
      <header>
        <h5>逐句声音处理</h5>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          已有配音 {preparation.voiced_count}{" "}
          句。没有配音的对白和旁白须逐句明确处理方式并填写原因，决定随本次清单固定。
        </p>
      </header>
      <details className="min-w-0 text-xs">
        <summary>本次声音来源剧本版本</summary>
        <p className="mt-2 break-all">{preparation.script_version_id}</p>
      </details>
      {preparation.lines.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          本时间轴没有需要补充声音决定的台词。
        </p>
      ) : (
        <ol className="flex min-w-0 flex-col gap-3">
          {preparation.lines.map((line, index) => {
            const choice = choices[line.line_id] ?? { mode: "", reason: "" };
            return (
              <li
                key={line.line_id}
                className="flex min-w-0 flex-col gap-3 rounded-lg border p-3 text-sm"
              >
                <div>
                  <h6>台词 {index + 1}</h6>
                  <p className="mt-1 whitespace-pre-wrap break-words">
                    {line.text}
                  </p>
                  <p className="mt-1 break-all text-xs text-muted-foreground">
                    台词 ID：{line.line_id}
                  </p>
                </div>
                <label className="flex min-w-0 flex-col gap-2">
                  台词 {index + 1} 的声音处理
                  <select
                    aria-label={`台词 ${index + 1} 的声音处理`}
                    className={selectClass}
                    value={choice.mode}
                    disabled={disabled}
                    onChange={(event) =>
                      onChange(line.line_id, {
                        ...choice,
                        mode: event.target.value as LineAudioChoice["mode"],
                      })
                    }
                  >
                    <option value="">请选择本句处理方式</option>
                    <option value="SILENT">明确静音</option>
                    {line.subtitle_available && (
                      <option value="SUBTITLE_ONLY">仅保留已有字幕</option>
                    )}
                  </select>
                </label>
                {!line.subtitle_available && (
                  <p className="text-xs text-muted-foreground">
                    本句没有精确对应的字幕，不能选择仅字幕。
                  </p>
                )}
                <label className="flex min-w-0 flex-col gap-2">
                  台词 {index + 1} 的处理原因
                  <Textarea
                    aria-label={`台词 ${index + 1} 的处理原因`}
                    value={choice.reason}
                    disabled={disabled}
                    maxLength={500}
                    placeholder="说明本句采用此方式的原因，1–500 字"
                    onChange={(event) =>
                      onChange(line.line_id, {
                        ...choice,
                        reason: event.target.value,
                      })
                    }
                  />
                </label>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
