import type { TimelineDto } from "../../api/generated/types";
import { activityLabel } from "../../shared/labels";
import { clock, duration } from "../../shared/time";
import { Badge, Notice } from "../../shared/components";

export type TemporalReviewState =
  | "queued"
  | "running"
  | "partial"
  | "complete"
  | "failed";

export type TemporalReviewDto = {
  state: TemporalReviewState;
  reviewed_from?: string | null;
  reviewed_to?: string | null;
  checkpoint_revision?: number | null;
  mutable_tail?: { from: string; to: string } | null;
  source_slice_ids?: string[];
  unresolved_ranges?: Array<{
    from: string;
    to: string;
    reason?: string;
  }>;
  conflicts?: Array<{
    from: string;
    to: string;
    reason?: string;
  }>;
  next_review?: { at: string; reason?: string } | null;
};

export type PublishedTimelineDto = {
  schema_version?: string;
  review_run_id?: string;
  revision: number;
  reviewed_from: string;
  reviewed_to: string;
  frozen_to?: string | null;
  segments: Array<{
    subject_id?: string | null;
    identity_status?: "matched" | "possible" | "unknown";
    activity: string;
    start_at: string;
    end_at: string;
    certainty?: "direct" | "inferred" | "uncertain" | "mixed";
    evidence_ids?: string[];
    source_slice_ids?: string[];
    boundary_range?: {
      start_lower?: string | null;
      start_upper?: string | null;
      end_lower?: string | null;
      end_upper?: string | null;
    };
    reason?: string;
  }>;
  unresolved_ranges?: Array<{
    from: string;
    to: string;
    reason?: string;
  }>;
  conflicts?: Array<{
    from: string;
    to: string;
    reason?: string;
  }>;
  next_review?: { at: string; reason?: string } | null;
};

export type TemporalTimelineDto = TimelineDto & {
  temporal_review?: TemporalReviewDto | null;
  published_timeline?: PublishedTimelineDto | null;
};

const stateLabels: Record<TemporalReviewState, string> = {
  queued: "等待上级模型复核",
  running: "上级模型正在复核",
  partial: "上级复核部分完成",
  complete: "上级复核已完成",
  failed: "上级复核失败",
};

const stateTones: Record<TemporalReviewState, string> = {
  queued: "neutral",
  running: "live",
  partial: "warning",
  complete: "success",
  failed: "danger",
};

const certaintyLabels = {
  direct: "直接观察",
  inferred: "跨片段推断",
  uncertain: "仍有不确定性",
  mixed: "直接观察与推断",
} as const;

function timeRange(from?: string | null, to?: string | null) {
  return from && to ? `${clock(from, true)}—${clock(to, true)}` : null;
}

export function TemporalReviewPanel({
  review,
  published,
  displayFrom,
  displayTo,
}: {
  review?: TemporalReviewDto | null;
  published?: PublishedTimelineDto | null;
  displayFrom: string;
  displayTo: string;
}) {
  if (!review && !published) return null;

  const state = review?.state;
  const segments = (published?.segments ?? []).filter(
    (segment) =>
      Date.parse(segment.start_at) < Date.parse(displayTo) &&
      Date.parse(segment.end_at) > Date.parse(displayFrom),
  );
  const unresolved = [
    ...(published?.unresolved_ranges ?? []),
    ...(review?.unresolved_ranges ?? []).filter(
      (range) =>
        !published?.unresolved_ranges?.some(
          (item) => item.from === range.from && item.to === range.to,
        ),
    ),
  ];
  const conflicts = [
    ...(published?.conflicts ?? []),
    ...(review?.conflicts ?? []).filter(
      (range) =>
        !published?.conflicts?.some(
          (item) => item.from === range.from && item.to === range.to,
        ),
    ),
  ];
  const nextReview = review?.next_review ?? published?.next_review;

  return (
    <Notice
      tone={state === "failed" ? "warning" : "info"}
      title="上级模型裁决"
    >
      <div className="stack">
        <p className="row-wrap">
          {published && <Badge>已发布 r{published.revision}</Badge>}
          {state && <Badge tone={stateTones[state]}>{stateLabels[state]}</Badge>}
        </p>
        {published ? (
          <p>
            已检查 {clock(published.reviewed_from, true)}—
            {clock(published.reviewed_to, true)}
            {published.frozen_to
              ? ` · 已冻结至 ${clock(published.frozen_to, true)}`
              : " · 最近范围仍可能随新证据修订"}
          </p>
        ) : (
          <p>
            尚无上级模型发布版本，当前范围没有最终裁决结论。
            {timeRange(review?.reviewed_from, review?.reviewed_to) &&
              ` 正在处理 ${timeRange(review?.reviewed_from, review?.reviewed_to)}。`}
          </p>
        )}

        {review?.checkpoint_revision != null && (
          <p className="caption muted">
            检查点 r{review.checkpoint_revision}
            {review.source_slice_ids &&
              ` · 读取 ${review.source_slice_ids.length} 个下级观察片段`}
            {review.mutable_tail &&
              ` · 可变范围 ${clock(review.mutable_tail.from, true)}—${clock(review.mutable_tail.to, true)}`}
          </p>
        )}

        {nextReview && (
          <p className="caption muted">
            下次计划检查：{clock(nextReview.at, true)}
          </p>
        )}

        {segments.length > 0 && (
          <div className="stack">
            <strong>发布结论</strong>
            {segments.map((segment, index) => (
              <div className="inset" key={`${segment.start_at}-${index}`}>
                <div className="row-between">
                  <strong>
                    {clock(segment.start_at, true)}—
                    {clock(segment.end_at, true)} · {" "}
                    <span>{activityLabel(segment.activity)}</span>
                  </strong>
                  <span>{duration(Date.parse(segment.end_at) - Date.parse(segment.start_at))}</span>
                </div>
                {segment.reason && <p>{segment.reason}</p>}
                <div className="row-wrap">
                  {segment.certainty && (
                    <Badge>{certaintyLabels[segment.certainty]}</Badge>
                  )}
                  {segment.source_slice_ids && (
                    <span className="caption muted">
                      来源于 {segment.source_slice_ids.length} 个观察片段
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {unresolved.length > 0 && (
          <p>
            尚未解决 {unresolved.length} 段
            {unresolved.map((range, index) => (
              <span key={`${range.from}-${index}`}>
                {index === 0 ? "：" : "、"}
                {timeRange(range.from, range.to)}
                {range.reason ? `（${range.reason}）` : ""}
              </span>
            ))}
          </p>
        )}

        {conflicts.length > 0 && (
          <p>
            有 {conflicts.length} 段证据或观察存在冲突，等待复核。
          </p>
        )}

        {state === "partial" && (
          <p className="caption muted">
            未查看或未解决的范围会继续保留为不确定，不会自动补成活动。
          </p>
        )}
      </div>
    </Notice>
  );
}
