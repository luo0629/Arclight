"use client";

import { ChevronDown } from "lucide-react";
import { Popover } from "radix-ui";
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type Ref,
} from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type Props = {
  id: string;
  value: string;
  onValueChange: (model: string) => void;
  suggestions: readonly { model: string; name: string }[];
  disabled: boolean;
  invalid: boolean;
  describedBy: string;
  inputRef: Ref<HTMLInputElement>;
};

export function ModelIdInput({
  id,
  value,
  onValueChange,
  suggestions,
  disabled,
  invalid,
  describedBy,
  inputRef,
}: Props) {
  const anchor = useRef<HTMLDivElement>(null);
  const [mode, setMode] = useState<"all" | "filter" | null>(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const term = value.trim().toLowerCase();
  const options =
    mode === "filter"
      ? suggestions.filter(
          ({ model, name }) =>
            model.toLowerCase().includes(term) ||
            name.toLowerCase().includes(term),
        )
      : suggestions;
  const hasSuggestions = suggestions.length > 0;
  const open = !disabled && mode !== null && options.length > 0;
  const listId = `${id}-suggestions`;
  const active = open ? options[activeIndex] : undefined;
  const activeId = active ? `${listId}-${activeIndex}` : undefined;

  useEffect(() => {
    if (activeId) {
      document.getElementById(activeId)?.scrollIntoView({ block: "nearest" });
    }
  }, [activeId]);

  function close() {
    setMode(null);
    setActiveIndex(-1);
  }

  function pick(model: string) {
    onValueChange(model);
    close();
    document.getElementById(id)?.focus({ preventScroll: true });
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (!hasSuggestions || event.nativeEvent.isComposing) return;
    if (event.key === "Tab") close();
    if (event.key === "Enter" && active) {
      event.preventDefault();
      pick(active.model);
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;

    event.preventDefault();
    const forward = event.key === "ArrowDown";
    if (!open) setMode("all");
    const count = open ? options.length : suggestions.length;
    if (!active) {
      setActiveIndex(forward ? 0 : count - 1);
    } else {
      setActiveIndex((activeIndex + (forward ? 1 : -1) + count) % count);
    }
  }

  return (
    <Popover.Root open={open} onOpenChange={(next) => !next && close()}>
      <Popover.Anchor asChild>
        <div ref={anchor} className="relative">
          <Input
            id={id}
            ref={inputRef}
            role={hasSuggestions ? "combobox" : undefined}
            aria-label="模型 ID"
            aria-autocomplete={hasSuggestions ? "list" : undefined}
            aria-expanded={hasSuggestions ? open : undefined}
            aria-controls={open ? listId : undefined}
            aria-activedescendant={activeId}
            aria-invalid={invalid}
            aria-describedby={describedBy}
            className={hasSuggestions ? "pr-9" : undefined}
            value={value}
            required
            maxLength={160}
            autoComplete="off"
            placeholder="供应商提供的模型 ID"
            disabled={disabled}
            onChange={(event) => {
              onValueChange(event.target.value);
              setMode(hasSuggestions ? "filter" : null);
              setActiveIndex(-1);
            }}
            onKeyDown={handleKeyDown}
          />
          {hasSuggestions && (
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              className="absolute top-1 right-1 text-muted-foreground"
              aria-label="查看推荐模型"
              aria-haspopup="listbox"
              aria-expanded={open}
              aria-controls={open ? listId : undefined}
              disabled={disabled}
              onClick={() => {
                if (open && mode === "all") {
                  close();
                } else {
                  setMode("all");
                  setActiveIndex(-1);
                }
                document.getElementById(id)?.focus({ preventScroll: true });
              }}
            >
              <ChevronDown aria-hidden="true" />
            </Button>
          )}
        </div>
      </Popover.Anchor>
      <Popover.Portal>
        <Popover.Content
          id={listId}
          role="listbox"
          aria-label="推荐模型"
          align="start"
          sideOffset={4}
          collisionPadding={12}
          className="z-[60] max-h-[min(16rem,var(--radix-popover-content-available-height))] w-(--radix-popover-trigger-width) overflow-y-auto overscroll-contain rounded-lg border bg-popover p-1 text-sm text-popover-foreground shadow-md outline-none"
          onOpenAutoFocus={(event) => event.preventDefault()}
          onCloseAutoFocus={(event) => event.preventDefault()}
          onInteractOutside={(event) => {
            if (
              event.target instanceof Node &&
              anchor.current?.contains(event.target)
            ) {
              event.preventDefault();
            }
          }}
        >
          {options.map(({ model, name }, index) => (
            <div
              key={model}
              id={`${listId}-${index}`}
              role="option"
              aria-selected={index === activeIndex}
              className="cursor-pointer rounded-md px-2 py-1.5 hover:bg-accent hover:text-accent-foreground aria-selected:bg-accent aria-selected:text-accent-foreground"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => pick(model)}
            >
              <span className="block break-all font-medium">{name}</span>
              {model !== name && (
                <span className="block break-all text-xs">{model}</span>
              )}
            </div>
          ))}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
