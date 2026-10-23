import { useId } from "react";
import { Field, FieldGroup } from "@/components/ui/field";
import { FormLabel } from "@/components/ui-kit";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";

export type Performance = {
  character: string;
  action?: string;
  expression?: string;
  gaze?: string;
};
export type ShotExtensions = {
  scene_id?: string | null;
  composition?: string;
  dialogue_line_ids?: string[];
  character_details?: Performance[];
  sound_effects?: string[];
  transition_out?: string;
  notes?: string;
};
export type SourceLine = { line_id?: string; speaker: string; text: string };

export function ContractMetadata({ content }: { content: unknown }) {
  return (
    <details className="contract-metadata min-w-0 text-sm">
      <summary>稳定标识与精确引用（只读）</summary>
      <p className="my-3 text-muted-foreground">
        台词由 ID
        关联到精确剧本版本，字幕不是台词引用。资产及质检关联必须通过服务端校验；此处不会生成或批准素材。
      </p>
      <pre className="max-h-80 overflow-auto rounded-lg bg-muted p-3 text-xs">
        {JSON.stringify(content, null, 2)}
      </pre>
    </details>
  );
}

export default function ShotDetails({
  shot,
  lines,
  editing,
  update,
}: {
  shot: ShotExtensions & { characters: string[] };
  lines?: SourceLine[];
  editing: boolean;
  update: (patch: ShotExtensions) => void;
}) {
  const id = useId();
  const selected = shot.dialogue_line_ids ?? [];
  function performance(
    character: string,
    field: "action" | "expression" | "gaze",
    value: string,
  ) {
    const current = shot.character_details ?? [];
    const found = current.find((row) => row.character === character);
    update({
      character_details: found
        ? current.map((row) =>
            row.character === character ? { ...row, [field]: value } : row,
          )
        : [...current, { character, [field]: value }],
    });
  }
  return (
    <details className="shot-details min-w-0 rounded-lg border p-3 [overflow-wrap:anywhere]">
      <summary>镜头表演、台词与制作备注</summary>
      {editing ? (
        <FieldGroup className="mt-4">
          <Field>
            <FormLabel htmlFor={id + "-composition"}>画面构图</FormLabel>
            <Textarea
              id={id + "-composition"}
              value={shot.composition ?? ""}
              maxLength={1000}
              onChange={(event) => update({ composition: event.target.value })}
            />
          </Field>
          <Field>
            <FormLabel htmlFor={id + "-lines"}>引用台词（可多选）</FormLabel>
            <NativeSelect
              id={id + "-lines"}
              multiple
              className="w-full [&_select]:h-28 [&_svg]:hidden"
              value={selected}
              disabled={!lines}
              onChange={(event) =>
                update({
                  dialogue_line_ids: Array.from(
                    event.target.selectedOptions,
                    (option) => option.value,
                  ),
                })
              }
            >
              {(lines ?? [])
                .filter((line) => line.line_id)
                .map((line) => (
                  <NativeSelectOption key={line.line_id} value={line.line_id}>
                    {line.speaker}：{line.text}
                  </NativeSelectOption>
                ))}
              {selected
                .filter((id) => !lines?.some((line) => line.line_id === id))
                .map((id) => (
                  <NativeSelectOption key={id} value={id}>
                    {id}（来源未加载或已不在此场景）
                  </NativeSelectOption>
                ))}
            </NativeSelect>
            <small className="text-muted-foreground">
              只引用当前场景的稳定台词 ID。
              {!lines && "精确来源剧本尚未加载，现有引用会保留。"}
            </small>
          </Field>
          {shot.characters.map((character) => {
            const detail = shot.character_details?.find(
              (row) => row.character === character,
            );
            return (
              <FieldGroup
                key={character}
                className="rounded-lg bg-muted/40 p-3"
              >
                <p className="text-sm font-medium">{character} · 镜头表演</p>
                {(["action", "expression", "gaze"] as const).map((field) => (
                  <Field key={field}>
                    <FormLabel>
                      {character} ·{" "}
                      {
                        { action: "动作", expression: "表情", gaze: "视线" }[
                          field
                        ]
                      }
                      <Input
                        value={detail?.[field] ?? ""}
                        maxLength={field === "action" ? 2000 : 300}
                        onChange={(event) =>
                          performance(character, field, event.target.value)
                        }
                      />
                    </FormLabel>
                  </Field>
                ))}
              </FieldGroup>
            );
          })}
          <Field>
            <FormLabel htmlFor={id + "-transition"}>镜尾转场</FormLabel>
            <NativeSelect
              id={id + "-transition"}
              value={shot.transition_out ?? "cut"}
              onChange={(event) =>
                update({ transition_out: event.target.value })
              }
            >
              <NativeSelectOption value="cut">直接切换</NativeSelectOption>
              <NativeSelectOption value="fade">淡入淡出</NativeSelectOption>
              <NativeSelectOption value="dissolve">叠化</NativeSelectOption>
              <NativeSelectOption value="none">无转场</NativeSelectOption>
            </NativeSelect>
          </Field>
          <FieldGroup>
            {(shot.sound_effects ?? []).map((effect, index) => (
              <Field key={index}>
                <FormLabel>
                  音效 {index + 1}
                  <Input
                    value={effect}
                    maxLength={500}
                    onChange={(event) =>
                      update({
                        sound_effects: shot.sound_effects?.map((row, i) =>
                          i === index ? event.target.value : row,
                        ),
                      })
                    }
                  />
                </FormLabel>
                <Button
                  variant="outline"
                  type="button"
                  onClick={() =>
                    update({
                      sound_effects: shot.sound_effects?.filter(
                        (_, i) => i !== index,
                      ),
                    })
                  }
                >
                  移除音效 {index + 1}
                </Button>
              </Field>
            ))}
            <Button
              variant="outline"
              type="button"
              disabled={(shot.sound_effects?.length ?? 0) >= 30}
              onClick={() =>
                update({ sound_effects: [...(shot.sound_effects ?? []), ""] })
              }
            >
              添加音效
            </Button>
          </FieldGroup>
          <Field>
            <FormLabel htmlFor={id + "-notes"}>人工备注</FormLabel>
            <Textarea
              id={id + "-notes"}
              value={shot.notes ?? ""}
              maxLength={2000}
              onChange={(event) => update({ notes: event.target.value })}
            />
          </Field>
        </FieldGroup>
      ) : (
        <div className="mt-3 flex flex-col gap-2 text-sm">
          <p>构图：{shot.composition || "未填写"}</p>
          <p>台词引用：{selected.length ? selected.join("、") : "无"}</p>
          {shot.character_details?.map((row) => (
            <p key={row.character}>
              {row.character}：
              {[row.action, row.expression, row.gaze]
                .filter(Boolean)
                .join(" · ") || "未填写表演"}
            </p>
          ))}
          <p>音效：{shot.sound_effects?.join("、") || "无"}</p>
          <p>转场：{shot.transition_out ?? "cut"}</p>
          {shot.notes && <p>备注：{shot.notes}</p>}
        </div>
      )}
    </details>
  );
}
