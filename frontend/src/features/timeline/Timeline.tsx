import { useApi } from "../../api/query";
import {
  useScope,
  ScopePicker,
  PageHeading,
  Panel,
  QueryState,
  EventList,
  EmptyState,
  Pager,
  Button,
} from "../../shared/components";
import { activities } from "../../shared/labels";
import { localDate, clock, serverNow } from "../../shared/time";
import { TimelineStrip } from "./TimelineStrip";
export function Timeline() {
  const scope = useScope(),
    zoom = scope.search.get("zoom") === "4" ? "4" : "24";
  const a = Date.parse(scope.query.from),
    b = Date.parse(scope.query.to),
    span = 4 * 3600000;
  const start =
    zoom === "4"
      ? Math.max(
          a,
          Math.min(
            Number(scope.search.get("window_start")) ||
              Math.max(
                a,
                Math.min(b - span, Math.floor(serverNow() / span) * span),
              ),
            Math.max(a, b - span),
          ),
        )
      : a;
  const range = {
    ...scope.query,
    from: new Date(start).toISOString(),
    to: new Date(zoom === "4" ? Math.min(b, start + span) : b).toISOString(),
  };
  const query = useApi(
    "getTimeline",
    { query: { ...range, bucket_seconds: zoom === "4" ? 60 : 300 } },
    { poll: scope.date === localDate() ? 30000 : undefined },
  );
  const events = useApi(
    "listEvents",
    {
      query: {
        ...range,
        activity: scope.search.get("activity"),
        identity_status: (scope.search.get("identity_status") || undefined) as
          | "matched"
          | "possible"
          | "unknown"
          | undefined,
        subject_id: scope.search.has("identity_status")
          ? undefined
          : range.subject_id,
        cursor: scope.search.get("cursor"),
        limit: 50,
      },
    },
    { poll: scope.date === localDate() ? 30000 : undefined },
  );
  const pan = (direction: number) => {
    scope.update(
      "window_start",
      String(
        Math.max(a, Math.min(start + span * direction, Math.max(a, b - span))),
      ),
    );
  };
  return (
    <>
      <PageHeading
        kicker="EXPLORE / TIMELINE"
        title="事件时间线"
        description="沿时间查看活动、未确定的片段和采集缺口。"
      />
      <ScopePicker />
      <Panel
        title="时间范围"
        action={
          <div className="segmented">
            <Button
              className={zoom === "24" ? "selected" : ""}
              onClick={() => scope.update("zoom", "24")}
            >
              全天
            </Button>
            <Button
              className={zoom === "4" ? "selected" : ""}
              onClick={() => scope.update("zoom", "4")}
            >
              4 小时
            </Button>
          </div>
        }
      >
        <div className="row-between">
          <span className="caption">
            {zoom === "24"
              ? "00:00—24:00"
              : `${clock(range.from)}—${clock(range.to)}`}{" "}
            · 北京时间
          </span>
          {zoom === "4" && (
            <div className="actions">
              <Button disabled={start <= a} onClick={() => pan(-1)}>
                前一段
              </Button>
              <Button disabled={start + span >= b} onClick={() => pan(1)}>
                后一段
              </Button>
            </div>
          )}
        </div>
        <QueryState query={query}>
          {({ data }) => (
            <TimelineStrip
              timeline={data}
              displayFrom={range.from}
              displayTo={range.to}
            />
          )}
        </QueryState>
      </Panel>
      <Panel
        title="活动事件"
        action={
          <div className="actions">
            <label className="inline-field">
              人物确认
              <select
                aria-label="人物确认筛选"
                value={scope.search.get("identity_status") ?? ""}
                onChange={(e) =>
                  scope.update("identity_status", e.target.value)
                }
              >
                <option value="">当前人物范围</option>
                <option value="unknown">人物未确认</option>
                <option value="possible">人物可能匹配</option>
                <option value="matched">已确认</option>
              </select>
            </label>
            <label className="inline-field">
              活动
              <select
                aria-label="活动筛选"
                value={scope.search.get("activity") ?? ""}
                onChange={(e) => scope.update("activity", e.target.value)}
              >
                <option value="">全部活动</option>
                {[
                  ...new Set([
                    ...Object.keys(activities),
                    ...(query.data?.data.buckets.flatMap((b) =>
                      b.activities.map((a) => a.activity),
                    ) ?? []),
                  ]),
                ]
                  .map((k) => [k, activities[k] ?? k])
                  .map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
              </select>
            </label>
          </div>
        }
      >
        <QueryState query={events}>
          {({ data, page }) => (
            <>
              {data.length ? (
                <EventList events={data} />
              ) : (
                <EmptyState
                  title="此范围没有匹配事件"
                  description="尝试切换活动筛选，或查看分钟记录中的分析进度。"
                />
              )}
              <Pager hasMore={page.has_more} nextCursor={page.next_cursor} />
            </>
          )}
        </QueryState>
      </Panel>
    </>
  );
}
