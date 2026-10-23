import { useEffect, useId, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  FormLabel,
  LoadingState,
  Notice,
  StatusBadge,
} from "@/components/ui-kit";
import { api, message, type ContentVersion } from "./api";
import usePagedList from "./usePagedList";
import ListPagination from "./ListPagination";
import type { VersionKind } from "./VersionEditor";

type Reference = { id: string; version_no: number; status: string };
type Snapshot = Reference & {
  created_at: string;
  content_json: Record<string, unknown>;
  origin: "manual" | "generated" | "unknown";
  author: { actor_id: string; display_name: string | null } | null;
  base_version: Reference | null;
  script_version: Reference | null;
  run_id: string | null;
  impact: {
    storyboard_count: number;
    shot_count: number;
    director_run_count: number;
    storyboards: Reference[];
  };
};
type Comparison = { base: Snapshot | null; target: Snapshot };

const names: Record<string, string> = {
  title: "标题",
  summary: "梗概",
  characters: "角色",
  scenes: "场景",
  dialogue: "对白",
  action: "动作",
  speaker: "说话角色",
  text: "内容",
  shots: "镜头",
  scene_no: "场景编号",
  shot_no: "镜头编号",
  shot_size: "景别",
  visual_description: "画面描述",
  duration_ms: "时长（毫秒）",
  camera_movement: "镜头运动",
  subtitle: "字幕",
  schema_version: "合同版本",
  scene_id: "稳定场景 ID",
  line_id: "稳定台词 ID",
  line_type: "台词类别",
  emotion: "情绪",
  character_assets: "角色精确资产",
  character_details: "角色表演",
  character: "角色",
  asset: "角色资产",
  asset_id: "资产 ID",
  asset_version_id: "精确资产版本",
  scene_asset: "场景资产",
  location_asset: "地点资产",
  props: "道具资产",
  costume: "服装版本",
  expression: "表情",
  gaze: "视线",
  composition: "构图",
  dialogue_line_ids: "台词引用",
  sound_effects: "音效",
  transition_out: "转场",
  notes: "人工备注",
  selected_visual_asset: "选用画面版本",
  quality_review: "质检批准引用",
  review_id: "审核记录 ID",
  order: "排列顺序",
};

// 台词按稳定 ID 配对；场景和镜头保持原业务编号比较。
function fields(value: unknown, path = "", result = new Map<string, string>()) {
  if (
    Array.isArray(value) &&
    value.length &&
    value.every(
      (row) =>
        row &&
        typeof row === "object" &&
        ("scene_no" in row || "shot_no" in row || "line_id" in row),
    )
  ) {
    const ids = value.map((row) => row.line_id ?? row.shot_no ?? row.scene_no);
    result.set(`${path} / 排列顺序`, ids.join(" → "));
    value.forEach((row) =>
      fields(
        row,
        `${path} ${row.line_id ?? row.shot_no ?? row.scene_no}`,
        result,
      ),
    );
  } else if (
    Array.isArray(value) &&
    value.length &&
    value.some((row) => row && typeof row === "object")
  ) {
    value.forEach((row, index) => fields(row, `${path} ${index + 1}`, result));
  } else if (
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value).length
  ) {
    Object.entries(value).forEach(([key, row]) =>
      fields(
        row,
        [path, names[key] ?? key].filter(Boolean).join(" / "),
        result,
      ),
    );
  } else {
    result.set(path, typeof value === "string" ? value : JSON.stringify(value));
  }
  return result;
}

function Metadata({
  row,
  label,
  kind,
}: {
  row: Snapshot;
  label: string;
  kind: VersionKind;
}) {
  const generated = row.origin === "generated";
  return (
    <Card
      className="min-w-0 gap-3 py-4 shadow-none"
      aria-label={`${label}版本信息`}
    >
      <CardHeader className="gap-2 px-4">
        <CardTitle className="flex flex-wrap items-center gap-2">
          {label} · v{row.version_no} <StatusBadge status={row.status} />
        </CardTitle>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-2 px-4 text-xs leading-relaxed [overflow-wrap:anywhere]">
        <p>
          {generated ? "生成：系统工作流；发起人" : "修改人"}：
          {row.author
            ? `${row.author.display_name ?? row.author.actor_id}`
            : "历史记录未提供"}
        </p>
        {row.author && (
          <p className="text-muted-foreground">
            操作者 ID：{row.author.actor_id}
          </p>
        )}
        <p>
          版本创建时间：
          <time dateTime={row.created_at}>
            {new Date(row.created_at).toLocaleString("zh-CN", {
              hour12: false,
            })}
          </time>
        </p>
        <p>版本 ID：{row.id}</p>
        <p>
          编辑来源：
          {row.base_version
            ? `同类 v${row.base_version.version_no} · ${row.base_version.id}`
            : generated
              ? "系统生成，并非人工另存"
              : "新建或历史来源未记录（不推断为上一版）"}
        </p>
        {kind === "storyboard" && (
          <p>
            引用剧本：
            {row.script_version
              ? `v${row.script_version.version_no} · ${row.script_version.id}`
              : "未记录"}
          </p>
        )}
        {row.run_id && <p>生成任务：{row.run_id}</p>}
        <div
          className="mt-1 rounded-lg bg-muted p-3"
          aria-label={`${label}下游影响`}
        >
          <p className="font-medium">此精确版本的现有下游</p>
          <p>
            {row.impact.storyboard_count} 个分镜版本 · {row.impact.shot_count}{" "}
            个镜头 · {row.impact.director_run_count} 个精确引用导演任务
          </p>
          {row.impact.storyboards.length > 0 && (
            <ul className="mt-2 list-inside list-disc">
              {row.impact.storyboards.map((board) => (
                <li key={board.id}>
                  分镜 v{board.version_no} · {board.id}
                </li>
              ))}
            </ul>
          )}
          {row.impact.storyboard_count > row.impact.storyboards.length && (
            <p>
              仅预览最新 {row.impact.storyboards.length} 个，共{" "}
              {row.impact.storyboard_count} 个。
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function ComparisonPanel({
  episodeId,
  kind,
  version,
}: {
  episodeId: string;
  kind: VersionKind;
  version: ContentVersion;
}) {
  const selectId = useId();
  const prefix = `/episodes/${episodeId}/${kind === "script" ? "scripts" : "storyboards"}`;
  const history = usePagedList<ContentVersion>(prefix);
  const { reload } = history;
  const [baseId, setBaseId] = useState("previous");
  const [attempt, setAttempt] = useState(0);
  const [data, setData] = useState<Comparison | null>(null);
  const [error, setError] = useState("");
  const [showAll, setShowAll] = useState(false);
  useEffect(() => {
    void reload().catch(() => {});
  }, [reload]);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError("");
    const query =
      baseId === "previous"
        ? ""
        : `?base_version_id=${encodeURIComponent(baseId)}`;
    void api<Comparison>(`${prefix}/${version.id}/comparison${query}`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (!controller.signal.aborted) setData(result);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(message(reason));
      });
    return () => controller.abort();
  }, [prefix, version.id, baseId, attempt]);
  const changes = useMemo(() => {
    if (!data) return [];
    const before = data.base
      ? fields(data.base.content_json)
      : new Map<string, string>();
    const after = fields(data.target.content_json);
    return [...new Set([...before.keys(), ...after.keys()])].map((path) => ({
      path,
      before: before.get(path),
      after: after.get(path),
      type: !before.has(path)
        ? "新增"
        : !after.has(path)
          ? "删除"
          : before.get(path) === after.get(path)
            ? "未变"
            : "修改",
    }));
  }, [data]);
  const changed = changes.filter((row) => row.type !== "未变");
  const candidates = new Map(history.rows.map((row) => [row.id, row]));
  if (data?.base && !candidates.has(data.base.id))
    candidates.set(data.base.id, { ...version, ...data.base });
  if (!candidates.has(version.id)) candidates.set(version.id, version);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <FormLabel htmlFor={selectId} id={`${selectId}-label`}>
        基准版本
      </FormLabel>
      <Select value={baseId} onValueChange={setBaseId}>
        <SelectTrigger
          id={selectId}
          aria-labelledby={`${selectId}-label`}
          className="w-full"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectItem value="previous">自动选择较早的最近版本</SelectItem>
            {[...candidates.values()]
              .sort((a, b) => b.version_no - a.version_no)
              .map((row) => (
                <SelectItem key={row.id} value={row.id}>
                  v{row.version_no}
                  {row.id === version.id ? "（与自身比较）" : ""}
                </SelectItem>
              ))}
          </SelectGroup>
        </SelectContent>
      </Select>
      <ListPagination
        {...history}
        label="对比版本"
        count={history.rows.length}
      />
      <Notice>
        比较的是已保存版本，不包含未保存草稿。左为基准、右为当前选中版本；创建时间不是审核时间。下游仅统计现有分镜、镜头和精确引用导演任务；对比不修改历史、不自动失效或重建，图片／音频／成片影响尚未实现。
      </Notice>
      {error ? (
        <Notice tone="error" role="alert">
          {error}
          <Button variant="outline" onClick={() => setAttempt((n) => n + 1)}>
            重试读取对比
          </Button>
        </Notice>
      ) : !data ? (
        <LoadingState>正在读取版本差异…</LoadingState>
      ) : (
        <>
          {!data.base && (
            <Notice>
              这是首个版本，没有更早的版本；以下内容作为首次新增展示。
            </Notice>
          )}
          <div className="grid min-w-0 gap-4 md:grid-cols-2">
            {data.base ? (
              <Metadata row={data.base} label="基准" kind={kind} />
            ) : (
              <Card className="min-w-0 justify-center p-4 text-muted-foreground">
                基准：空内容
              </Card>
            )}
            <Metadata row={data.target} label="当前" kind={kind} />
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p role="status">
              {changed.length
                ? `共 ${changed.length} 处字段差异`
                : "两个版本内容相同"}
            </p>
            <Button
              variant="outline"
              aria-pressed={showAll}
              onClick={() => setShowAll(!showAll)}
            >
              {showAll ? "只看变化" : "显示全部字段"}
            </Button>
          </div>
          <div
            className="flex min-w-0 flex-col gap-3"
            aria-label="内容字段差异"
          >
            {(showAll ? changes : changed).map((row) => (
              <section
                key={row.path}
                aria-label={row.path}
                className="min-w-0 rounded-lg border p-3"
              >
                <h3 className="mb-3 flex flex-wrap items-center gap-2 text-sm">
                  <Badge variant="outline">{row.type}</Badge>
                  {row.path}
                </h3>
                <div className="grid min-w-0 gap-2 md:grid-cols-2">
                  <div className="min-w-0 rounded-md bg-secondary/45 p-3">
                    <p className="mb-1 text-xs font-medium text-muted-foreground">
                      基准内容
                    </p>
                    <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
                      {row.before === undefined
                        ? "（不存在）"
                        : row.before === ""
                          ? "（空文本）"
                          : row.before}
                    </p>
                  </div>
                  <div className="min-w-0 rounded-md bg-accent/60 p-3">
                    <p className="mb-1 text-xs font-medium text-muted-foreground">
                      当前内容
                    </p>
                    <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
                      {row.after === undefined
                        ? "（不存在）"
                        : row.after === ""
                          ? "（空文本）"
                          : row.after}
                    </p>
                  </div>
                </div>
              </section>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default function VersionComparison(props: {
  episodeId: string;
  kind: VersionKind;
  version: ContentVersion;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="lg">
          查看版本差异
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90dvh] min-w-0 overflow-y-auto sm:max-w-5xl">
        <DialogHeader>
          <DialogTitle>
            {props.kind === "script" ? "剧本" : "分镜"}版本差异 · v
            {props.version.version_no}
          </DialogTitle>
          <DialogDescription>
            选择基准，核对字段变化、来源与现有下游。
          </DialogDescription>
        </DialogHeader>
        {open && <ComparisonPanel {...props} />}
      </DialogContent>
    </Dialog>
  );
}
