import { useState } from "react";
import { Activity, ArrowRight, HardDrive, Radio, Workflow } from "lucide-react";
import { useApi } from "../../api/query";
import {
  Badge,
  Button,
  EmptyState,
  JobModal,
  Notice,
  PageHeading,
  Pager,
  Panel,
  QueryState,
  ScopeLink,
  ScopePicker,
  useScope,
} from "../../shared/components";
import { jobLabels } from "../../shared/labels";
import { bytes, clock, dateTime, duration } from "../../shared/time";
import type { TokenTotal, UsageTotal } from "../../api/generated/types";
const num = (n: unknown) =>
  typeof n === "number" ? n.toLocaleString("zh-CN") : "未知";
export function tokenText(token: TokenTotal) {
  return token.value == null
    ? "未知"
    : `${num(token.value)}${token.unknown_count ? `（另 ${token.unknown_count} 次未知）` : ""}`;
}
export function costText(value: UsageTotal) {
  return value.cost == null
    ? "未配置"
    : `${value.currency ?? "币种未知"} ${value.cost}${value.cost_estimated ? " · 估算" : ""}`;
}
export function Status() {
  const scope = useScope(),
    q = useApi("getStatus", {}, { poll: 5000 }),
    [job, setJob] = useState<string | null>(scope.search.get("job_id"));
  const jobs = useApi(
    "listJobs",
    {
      query: {
        from: scope.query.from,
        to: scope.query.to,
        job_state: scope.search.get("job_state"),
        kind: scope.search.get("kind"),
        cursor: scope.search.get("cursor"),
        limit: 50,
      },
    },
    { poll: 5000 },
  );
  return (
    <>
      <PageHeading
        kicker="OPERATIONS / OBSERVABILITY"
        title="设备与任务"
        description="采集、AI 队列、媒体存储和应用进程分别显示。"
      />
      <QueryState query={q}>
        {({ data: s }) => (
          <>
            {s.warnings.map((w) => (
              <Notice tone="warning" key={w.code}>
                {w.message}
              </Notice>
            ))}
            <div className="two-columns">
              <Panel title="采集状态" action={<Radio size={18} />}>
                <div>
                  {s.cameras.map((c, i) => (
                    <div
                      className="resource-row"
                      key={String(c.camera_id ?? i)}
                    >
                      <div>
                        <strong>{String(c.name ?? "机位")}</strong>
                        <p className="caption muted">
                          最近帧{" "}
                          {dateTime(
                            typeof c.last_frame_at === "string"
                              ? c.last_frame_at
                              : null,
                          )}
                        </p>
                        <p className="caption muted">
                          过去 5 分钟计划内覆盖率：
                          {typeof c.valid_slot_ratio_5m === "number"
                            ? `${(c.valid_slot_ratio_5m * 100).toFixed(1)}%`
                            : "无计划内范围"}
                        </p>
                        <p className="caption">
                          {c.capture_state === "offline"
                            ? "视频流断开，期间无可用图片"
                            : c.capture_state === "resource_paused"
                              ? "磁盘保护暂停采集"
                              : `采集：${String(c.capture_state ?? "等待状态")}`}
                        </p>
                      </div>
                      <ScopeLink
                        className="text-link"
                        to={`/cameras/${String(c.camera_id)}`}
                      >
                        机位详情 →
                      </ScopeLink>
                    </div>
                  ))}
                </div>
                {!s.cameras.length && (
                  <EmptyState
                    title="尚无机位"
                    description="添加摄像头后可查看实际采集状态。"
                  />
                )}
                <p className="caption muted">
                  家庭路由器与中间链路：未独立检测。
                </p>
              </Panel>
              <Panel title="应用与模型服务" action={<Activity size={18} />}>
                <dl className="definition-list">
                  <div>
                    <dt>API 版本</dt>
                    <dd>{s.api_version}</dd>
                  </div>
                  <div>
                    <dt>数据库版本</dt>
                    <dd>{s.db_schema_version}</dd>
                  </div>
                  <div>
                    <dt>后台进程</dt>
                    <dd>
                      <Badge
                        tone={
                          s.worker_state === "online" ? "success" : "warning"
                        }
                      >
                        {s.worker_state === "online"
                          ? "正常"
                          : s.worker_state === "stale"
                            ? "心跳延迟"
                            : "未启动或离线"}
                      </Badge>
                    </dd>
                  </div>
                  <div>
                    <dt>最近心跳</dt>
                    <dd>{dateTime(s.worker_heartbeat_at)}</dd>
                  </div>
                </dl>
                {s.providers.map((p) => (
                  <div className="row-between" key={String(p.id)}>
                    <strong className="caption">{String(p.name)}</strong>
                    <Badge tone={p.paused ? "warning" : "neutral"}>
                      {p.paused ? "服务已暂停" : "未报告暂停"}
                    </Badge>
                    {p.last_error_code ? (
                      <p className="caption">{String(p.last_error_code)}</p>
                    ) : null}
                  </div>
                ))}
              </Panel>
              <Panel title="媒体存储" action={<HardDrive size={18} />}>
                <dl className="definition-list">
                  {[
                    ["free_bytes", "磁盘剩余"],
                    ["media_bytes", "现场图占用"],
                    ["reference_bytes", "参考图占用"],
                    ["hls_bytes", "预览占用"],
                    ["db_bytes", "数据库"],
                    ["wal_bytes", "写入日志"],
                  ].map(([key, label]) => (
                    <div key={key}>
                      <dt>{label}</dt>
                      <dd>{bytes(s.storage[key])}</dd>
                    </div>
                  ))}
                  <div>
                    <dt>最早可用原图</dt>
                    <dd>
                      {dateTime(
                        typeof s.storage.earliest_available_at === "string"
                          ? s.storage.earliest_available_at
                          : null,
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>提前回收数量</dt>
                    <dd>
                      {num(
                        s.storage.evicted_frame_count ??
                          s.storage.evicted_count,
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>清理时间</dt>
                    <dd>
                      {dateTime(
                        typeof s.storage.last_cleanup_at === "string"
                          ? s.storage.last_cleanup_at
                          : null,
                      )}
                    </dd>
                  </div>
                </dl>
                <ScopeLink className="text-link" to="/settings/analysis">
                  调整保存与资源设置
                  <ArrowRight size={15} />
                </ScopeLink>
              </Panel>
              <Panel title="AI 队列" action={<Workflow size={18} />}>
                <dl className="definition-list">
                  <div>
                    <dt>在途请求</dt>
                    <dd>{num(s.queue.inflight_requests)}</dd>
                  </div>
                  <div>
                    <dt>最老排队图像任务</dt>
                    <dd>
                      {typeof s.queue.oldest_image_job_age_ms === "number"
                        ? duration(s.queue.oldest_image_job_age_ms, false)
                        : "无排队图像任务"}
                    </dd>
                  </div>
                  <div>
                    <dt>最近截止时间</dt>
                    <dd>
                      {dateTime(
                        typeof s.queue.nearest_deadline_at === "string"
                          ? s.queue.nearest_deadline_at
                          : null,
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>最近成功</dt>
                    <dd>
                      {dateTime(
                        typeof s.queue.last_success_at === "string"
                          ? s.queue.last_success_at
                          : null,
                      )}
                    </dd>
                  </div>
                </dl>
                {Array.isArray(s.queue.counts) &&
                  s.queue.counts.map((c, i) => (
                    <div className="resource-meta" key={i}>
                      <span>{String(c.kind)}</span>
                      <span>
                        {jobLabels[String(c.job_state)] ?? String(c.job_state)}
                        ：{num(c.count)}
                      </span>
                    </div>
                  ))}
              </Panel>
            </div>
          </>
        )}
      </QueryState>
      <ScopePicker person={false} cameras={false} />
      <Panel
        title="任务记录"
        action={
          <div className="actions">
            <label className="inline-field">
              状态
              <select
                value={scope.search.get("job_state") ?? ""}
                onChange={(e) => scope.update("job_state", e.target.value)}
              >
                <option value="">全部状态</option>
                {Object.entries(jobLabels).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
            <label className="inline-field">
              类型
              <select
                value={scope.search.get("kind") ?? ""}
                onChange={(e) => scope.update("kind", e.target.value)}
              >
                <option value="">全部类型</option>
                {[
                  ["manual_visual", "原图重分析"],
                  ["manual_records", "记录汇总"],
                  ["camera_test", "机位测试"],
                  ["provider_test", "服务测试"],
                  ["hour_summary", "小时汇总"],
                  ["day_summary", "日报"],
                  ["apply_settings", "设置应用"],
                ].map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
          </div>
        }
      >
        <QueryState query={jobs}>
          {({ data, page }) => (
            <>
              {data.length ? (
                data.map((j) => (
                  <div className="resource-row" key={j.id}>
                    <div>
                      <div className="row-wrap">
                        <Badge
                          tone={
                            j.job_state === "failed"
                              ? "danger"
                              : j.job_state === "succeeded"
                                ? "success"
                                : "neutral"
                          }
                        >
                          {jobLabels[j.job_state]}
                        </Badge>
                        <strong className="caption">{j.kind}</strong>
                      </div>
                      <p className="caption muted">
                        {clock(j.created_at, true)} · {j.phase} ·{" "}
                        <span className="mono">{j.id}</span>
                      </p>
                    </div>
                    <Button onClick={() => setJob(j.id)}>查看进度</Button>
                  </div>
                ))
              ) : (
                <EmptyState
                  title="此范围没有任务"
                  description="切换日期或任务状态查看其他记录。"
                />
              )}
              <Pager hasMore={page.has_more} nextCursor={page.next_cursor} />
            </>
          )}
        </QueryState>
      </Panel>
      {job && <JobModal id={job} onClose={() => setJob(null)} />}
    </>
  );
}
export function Usage() {
  const scope = useScope(),
    group = (
      ["hour", "day", "provider", "purpose"].includes(
        scope.search.get("group_by") ?? "",
      )
        ? scope.search.get("group_by")
        : "hour"
    ) as "hour" | "day" | "provider" | "purpose";
  const providers = useApi("listProviders", { query: { limit: 100 } }),
    q = useApi("getUsage", {
      query: {
        from: scope.query.from,
        to: scope.query.to,
        group_by: group,
        provider_id: scope.search.get("provider_id"),
      },
    });
  return (
    <>
      <PageHeading
        kicker="OPERATIONS / USAGE"
        title="用量"
        description="实际调用与图片数量；未返回的 Token 和未配置的费用保持未知。"
      />
      <ScopePicker person={false} cameras={false} />
      <div className="row-between">
        <label className="inline-field">
          分组
          <select
            value={group}
            onChange={(e) => scope.update("group_by", e.target.value)}
          >
            <option value="hour">按小时</option>
            <option value="day">按日</option>
            <option value="provider">按服务</option>
            <option value="purpose">按用途</option>
          </select>
        </label>
        <label className="inline-field">
          服务
          <select
            value={scope.search.get("provider_id") ?? ""}
            onChange={(e) => scope.update("provider_id", e.target.value)}
          >
            <option value="">全部服务</option>
            {providers.data?.data.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <QueryState query={q}>
        {({ data: u }) => (
          <>
            <div className="stats-grid">
              <Stat
                label="模型请求"
                value={num(u.totals.requests)}
                hint={`${u.totals.succeeded} 成功 · ${u.totals.failed} 失败`}
              />
              <Stat
                label="现场图 / 参考图"
                value={`${num(u.totals.capture_images)} / ${num(u.totals.reference_images)}`}
                hint="按实际调用图片数"
              />
              <Stat
                label="输入 / 输出 Token"
                value={`${tokenText(u.totals.input_tokens)} / ${tokenText(u.totals.output_tokens)}`}
                hint={`${u.totals.unknown_usage_requests} 次请求用量未知`}
              />
              <Stat
                label="费用"
                value={costText(u.totals)}
                hint={
                  u.totals.cost_estimated
                    ? "按已配置单价估算"
                    : "未配置单价不推算金额"
                }
              />
            </div>
            <Panel
              title="调用明细"
              action={<span className="caption muted">表格可横向滚动</span>}
            >
              {u.buckets.length ? (
                <div
                  className="table-scroll"
                  role="region"
                  aria-label="用量明细，可左右滚动"
                  tabIndex={0}
                >
                  <table>
                    <thead>
                      <tr>
                        <th>分组</th>
                        <th>请求</th>
                        <th>成功 / 失败</th>
                        <th>现场图</th>
                        <th>参考图</th>
                        <th>输入 Token</th>
                        <th>输出 Token</th>
                        <th>缓存 Token</th>
                        <th>费用</th>
                      </tr>
                    </thead>
                    <tbody>
                      {u.buckets.map((b) => (
                        <tr key={b.key}>
                          <td>
                            {group === "provider"
                              ? (providers.data?.data.find(
                                  (p) => p.id === b.key,
                                )?.name ?? "已删除服务")
                              : group === "hour"
                                ? clock(b.key)
                                : b.key}
                          </td>
                          <td>{num(b.requests)}</td>
                          <td>
                            {b.succeeded} / {b.failed}
                          </td>
                          <td>{num(b.capture_images)}</td>
                          <td>{num(b.reference_images)}</td>
                          <td>{tokenText(b.input_tokens)}</td>
                          <td>{tokenText(b.output_tokens)}</td>
                          <td>{tokenText(b.cached_tokens)}</td>
                          <td>{costText(b)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState
                  title="此范围暂无模型调用"
                  description="请求数为零；服务未提供的 Token 不会被当成 0。"
                />
              )}
            </Panel>
            <Notice>
              取消任务不撤销已发生的调用。费用仅在有明确币种与价格来源时显示，不执行预算拦截。
            </Notice>
          </>
        )}
      </QueryState>
    </>
  );
}
function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="stat-block">
      <span>{label}</span>
      <strong style={{ fontSize: value.length > 16 ? 18 : 26 }}>{value}</strong>
      <small>{hint}</small>
    </div>
  );
}
