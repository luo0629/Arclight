"use client";

import { useId } from "react";
import { Button } from "@/components/ui/button";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";
import { Notice } from "@/components/ui-kit";

export type RightsDraft = {
  source: string;
  rights: string;
  confirmed: boolean;
};
export const emptyRights: RightsDraft = {
  source: "",
  rights: "",
  confirmed: false,
};
export function RightsFields({
  value,
  onChange,
  disabled,
  readOnlySource = false,
}: {
  value: RightsDraft;
  onChange: (value: RightsDraft) => void;
  disabled: boolean;
  readOnlySource?: boolean;
}) {
  const prefix = useId();
  return (
    <FieldGroup>
      <Field>
        <FieldLabel htmlFor={prefix + "-source"}>音频来源说明</FieldLabel>
        <Textarea
          id={prefix + "-source"}
          required
          maxLength={1000}
          value={value.source}
          disabled={disabled}
          readOnly={readOnlySource}
          onChange={(event) =>
            onChange({ ...value, source: event.target.value })
          }
        />
      </Field>
      <Field>
        <FieldLabel htmlFor={prefix + "-rights"}>音频授权说明</FieldLabel>
        <Textarea
          id={prefix + "-rights"}
          required
          maxLength={1000}
          value={value.rights}
          disabled={disabled}
          readOnly={readOnlySource}
          onChange={(event) =>
            onChange({ ...value, rights: event.target.value })
          }
        />
      </Field>
      <Field orientation="horizontal">
        <input
          id={prefix + "-confirmed"}
          type="checkbox"
          checked={value.confirmed}
          disabled={disabled}
          onChange={(event) =>
            onChange({ ...value, confirmed: event.target.checked })
          }
        />
        <FieldLabel htmlFor={prefix + "-confirmed"}>
          我确认拥有这段音频的使用权
        </FieldLabel>
      </Field>
    </FieldGroup>
  );
}
export function AudioFeedback({
  error,
  notice,
}: {
  error?: string;
  notice?: string;
}) {
  return (
    <>
      {error && <Notice tone="error">{error}</Notice>}
      {notice && <Notice tone="success">{notice}</Notice>}
    </>
  );
}
export function AudioPagination({
  list,
  label,
}: {
  list: {
    loading: boolean;
    error: string;
    hasMore: boolean;
    load: (append?: boolean) => Promise<void>;
  };
  label: string;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-2" aria-label={label + "分页"}>
      {list.error && <Notice tone="error">{list.error}</Notice>}
      <div className="flex flex-wrap gap-2">
        <Button
          variant="ghost"
          size="sm"
          disabled={list.loading}
          onClick={() => void list.load()}
        >
          刷新{label}
        </Button>
        {list.hasMore && (
          <Button
            variant="outline"
            size="sm"
            disabled={list.loading}
            onClick={() => void list.load(true)}
          >
            继续加载{label}
          </Button>
        )}
      </div>
      {list.loading && (
        <p role="status" className="text-sm text-muted-foreground">
          正在读取{label}…
        </p>
      )}
    </div>
  );
}
