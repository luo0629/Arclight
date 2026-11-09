import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Notice } from "@/components/ui-kit";
import VersionImage from "@/features/assets/components/version-image";
import { label, type ComparisonReview } from "../types";

export default function ReviewReport({
  item,
  onUnavailable,
}: {
  item: ComparisonReview;
  onUnavailable: (error: unknown) => void;
}) {
  const { record, references } = item;
  const review = record.review;
  return (
    <section
      aria-label="连续性审查报告"
      className="flex min-w-0 flex-col gap-5"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={review.conclusion === "PASS" ? "secondary" : "outline"}>
          {label(review.conclusion)}
        </Badge>
        <span className="text-sm">风险：{label(review.risk)}</span>
        <span className="text-sm">
          置信等级：{label(review.confidence.level)}（未经概率校准）
        </span>
      </div>
      <p>{review.summary}</p>
      <p className="text-sm text-muted-foreground">
        建议动作：{label(review.suggested_action)} · 第 {record.sequence} 次审查
        ·{" "}
        <time dateTime={record.recorded_at}>
          {new Date(record.recorded_at).toLocaleString()}
        </time>
      </p>
      {review.confidence.uncertainty.length > 0 && (
        <Notice>
          <ul className="flex flex-col gap-2">
            {review.confidence.uncertainty.map((item, index) => (
              <li key={index}>
                {label(item.reason)}：{item.detail}
              </li>
            ))}
          </ul>
        </Notice>
      )}
      <section aria-label="逐项问题" className="flex flex-col gap-3">
        <h3 className="font-semibold">问题</h3>
        {review.issues.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            此轮未报告连续性问题。
          </p>
        ) : (
          <ol className="flex flex-col gap-4">
            {review.issues.map((issue, index) => (
              <li key={issue.issue_id} className="flex min-w-0 flex-col gap-2">
                <p>
                  <strong>
                    {index + 1}. {label(issue.category)}
                  </strong>{" "}
                  · {label(issue.risk)}风险
                </p>
                <p>{issue.summary}</p>
                <p className="text-sm">建议：{label(issue.suggested_action)}</p>
                <div className="flex flex-wrap gap-3 text-sm">
                  {issue.evidence_ids.map((id) => (
                    <a
                      key={id}
                      className="underline underline-offset-4"
                      href={`#evidence-${id}`}
                    >
                      依据{" "}
                      {review.evidence.findIndex(
                        (item) => item.evidence_id === id,
                      ) + 1}
                    </a>
                  ))}
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>
      <Separator />
      <section aria-label="审查依据" className="flex flex-col gap-3">
        <h3 className="font-semibold">依据与定位</h3>
        <ol className="flex flex-col gap-4">
          {review.evidence.map((evidence, index) => {
            const citation = references.find(
              (item) => item.reference.reference_id === evidence.reference_id,
            );
            return (
              <li
                key={evidence.evidence_id}
                id={`evidence-${evidence.evidence_id}`}
                className="flex min-w-0 scroll-mt-4 flex-col gap-1"
              >
                <p>
                  <strong>依据 {index + 1}</strong> ·{" "}
                  {citation ? label(citation.reference.role) : "引用缺失"}
                </p>
                <p>{evidence.basis}</p>
                {evidence.region && (
                  <p className="text-sm text-muted-foreground">
                    图中区域（比例）：x {evidence.region.x}，y{" "}
                    {evidence.region.y}，宽 {evidence.region.width}，高{" "}
                    {evidence.region.height}
                  </p>
                )}
                <p className="break-all text-xs text-muted-foreground">
                  精确引用：{evidence.reference_id}
                </p>
              </li>
            );
          })}
        </ol>
      </section>
      <Separator />
      <section
        aria-label="精确审查参考"
        className="flex min-w-0 flex-col gap-4"
      >
        <h3 className="font-semibold">本轮精确参考</h3>
        <div className="grid min-w-0 grid-cols-1 gap-4 md:grid-cols-2">
          {references.map((item) => (
            <figure
              key={item.reference.reference_id}
              className="flex min-w-0 flex-col gap-2 rounded-lg border p-3"
            >
              <figcaption className="text-sm font-medium">
                {label(item.reference.role)}
                {item.reference.role === "REFERENCE" ? ` · ${item.name}` : ""}
                {item.status ? ` · ${label(item.status)}` : ""}
              </figcaption>
              {item.image_path && (
                <VersionImage
                  key={item.image_path}
                  compact
                  path={item.image_path}
                  alt={`${label(item.reference.role)} · ${item.reference.resource_id}`}
                  onUnavailable={onUnavailable}
                />
              )}
              {item.unavailable_code && (
                <Notice tone="error">
                  此精确参考暂不可读：{item.unavailable_code}
                </Notice>
              )}
              <dl className="grid min-w-0 grid-cols-1 gap-1 text-xs [&_dd]:break-all [&_dt]:text-muted-foreground">
                <dt>引用 ID</dt>
                <dd>{item.reference.reference_id}</dd>
                <dt>精确资源 ID</dt>
                <dd>{item.reference.resource_id}</dd>
                <dt>内容 SHA-256</dt>
                <dd>{item.reference.content_sha256}</dd>
              </dl>
            </figure>
          ))}
        </div>
      </section>
      <details className="min-w-0 text-xs">
        <summary className="cursor-pointer">审查版本与追溯</summary>
        <dl className="mt-3 grid min-w-0 grid-cols-1 gap-1 [&_dd]:break-all [&_dt]:text-muted-foreground">
          <dt>审查 ID</dt>
          <dd>{review.result_id}</dd>
          <dt>精确镜头／分镜</dt>
          <dd>
            {review.target.shot_id} / {review.target.storyboard_id}
          </dd>
          <dt>记录人</dt>
          <dd>{record.recorded_by}</dd>
          {review.rerun && (
            <>
              <dt>重审来源</dt>
              <dd>
                {review.rerun.previous_review_id} · {review.rerun.reason}
              </dd>
            </>
          )}
          {review.versions.map((version) => (
            <div key={version.id} className="min-w-0">
              <dt>{version.kind}</dt>
              <dd>
                {version.name} · {version.version} · {version.digest}
              </dd>
            </div>
          ))}
        </dl>
      </details>
    </section>
  );
}
