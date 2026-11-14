import type { GraphNode } from "@/types/api";
import { isResourceId } from "@/lib/routes";

export const assetTypeNames: Record<string, string> = {
  CHARACTER: "角色",
  SCENE: "场景",
  PROP: "道具",
  STYLE: "风格",
  IMAGE: "候选图",
  SHOT_IMAGE: "镜头候选图",
  SHOT_PREVIEW: "预览成片",
  SCRIPT: "剧本",
  STORYBOARD: "分镜",
  AUDIO: "配音",
  MUSIC: "音乐与音效",
  SUBTITLE: "字幕",
  TIMELINE: "时间轴",
  RENDER: "成片",
};
export function nodeGroup(node: GraphNode) {
  if (node.kind === "asset") return "assets";
  if (node.preview_kind === "TIMELINE" || node.asset_type === "TIMELINE")
    return "timelines";
  if (
    node.preview_kind === "DELIVERY" ||
    ["RENDER", "SHOT_PREVIEW"].includes(node.asset_type ?? "")
  )
    return "deliveries";
  if (
    node.preview_kind === "AUDIO" ||
    ["AUDIO", "MUSIC"].includes(node.asset_type ?? "")
  )
    return "audio";
  if (
    node.preview_kind === "IMAGE" ||
    ["IMAGE", "SHOT_IMAGE"].includes(node.asset_type ?? "")
  )
    return "images";
  if (node.kind === "asset_version") return "assets";
  return "text";
}
export function nodeLabel(node: GraphNode) {
  if (node.kind === "asset")
    return `${assetTypeNames[node.asset_type ?? ""] ?? "项目"}资产`;
  if (node.kind === "asset_version")
    return assetTypeNames[node.asset_type ?? ""] ?? "精确资产版本";
  return (
    { episode: "故事起点", script: "剧本", storyboard: "分镜", shot: "镜头" }[
      node.kind
    ] ?? "内容版本"
  );
}
export function selectionLabel(node: GraphNode) {
  if (node.kind !== "asset_version" || !node.asset_id) return "";
  if (["TIMELINE", "RENDER", "SHOT_PREVIEW"].includes(node.asset_type ?? ""))
    return "不可变保存版本";
  return node.current
    ? "正式选用"
    : ["DRAFT", "READY_FOR_REVIEW"].includes(node.status)
      ? "候选版本"
      : "历史／未选用";
}
export function imagePath(node: GraphNode, projectId: string) {
  if (
    !node.preview_path ||
    !node.asset_id ||
    !node.resource_id ||
    ![projectId, node.asset_id, node.resource_id].every(isResourceId)
  )
    return null;
  const expected = `/projects/${projectId}/assets/${node.asset_id}/versions/${node.resource_id}/image`;
  return node.preview_path === expected ? expected : null;
}
