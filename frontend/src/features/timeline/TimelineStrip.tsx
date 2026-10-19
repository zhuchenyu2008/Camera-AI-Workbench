import { useState } from "react";
import type { TimelineDto } from "../../api/generated/types";
import { activityLabel, activityStyle } from "../../shared/labels";
import { clock, duration } from "../../shared/time";
import { Notice } from "../../shared/components";
export function TimelineStrip({
  timeline,
  displayFrom = timeline.from,
  displayTo = timeline.to,
}: {
  timeline: TimelineDto;
  displayFrom?: string;
  displayTo?: string;
}) {
  const [selected, setSelected] = useState<number | null>(null);
  const start = Date.parse(displayFrom),
    end = Date.parse(displayTo),
    span = Math.max(1, end - start),
    bucket = selected != null ? timeline.buckets[selected] : null;
  return (
    <div className="timeline-strip">
      <div className="timeline-ticks">
        {Array.from({ length: 7 }, (_, i) => (
          <span key={i}>
            {i === 6 && span === 86400000
              ? "24:00"
              : clock(start + (span * i) / 6)}
          </span>
        ))}
      </div>
      <div
        className="timeline-lanes"
        role="group"
        aria-label="服务端时间桶；下方事件列表提供等价的文本入口"
      >
        {timeline.buckets.map((b, i) => {
          const a = b.activities.reduce<(typeof b.activities)[number] | null>(
            (best, x) => (!best || x.duration_ms > best.duration_ms ? x : best),
            null,
          );
          const c = b.coverage;
          const dominant =
            (c.gap_ms ?? 0) >
            Math.max(
              c.classified_ms ?? 0,
              c.unknown_ms ?? 0,
              c.unanalysed_ms ?? 0,
            )
              ? "gap"
              : (c.unanalysed_ms ?? 0) > (c.classified_ms ?? 0)
                ? "pending"
                : (c.unknown_ms ?? 0) > (c.classified_ms ?? 0)
                  ? "unknown"
                  : (a?.activity ?? "empty");
          return (
            <button
              key={b.start_at}
              className={`time-bucket ${["gap", "pending", "unknown", "empty"].includes(dominant) ? dominant : "classified"} ${i === selected ? "selected" : ""}`}
              style={{
                ...(a ? activityStyle(a.activity) : {}),
                left: `${((Date.parse(b.start_at) - start) / span) * 100}%`,
                width: `${((Date.parse(b.end_at) - Date.parse(b.start_at)) / span) * 100}%`,
              }}
              onClick={() => setSelected(i === selected ? null : i)}
              aria-label={`${clock(b.start_at)}—${clock(b.end_at)}，${a ? activityLabel(a.activity) + " " + duration(a.duration_ms) : "尚无已归类活动"}。查看覆盖明细`}
            />
          );
        })}
      </div>
      <div className="timeline-key">
        {[
          ...new Set(
            timeline.buckets.flatMap((b) =>
              b.activities.map((a) => a.activity),
            ),
          ),
        ].map((name) => (
          <span key={name}>
            <i className="swatch" style={activityStyle(name)} />
            {activityLabel(name)}
          </span>
        ))}
        <span>
          <i className="swatch unknown" />
          无法判断
        </span>
        <span>
          <i className="swatch pending" />
          分析结果缺口
        </span>
        <span>
          <i className="swatch gap" />
          缺口
        </span>
      </div>
      {bucket && (
        <Notice
          title={`${clock(bucket.start_at)}—${clock(bucket.end_at)} · 覆盖明细`}
        >
          <p>
            已归类 {duration(bucket.coverage.classified_ms ?? 0, false)} ·
            无法判断 {duration(bucket.coverage.unknown_ms ?? 0, false)} · 分析结果缺口{" "}
            {duration(bucket.coverage.unanalysed_ms ?? 0, false)} · 缺口{" "}
            {duration(bucket.coverage.gap_ms ?? 0, false)}
          </p>
          <p className="caption">
            时间桶仅概括这一段；短活动请从事件列表进入。
          </p>
        </Notice>
      )}
      {timeline.truncated && (
        <Notice tone="warning">
          此范围返回结果已截断，请缩小到 4 小时查看。
        </Notice>
      )}
    </div>
  );
}
