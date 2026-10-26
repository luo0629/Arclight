import { useState, type ReactNode } from "react";
import {
  Search,
  FileText,
  Clapperboard,
  Film,
  NotebookPen,
  ArrowUpRight,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { EmptyState, StatusBadge } from "@/components/ui-kit";
import { useDraftStore } from "./Drafts";
import type { ContentVersion, Graph, GraphNode } from "./api";

type Props = {
  episodeId: string;
  scripts: ContentVersion[];
  boards: ContentVersion[];
  graph: Graph;
  pagination: ReactNode;
  onOpen: (node: GraphNode) => void;
  onResume: (key: string) => void;
};
const filters = [
  ["all", "全部"],
  ["drafts", "草稿"],
  ["saved", "已保存"],
  ["approved", "已批准"],
  ["shots", "镜头"],
] as const;
export default function AssetLibrary({
  episodeId,
  scripts,
  boards,
  graph,
  pagination,
  onOpen,
  onResume,
}: Props) {
  const { drafts } = useDraftStore();
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const matches = (value: string) =>
    value.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase());
  const localDrafts = Object.entries(drafts).filter(
    ([, draft]) => draft.episodeId === episodeId,
  );
  const versions = [
    ...scripts.map((version) => ({ version, kind: "script" })),
    ...boards.map((version) => ({ version, kind: "storyboard" })),
  ];
  const saved = versions.filter(
    ({ version, kind }) =>
      matches(
        (kind === "script"
          ? String(version.content_json.title ?? "剧本")
          : "分镜") +
          " v" +
          version.version_no,
      ) &&
      (filter !== "approved" || version.status === "APPROVED"),
  );
  const shots = graph.nodes.filter(
    (node) =>
      node.kind === "shot" &&
      matches(node.title + " " + (node.description ?? "")),
  );
  const visibleDrafts = localDrafts.filter(([, draft]) => matches(draft.label));
  const showDrafts = filter === "all" || filter === "drafts";
  const showSaved = ["all", "saved", "approved"].includes(filter);
  const showShots = filter === "all" || filter === "shots";
  const count =
    (showDrafts ? visibleDrafts.length : 0) +
    (showSaved ? saved.length : 0) +
    (showShots ? shots.length : 0);
  return (
    <section className="asset-library" aria-label="本集素材库">
      <header className="library-heading">
        <div>
          <p className="library-kicker">当前剧集</p>
          <h2>
            素材库
            <span>
              {localDrafts.length +
                versions.length +
                graph.nodes.filter((node) => node.kind === "shot").length}{" "}
              项已载入
            </span>
          </h2>
          <p>查找草稿、项目版本与镜头。</p>
        </div>
        <label className="library-search">
          <Search aria-hidden="true" />
          <Input
            aria-label="搜索素材"
            placeholder="搜索标题、草稿或镜头…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
      </header>
      <ToggleGroup
        type="single"
        variant="outline"
        value={filter}
        onValueChange={(value) => {
          if (value) setFilter(value);
        }}
        aria-label="素材分类"
        className="library-filters"
      >
        {filters.map(([key, label]) => (
          <ToggleGroupItem key={key} value={key}>
            {label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <p className="library-scope">
        当前剧集 ·
        草稿保存在本机浏览器，关闭后仍可找回，未同步到云端。已保存与已批准内容来自项目版本；图片、音频与视频成品尚未开放。
      </p>
      {showDrafts && visibleDrafts.length > 0 && (
        <section className="library-section" aria-label="未提交的草稿">
          <h3>
            <NotebookPen />
            草稿 <span>{visibleDrafts.length}</span>
          </h3>
          <div className="library-items">
            {visibleDrafts.map(([key, draft]) => (
              <div className="library-row" key={key}>
                <NotebookPen className="library-item-icon" />
                <div className="library-item-copy">
                  <strong>{draft.label}</strong>
                  <p>尚未提交 · 继续编辑后可保存为项目版本</p>
                </div>
                <Button variant="outline" onClick={() => onResume(key)}>
                  继续编辑
                  <ArrowUpRight />
                </Button>
              </div>
            ))}
          </div>
        </section>
      )}
      {showSaved && saved.length > 0 && (
        <section className="library-section" aria-label="保存的版本">
          <h3>
            <FileText />
            {filter === "approved" ? "已批准的版本" : "已保存版本"}{" "}
            <span>{saved.length}</span>
          </h3>
          <div className="library-items">
            {saved.map(({ version, kind }) => {
              const node: GraphNode = graph.nodes.find(
                (node) => node.id === kind + ":" + version.id,
              ) ?? {
                id: kind + ":" + version.id,
                kind,
                resource_id: version.id,
                title:
                  kind === "script"
                    ? String(version.content_json.title ?? "剧本")
                    : "分镜",
                version_no: version.version_no,
                status: version.status,
              };
              const Icon = kind === "script" ? FileText : Clapperboard;
              return (
                <div className="library-row" key={node.id}>
                  <Icon className="library-item-icon" />
                  <div className="library-item-copy">
                    <strong>{node.title}</strong>
                    <p>
                      {kind === "script" ? "剧本" : "分镜"} · v
                      {version.version_no}
                    </p>
                  </div>
                  <StatusBadge status={version.status} />
                  <Button variant="ghost" onClick={() => onOpen(node)}>
                    打开
                    <ArrowUpRight />
                  </Button>
                </div>
              );
            })}
          </div>
        </section>
      )}
      {showShots && shots.length > 0 && (
        <section className="library-section" aria-label="镜头收藏">
          <h3>
            <Film />
            镜头 <span>{shots.length}</span>
          </h3>
          <div className="library-shot-grid">
            {shots.map((node) => {
              const parent = graph.nodes.find((board) =>
                graph.edges.some(
                  (edge) => edge.target === node.id && edge.source === board.id,
                ),
              );
              return (
                <button
                  className="library-shot"
                  key={node.id}
                  onClick={() => onOpen(node)}
                >
                  <div className="shot-thumbnail">
                    <Film aria-hidden="true" />
                    <span>{(node.duration_ms ?? 0) / 1000}s</span>
                  </div>
                  <div>
                    <strong>{node.title}</strong>
                    <small>分镜 v{parent?.version_no ?? "—"} · 独立查看</small>
                    <p>{node.description || "暂无画面描述"}</p>
                  </div>
                  <ArrowUpRight aria-hidden="true" />
                </button>
              );
            })}
          </div>
        </section>
      )}
      {count === 0 && (
        <EmptyState className="library-empty">
          <NotebookPen />
          <h3>
            {search
              ? "没有找到匹配的素材"
              : filter === "drafts"
                ? "还没有未提交的草稿"
                : filter === "approved"
                  ? "还没有已批准的版本"
                  : filter === "shots"
                    ? "还没有镜头"
                    : "还没有保存的素材"}
          </h3>
          <p>
            {search
              ? "换一个关键词，或选择其他分类。"
              : "在创作页起稿或保存版本后，可以在这里找回。"}
          </p>
        </EmptyState>
      )}
      {showSaved && <div className="library-pagination">{pagination}</div>}
    </section>
  );
}
