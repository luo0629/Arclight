import type { ComponentProps, Ref } from "react";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

type Props = Omit<ComponentProps<typeof Select>, "children"> &
  Pick<
    ComponentProps<typeof SelectTrigger>,
    "id" | "aria-label" | "aria-describedby" | "aria-invalid" | "className"
  > & {
    options: { value: string; label: string; disabled?: boolean }[];
    placeholder?: string;
    triggerRef?: Ref<HTMLButtonElement>;
    onCloseAutoFocus?: ComponentProps<typeof SelectContent>["onCloseAutoFocus"];
  };

// 项目与角色等单选字段共用滚动弹层、键盘选择与移动端边界。
export function ChoiceSelect({
  options,
  placeholder,
  triggerRef,
  onCloseAutoFocus,
  className,
  id,
  "aria-label": label,
  "aria-describedby": description,
  "aria-invalid": invalid,
  ...props
}: Props) {
  return (
    <Select {...props}>
      <SelectTrigger
        ref={triggerRef}
        id={id}
        aria-label={label}
        aria-describedby={description}
        aria-invalid={invalid}
        className={cn("w-full min-w-0", className)}
      >
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent
        onCloseAutoFocus={onCloseAutoFocus}
        position="popper"
        sideOffset={8}
        collisionPadding={12}
        className="max-h-[min(18rem,var(--radix-select-content-available-height))] max-w-[calc(100vw-24px)]"
      >
        <SelectGroup>
          {options.map((option) => (
            <SelectItem
              key={option.value}
              value={option.value}
              disabled={option.disabled}
            >
              {option.label}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  );
}
