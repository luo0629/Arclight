"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Notice } from "@/components/ui-kit";
import type { Run } from "@/types/api";

export type HumanOperation = {
  loop_id: string;
  question_id: string | null;
  input_sha256: string;
  human_request: { question: string; reason: string; options: string[] } | null;
};

export default function AgentAnswer({
  operation,
  run,
  disabled,
  canStop,
  stop,
  submit,
}: {
  operation: HumanOperation;
  run: Run;
  disabled: boolean;
  canStop: boolean;
  stop: () => Promise<boolean>;
  submit: (answer: string) => Promise<boolean>;
}) {
  const [answer, setAnswer] = useState("");
  const question = operation.human_request!;
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (!disabled && answer.trim()) void submit(answer);
      }}
    >
      <FieldGroup>
        <Notice>{question.question}</Notice>
        <Field data-disabled={disabled}>
          <FieldLabel htmlFor={`answer-${run.id}-${operation.loop_id}`}>
            创作问题回答
          </FieldLabel>
          {question.options.length ? (
            <ToggleGroup
              type="single"
              variant="outline"
              aria-label="创作问题回答"
              disabled={disabled}
              value={answer}
              onValueChange={setAnswer}
            >
              {question.options.map((option) => (
                <ToggleGroupItem key={option} value={option}>
                  {option}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          ) : (
            <Textarea
              id={`answer-${run.id}-${operation.loop_id}`}
              value={answer}
              disabled={disabled}
              maxLength={1000}
              required
              onChange={(e) => setAnswer(e.target.value)}
            />
          )}
        </Field>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={disabled || !answer.trim()}>
            提交回答并继续
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={disabled || !canStop}
            onClick={() => void stop()}
          >
            停止本次创作
          </Button>
        </div>
      </FieldGroup>
    </form>
  );
}
