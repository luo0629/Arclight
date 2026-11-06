import { Notice } from "@/components/ui-kit";

type Shot = {
  shot_no: number;
  duration_ms: number;
  visual_description: string;
  dialogue_line_ids: string[];
};
type DirectorIssue = {
  code: string;
  message: string;
  scene_id: string | null;
  slot: number | null;
  line_id: string | null;
};
export type DirectorCheck = {
  accepted: boolean;
  valid: boolean;
  check_sha256: string;
  issue_count: number;
  issues_truncated: boolean;
  issues: DirectorIssue[];
  metrics: {
    expected_lines: number;
    covered_lines: number;
    dialogue_occurrences: number;
    shot_count: number;
    duration_ms: number;
    lower_duration_ms: number;
    upper_duration_ms: number;
    scenes: {
      scene_id: string;
      duration_ms: number;
      expected_duration_ms: number;
    }[];
  } | null;
};
export type DirectorRevision = {
  round: number;
  remaining_patches: number;
  source_sha256: string;
  working_storyboard: {
    scenes: { scene_id: string; scene_no: number; shots: Shot[] }[];
  };
  initial_check: DirectorCheck;
  script_scenes: {
    scene_id: string;
    title: string;
    dialogue: { line_id: string; text: string; speaker: string }[];
  }[];
  patches: {
    selected: boolean;
    patch: { scene_id: string; start: number; end: number; shots: Shot[] };
    check: DirectorCheck;
  }[];
};

function CheckDetails({
  check,
  revision,
}: {
  check: DirectorCheck;
  revision: DirectorRevision;
}) {
  const metrics = check.metrics;
  return (
    <>
      <p>
        {check.valid ? "全部规则检查通过" : `发现 ${check.issue_count} 个问题`}
      </p>
      {metrics && (
        <>
          <p>
            对白覆盖 {metrics.covered_lines} / {metrics.expected_lines} 条 ·
            实际引用 {metrics.dialogue_occurrences} 次 · 共 {metrics.shot_count}{" "}
            个镜头
          </p>
          <p>
            总时长 {metrics.duration_ms / 1000} 秒 · 允许{" "}
            {metrics.lower_duration_ms / 1000}–
            {metrics.upper_duration_ms / 1000} 秒
          </p>
          <ul>
            {metrics.scenes.map((scene) => (
              <li key={scene.scene_id}>
                {revision.script_scenes.find(
                  (item) => item.scene_id === scene.scene_id,
                )?.title ?? "场景"}
                ：{scene.duration_ms / 1000} 秒 / 计划{" "}
                {scene.expected_duration_ms / 1000} 秒
              </li>
            ))}
          </ul>
        </>
      )}
      {check.issues.length > 0 && (
        <ul className="list-disc pl-5">
          {check.issues.map((issue, index) => {
            const scene = revision.working_storyboard.scenes.find(
              (item) => item.scene_id === issue.scene_id,
            );
            const script = revision.script_scenes.find(
              (item) => item.scene_id === issue.scene_id,
            );
            const line = script?.dialogue.find(
              (item) => item.line_id === issue.line_id,
            );
            return (
              <li key={index}>
                <strong>{issue.message}</strong>
                <p>
                  {scene
                    ? `场景 ${scene.scene_no} · ${script?.title ?? ""}`
                    : "完整分镜"}
                  {issue.slot !== null && ` · 场景内镜头 ${issue.slot + 1}`}
                  {line && ` · ${line.speaker}：${line.text}`}
                </p>
              </li>
            );
          })}
        </ul>
      )}
      {check.issues_truncated && (
        <Notice>
          此处展示前 {check.issues.length} 个问题；全部 {check.issue_count}{" "}
          个问题解决后才能交付。
        </Notice>
      )}
    </>
  );
}

export default function DirectorRevisionDetails({
  revision,
  decision,
  reason,
  checks,
}: {
  revision: DirectorRevision;
  decision?: "ACCEPT" | "REVISE" | "ESCALATE";
  reason?: string;
  checks: DirectorCheck[];
}) {
  return (
    <section
      aria-label={`导演检查第 ${revision.round + 1} 轮`}
      className="flex flex-col gap-3"
    >
      <p>剩余导演自动修订：{revision.remaining_patches} 轮</p>
      {decision && (
        <p>
          {decision === "ACCEPT"
            ? "工具检查通过，交完整分镜审核。"
            : decision === "REVISE"
              ? "已采用局部补丁，继续复查。"
              : "保留问题，请你决定。"}
          {reason && ` ${reason}`}
        </p>
      )}
      <CheckDetails check={revision.initial_check} revision={revision} />
      {checks.map((check, index) => (
        <details key={check.check_sha256 + index}>
          <summary>
            实际工具检查 {index + 1}：
            {check.accepted
              ? check.valid
                ? "通过"
                : "仍有问题"
              : "范围未通过"}
          </summary>
          <CheckDetails check={check} revision={revision} />
        </details>
      ))}
      {revision.patches.map((item, index) => {
        const scene = revision.working_storyboard.scenes.find(
          (scene) => scene.scene_id === item.patch.scene_id,
        );
        return (
          <details key={item.check.check_sha256 + index}>
            <summary>
              局部方案 {index + 1} · {item.selected ? "已采用" : "未采用"}
            </summary>
            <p>
              场景 {scene?.scene_no} ·{" "}
              {item.patch.start === item.patch.end
                ? `在第 ${item.patch.start + 1} 个镜头前插入`
                : `替换场景内镜头 ${item.patch.start + 1}–${item.patch.end}`}{" "}
              · 新镜头 {item.patch.shots.length} 个
            </p>
            <p>范围外内容保留；全局镜头序号会重新连续编号。</p>
            <ol className="list-decimal pl-5">
              {item.patch.shots.map((shot, i) => (
                <li key={i}>
                  {shot.visual_description} · {shot.duration_ms / 1000} 秒
                </li>
              ))}
            </ol>
          </details>
        );
      })}
      <details>
        <summary>查看本轮分镜工作稿</summary>
        {revision.working_storyboard.scenes.map((scene) => (
          <section
            key={scene.scene_id}
            aria-label={`工作场景 ${scene.scene_no}`}
          >
            <h5>场景 {scene.scene_no}</h5>
            <ol className="list-decimal pl-5">
              {scene.shots.map((shot, i) => (
                <li key={i}>
                  镜头 {shot.shot_no} · {shot.duration_ms / 1000} 秒
                  <p>{shot.visual_description}</p>
                  <p>对白引用 {shot.dialogue_line_ids.length} 条</p>
                </li>
              ))}
            </ol>
          </section>
        ))}
        <p className="break-all text-xs">
          工作稿 SHA-256：{revision.source_sha256}
        </p>
      </details>
    </section>
  );
}
