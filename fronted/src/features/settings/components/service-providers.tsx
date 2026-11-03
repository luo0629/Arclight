import { Check, Ellipsis, Server } from "lucide-react";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

export type ServiceKind = "text" | "image" | "video" | "speech";
export type ResponseFormat = "json_schema" | "json_object";
export type ServicePreset = {
  id: string;
  kind: ServiceKind;
  name: string;
  group: "domestic" | "international";
  description: string;
  base_url: string;
  response_format: ResponseFormat;
};

export default function ServiceProviders({
  kind,
  presets,
  value,
  disabled,
  onSelect,
}: {
  kind: ServiceKind;
  presets: ServicePreset[];
  value: string;
  disabled: boolean;
  onSelect: (value: string) => void;
}) {
  const itemClass =
    "group relative h-auto min-h-17 w-full min-w-0 justify-start gap-3 rounded-lg border border-border bg-muted/30 px-3 py-3 text-left whitespace-normal transition-colors hover:bg-muted data-[state=on]:border-primary data-[state=on]:bg-accent/30";
  return (
    <ToggleGroup
      type="single"
      value={value}
      onValueChange={(selected) => selected && onSelect(selected)}
      disabled={disabled}
      aria-label="选择服务商"
      className="flex w-full flex-col items-stretch gap-5"
    >
      {(
        [
          ["domestic", "国内"],
          ["international", "海外"],
        ] as const
      ).map(([group, label]) => {
        const providers = presets.filter((preset) => preset.group === group);
        if (!providers.length) return null;
        return (
          <section key={group} aria-label={label} className="min-w-0">
            <h3 className="mb-2 text-xs font-medium text-muted-foreground">
              {label}
            </h3>
            <div className="grid min-w-0 gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {providers.map((provider) => (
                <ToggleGroupItem
                  key={provider.id}
                  value={provider.id}
                  aria-label={provider.name}
                  className={itemClass}
                >
                  <span
                    aria-hidden="true"
                    className="flex size-9 shrink-0 items-center justify-center rounded-lg border bg-muted text-sm font-semibold text-primary"
                  >
                    {provider.name.startsWith("通义")
                      ? "通"
                      : provider.name.startsWith("智谱")
                        ? "智"
                        : provider.name.slice(0, 1)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-semibold">
                      {provider.name}
                    </span>
                    <span className="mt-0.5 block text-xs leading-relaxed text-muted-foreground">
                      {provider.description}
                    </span>
                  </span>
                  <Check
                    aria-hidden="true"
                    className="size-3.5 shrink-0 text-primary opacity-0 group-data-[state=on]:opacity-100"
                  />
                </ToggleGroupItem>
              ))}
            </div>
          </section>
        );
      })}
      <section aria-label="默认与自定义" className="min-w-0">
        <h3 className="mb-2 text-xs font-medium text-muted-foreground">
          {kind === "text" ? "默认与自定义" : "自定义"}
        </h3>
        <div className="grid min-w-0 gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {kind === "text" && (
            <ToggleGroupItem
              value="server"
              aria-label="服务器默认服务"
              className={itemClass}
            >
              <Server className="size-5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold">服务器默认</span>
                <span className="mt-0.5 block text-xs text-muted-foreground">
                  使用系统提供的文字服务
                </span>
              </span>
              <Check
                aria-hidden="true"
                className="size-3.5 shrink-0 opacity-0 group-data-[state=on]:opacity-100"
              />
            </ToggleGroupItem>
          )}
          <ToggleGroupItem
            value="custom"
            aria-label="自定义服务商"
            className={itemClass}
          >
            <Ellipsis className="size-5 shrink-0 text-muted-foreground" />
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold">
                {kind === "text" ? "自定义 · OpenAI 兼容" : "自定义服务"}
              </span>
              <span className="mt-0.5 block text-xs text-muted-foreground">
                自填地址、模型与密钥
              </span>
            </span>
            <Check
              aria-hidden="true"
              className="size-3.5 shrink-0 opacity-0 group-data-[state=on]:opacity-100"
            />
          </ToggleGroupItem>
        </div>
      </section>
    </ToggleGroup>
  );
}
