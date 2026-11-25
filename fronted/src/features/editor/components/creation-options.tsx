"use client";

import type { Episode } from "@/types/api";
import NarrativeInputs from "./narrative-inputs";
import {
  isNarrativeRequest,
  narrativeRequestProblem,
  type NarrativeRequest,
} from "../script-narrative";
import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldDescription,
  FieldSet,
  FieldLegend,
} from "@/components/ui/field";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

export type CreationOptions = {
  narrative?: NarrativeRequest | null;
  professional: boolean;
  performance?: boolean;
  professionalDirector?: boolean;
  directorReview?: boolean;
  artCollaboration?: boolean;
  review: boolean;
};
export const initialCreationOptions: CreationOptions = {
  professional: false,
  professionalDirector: false,
  directorReview: false,
  artCollaboration: false,
  review: false,
};
const automaticRevisionRounds = 3;

export function validCreationOptions(value: CreationOptions) {
  return (
    !value.professional ||
    !value.narrative ||
    (!value.professionalDirector && !narrativeRequestProblem(value.narrative))
  );
}
export function creationPayload(value: CreationOptions, directorOnly = false) {
  return value.professional
    ? {
        professional: true,
        ...(directorOnly && value.performance
          ? {
              performance: true,
              director_revision_rounds: automaticRevisionRounds,
            }
          : {}),
        ...(value.narrative ? { narrative: value.narrative } : {}),
        ...(value.professionalDirector ? { professional_director: true } : {}),
        ...((directorOnly
          ? value.review
          : value.professionalDirector && value.directorReview) &&
        value.artCollaboration
          ? { director_art_collaboration: true }
          : {}),
        ...(value.review
          ? directorOnly
            ? { director_revision_rounds: automaticRevisionRounds }
            : { revision_rounds: automaticRevisionRounds }
          : {}),
        ...(value.professionalDirector && value.directorReview
          ? {
              director_revision_rounds: automaticRevisionRounds,
            }
          : {}),
      }
    : {};
}
export function isCreationOptions(value: unknown): value is CreationOptions {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return (
    typeof row.professional === "boolean" &&
    (row.performance === undefined || typeof row.performance === "boolean") &&
    (row.narrative === undefined ||
      row.narrative === null ||
      isNarrativeRequest(row.narrative)) &&
    (row.professionalDirector === undefined ||
      typeof row.professionalDirector === "boolean") &&
    (row.artCollaboration === undefined ||
      typeof row.artCollaboration === "boolean") &&
    (row.directorReview === undefined ||
      typeof row.directorReview === "boolean") &&
    typeof row.review === "boolean"
  );
}
export default function CreationOptionsFields({
  value,
  onChange,
  disabled,
  allowReview = true,
  rewrite = false,
  allowDirector = false,
  editableDraft = false,
  directorOnly = false,
  narrativeEpisode,
  performanceSourceSchema,
}: {
  value: CreationOptions;
  onChange: (value: CreationOptions) => void;
  disabled: boolean;
  allowReview?: boolean;
  rewrite?: boolean;
  allowDirector?: boolean;
  editableDraft?: boolean;
  directorOnly?: boolean;
  narrativeEpisode?: Episode;
  performanceSourceSchema?: string;
}) {
  const performanceAvailable = performanceSourceSchema === "3.0";
  return (
    <FieldSet disabled={disabled}>
      <FieldLegend variant="label">
        {directorOnly ? "分镜方式" : "创作方式"}
      </FieldLegend>
      <FieldGroup>
        {value.professional && !directorOnly && narrativeEpisode && (
          <NarrativeInputs
            episode={narrativeEpisode}
            value={value.narrative ?? null}
            onChange={(narrative) =>
              onChange({
                ...value,
                narrative,
                ...(narrative
                  ? { professionalDirector: false, directorReview: false }
                  : {}),
              })
            }
            disabled={disabled}
          />
        )}
        <Field>
          <ToggleGroup
            className="w-full flex-wrap"
            type="single"
            variant="outline"
            aria-label={directorOnly ? "分镜方式" : "创作方式"}
            value={
              value.professional
                ? !rewrite && value.review
                  ? "reviewed"
                  : "professional"
                : "standard"
            }
            onValueChange={(mode) => {
              if (mode)
                onChange({
                  ...value,
                  professional: mode !== "standard",
                  ...(mode === "standard" ? { performance: false } : {}),
                  professionalDirector:
                    mode === "standard" ? false : value.professionalDirector,
                  review:
                    mode === "reviewed" || (rewrite && mode === "professional"),
                });
            }}
          >
            <ToggleGroupItem value="standard">
              {directorOnly ? "直接拆镜" : rewrite ? "直接改写" : "直接起稿"}
            </ToggleGroupItem>
            <ToggleGroupItem value="professional">
              {directorOnly
                ? "调查、规划与逐场景拆镜"
                : rewrite
                  ? "调查、规划与评价改写"
                  : "调查、规划与分段创作"}
            </ToggleGroupItem>
            {allowReview && !rewrite && (
              <ToggleGroupItem value="reviewed">
                {directorOnly ? "拆镜、检查与局部修订" : "创作、评价与局部修订"}
              </ToggleGroupItem>
            )}
          </ToggleGroup>
          {value.professional && (
            <FieldDescription>
              {directorOnly
                ? value.review
                  ? "工具检查对白覆盖、镜头顺序和时长，最多自动修订 3 次。通过后交完整分镜审核。"
                  : "先调查精确批准剧本并保存场景计划，再逐场景拆镜；遇到问题会等待你的回答，完整分镜仍须审核。"
                : value.review
                  ? rewrite
                    ? "按指令完成首次改写，再结合剧情评价和规则检查，最多自动修订 3 次。"
                    : "结合剧情评价和规则检查，最多自动修订 3 次。若问题仍未解决，会请你决定。"
                  : "先调查并保存计划，再逐段创作；遇到问题会等待你的回答。"}
              {!directorOnly &&
                (editableDraft ? "完成后可编辑剧本。" : "完整稿仍须审核。")}
            </FieldDescription>
          )}
        </Field>
        {directorOnly && value.professional && (
          <Field>
            <ToggleGroup
              type="single"
              variant="outline"
              className="w-full flex-wrap"
              aria-label="导演分镜合同"
              value={value.performance ? "performance" : "standard"}
              onValueChange={(mode) => {
                if (mode && (mode !== "performance" || performanceAvailable))
                  onChange({ ...value, performance: mode === "performance" });
              }}
            >
              <ToggleGroupItem value="standard">常规文字分镜</ToggleGroupItem>
              <ToggleGroupItem
                value="performance"
                disabled={!performanceAvailable}
              >
                表演与动态分镜
              </ToggleGroupItem>
            </ToggleGroup>
            <FieldDescription>
              {!performanceAvailable
                ? "表演与动态分镜需要已批准的剧情 3.0 剧本；请先将此精确剧本另存升级并批准，再从该版本启动。"
                : value.performance
                  ? "记录叙事目的、动作起止、人物互动与接续，精确标记台词及口型。能力不足会请求修订或人工决定。"
                  : "沿用现有调查、拆镜与审核流程。"}
            </FieldDescription>
          </Field>
        )}
        {value.professional && (
          <>
            {allowDirector && (
              <Field>
                <FieldLabel>剧本批准后的分镜方式</FieldLabel>
                <ToggleGroup
                  type="single"
                  variant="outline"
                  aria-label="剧本批准后的分镜方式"
                  value={
                    value.professionalDirector
                      ? value.directorReview
                        ? "checked"
                        : "professional"
                      : "standard"
                  }
                  onValueChange={(mode) => {
                    if (mode)
                      onChange({
                        ...value,
                        professionalDirector: mode !== "standard",
                        directorReview: mode === "checked",
                      });
                  }}
                >
                  <ToggleGroupItem value="standard">直接拆镜</ToggleGroupItem>
                  <ToggleGroupItem
                    value="professional"
                    disabled={!!value.narrative}
                  >
                    调查、规划与逐场景拆镜
                  </ToggleGroupItem>
                  <ToggleGroupItem value="checked" disabled={!!value.narrative}>
                    拆镜、检查与局部修订
                  </ToggleGroupItem>
                </ToggleGroup>
                <FieldDescription>
                  {value.narrative
                    ? "连续剧剧本批准后可直接拆镜；连续剧专业导演接入将在后续任务开放。"
                    : value.directorReview
                      ? "剧本批准后自动检查和修订分镜，最多修订 3 次。"
                      : "先审核剧本，再审核完整分镜。"}
                </FieldDescription>
              </Field>
            )}
            {((directorOnly && value.review) ||
              (allowDirector &&
                value.professionalDirector &&
                value.directorReview)) && (
              <Field>
                <FieldLabel>美术意见</FieldLabel>
                <ToggleGroup
                  type="single"
                  variant="outline"
                  aria-label="美术意见"
                  value={value.artCollaboration ? "consult" : "independent"}
                  onValueChange={(mode) => {
                    if (mode)
                      onChange({
                        ...value,
                        artCollaboration: mode === "consult",
                      });
                  }}
                >
                  <ToggleGroupItem value="independent">
                    导演独立完成
                  </ToggleGroupItem>
                  <ToggleGroupItem value="consult">
                    按需征询美术
                  </ToggleGroupItem>
                </ToggleGroup>
                <FieldDescription>
                  可基于已批准剧本和工作中镜头征询一次视觉意见，正式变更须人工批准。
                </FieldDescription>
              </Field>
            )}
          </>
        )}
      </FieldGroup>
    </FieldSet>
  );
}
