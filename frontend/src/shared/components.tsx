import {
  useEffect,
  useId,
  useRef,
  cloneElement,
  isValidElement,
  type ReactNode,
  type ButtonHTMLAttributes,
} from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import {
  AlertCircle,
  ArrowRight,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Loader2,
  X,
} from "lucide-react";
import type { UseQueryResult } from "@tanstack/react-query";
import type {
  ActivityDuration,
  Coverage,
  EventSummaryDto,
  ImpactPreviewDto,
} from "../api/generated/types";
import { api, ApiError, errorText } from "../api/client";
import { useApi, invalidate, recordOps } from "../api/query";
import { useAction } from "./hooks";
import {
  activityLabel,
  activityStyle,
  evidenceLabels,
  jobLabels,
  phaseLabels,
  queueLabels,
} from "./labels";
import { clock, dateTime, duration, localDate, dayRange } from "./time";

export function Button({
  children,
  variant = "secondary",
  className = "",
  busy,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger" | "ghost";
  busy?: boolean;
}) {
  return (
    <button
      className={`button ${variant} ${className}`}
      aria-busy={busy || undefined}
      disabled={busy || props.disabled}
      {...props}
    >
      {busy && <Loader2 size={16} className="spin" />}
      {children}
    </button>
  );
}
export function Badge({
  children,
  tone = "neutral",
  dot = false,
}: {
  children: ReactNode;
  tone?: string;
  dot?: boolean;
}) {
  return (
    <span className={`badge ${tone}`}>
      {dot && <span className="status-dot" />}
      {children}
    </span>
  );
}
export function Panel({
  title,
  kicker,
  action,
  children,
  className = "",
}: {
  title?: ReactNode;
  kicker?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || action) && (
        <div className="panel-heading">
          <div>
            {kicker && <span className="eyebrow">{kicker}</span>}
            {title && <h2>{title}</h2>}
          </div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
export function PageHeading({
  title,
  description,
  action,
  kicker,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  kicker?: string;
}) {
  return (
    <header className="page-heading">
      <div>
        {kicker && <span className="eyebrow">{kicker}</span>}
        <h1>{title}</h1>
        {description && <p className="muted">{description}</p>}
      </div>
      {action && <div className="actions">{action}</div>}
    </header>
  );
}
export function Notice({
  children,
  tone = "info",
  title,
}: {
  children: ReactNode;
  tone?: string;
  title?: string;
}) {
  return (
    <div
      className={`notice ${tone}`}
      role={tone === "danger" ? "alert" : undefined}
    >
      <AlertCircle size={18} />
      <div>
        {title && <strong>{title}</strong>}
        {children}
      </div>
    </div>
  );
}
export function ErrorNotice({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => void;
}) {
  const errorId = useId(),
    root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!(error instanceof ApiError) || !error.fields?.length) return;
    const scope =
      root.current?.closest("form") ??
      root.current?.closest("dialog") ??
      root.current?.closest("main");
    const changed: {
      element: HTMLElement;
      before: string | null;
      invalid: string | null;
    }[] = [];
    error.fields.forEach((field, index) => {
      const path = field.path.replace(/^patch\./, "");
      const element = Array.from(
        scope?.querySelectorAll<HTMLElement>("[name]") ?? [],
      ).find((input) => input.getAttribute("name") === path);
      if (element) {
        changed.push({
          element,
          before: element.getAttribute("aria-describedby"),
          invalid: element.getAttribute("aria-invalid"),
        });
        element.setAttribute("aria-invalid", "true");
        element.setAttribute(
          "aria-describedby",
          `${element.getAttribute("aria-describedby") ?? ""} ${errorId}-${index}`.trim(),
        );
      }
    });
    changed[0]?.element.focus();
    return () => {
      for (const { element, before, invalid } of changed) {
        before
          ? element.setAttribute("aria-describedby", before)
          : element.removeAttribute("aria-describedby");
        invalid
          ? element.setAttribute("aria-invalid", invalid)
          : element.removeAttribute("aria-invalid");
      }
    };
  }, [error, errorId]);
  if (!error) return null;
  return (
    <div ref={root}>
      <Notice tone="danger">
        <p>{errorText(error)}</p>
        {error instanceof ApiError && (
          <>
            {error.fields?.map((f, index) => (
              <p key={f.path} id={`${errorId}-${index}`} className="caption">
                {f.path}：{f.message}
              </p>
            ))}
            {error.requestId && (
              <button
                type="button"
                className="text-button mono"
                onClick={() => navigator.clipboard?.writeText(error.requestId)}
                aria-label="复制请求 ID"
              >
                请求 ID：{error.requestId}
              </button>
            )}
          </>
        )}
        {retry && <Button onClick={retry}>重新读取</Button>}
      </Notice>
    </div>
  );
}
export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <CircleHelp size={26} strokeWidth={1.4} />
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}
export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="skeleton-wrap" role="status" aria-label="正在加载">
      <span className="sr-only">正在读取记录</span>
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          className="skeleton"
          style={{ width: `${100 - i * 12}%` }}
        />
      ))}
    </div>
  );
}
export function QueryState<T>({
  query,
  children,
}: {
  query: Pick<
    UseQueryResult<T, Error>,
    "data" | "isPending" | "error" | "refetch" | "isFetching"
  >;
  children: (data: T) => ReactNode;
}) {
  if (query.isPending)
    return query.error ? (
      <ErrorNotice error={query.error} retry={() => void query.refetch()} />
    ) : (
      <Skeleton />
    );
  if (!query.data)
    return (
      <ErrorNotice error={query.error} retry={() => void query.refetch()} />
    );
  return (
    <>
      {query.error && (
        <ErrorNotice error={query.error} retry={() => void query.refetch()} />
      )}
      <div aria-busy={query.isFetching || undefined}>
        {children(query.data)}
      </div>
    </>
  );
}
export function Field({
  label,
  children,
  hint,
  error,
}: {
  label: ReactNode;
  children: ReactNode;
  hint?: ReactNode;
  error?: string;
}) {
  const id = useId();
  return (
    <div className="field">
      <label
        htmlFor={
          isValidElement<{ id?: string }>(children)
            ? (children.props.id ?? id)
            : id
        }
      >
        {label}
      </label>
      {isValidElement<{
        id?: string;
        "aria-describedby"?: string;
        "aria-invalid"?: boolean;
      }>(children)
        ? cloneElement(children, {
            id: children.props.id ?? id,
            "aria-describedby":
              [
                hint ? `${id}-hint` : "",
                error ? `${id}-error` : "",
                children.props["aria-describedby"],
              ]
                .filter(Boolean)
                .join(" ") || undefined,
            "aria-invalid": error ? true : undefined,
          })
        : children}
      {hint && <small id={`${id}-hint`}>{hint}</small>}
      {error && (
        <small role="alert" className="field-error" id={`${id}-error`}>
          {error}
        </small>
      )}
    </div>
  );
}
export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const headingId = useId();
  const ref = useRef<HTMLDialogElement>(null),
    close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const d = ref.current!;
    d.showModal();
    const cancel = (e: Event) => {
      e.preventDefault();
      close.current();
    };
    d.addEventListener("cancel", cancel);
    document.body.classList.add("modal-open");
    return () => {
      d.removeEventListener("cancel", cancel);
      d.close();
      if (!document.querySelector("dialog[open]"))
        document.body.classList.remove("modal-open");
      prev?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={`modal ${wide ? "wide" : ""}`}
      aria-labelledby={headingId}
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          const r = e.currentTarget.getBoundingClientRect();
          if (
            e.clientX < r.left ||
            e.clientX > r.right ||
            e.clientY < r.top ||
            e.clientY > r.bottom
          )
            onClose();
        }
      }}
    >
      <div className="modal-header">
        <h2 id={headingId}>{title}</h2>
        <Button variant="ghost" onClick={onClose} aria-label="关闭对话框">
          <X size={20} />
        </Button>
      </div>
      <div className="modal-body">{children}</div>
    </dialog>
  );
}
export function Confirm({
  title,
  description,
  onConfirm,
  onClose,
  busy,
  error,
  danger = false,
  label = "确认",
}: {
  title: string;
  description: ReactNode;
  onConfirm: () => void;
  onClose: () => void;
  busy?: boolean;
  error?: unknown;
  danger?: boolean;
  label?: string;
}) {
  return (
    <Modal title={title} onClose={onClose}>
      <div className="stack">
        {description}
        <ErrorNotice error={error} />
        <div className="actions end">
          <Button onClick={onClose}>取消</Button>
          <Button
            variant={danger ? "danger" : "primary"}
            busy={busy}
            onClick={onConfirm}
          >
            {label}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
export function useScope() {
  const [search, setSearch] = useSearchParams();
  const candidate = search.get("date") ?? localDate();
  const date =
    /^\d{4}-\d{2}-\d{2}$/.test(candidate) &&
    Number.isFinite(Date.parse(`${candidate}T00:00:00+08:00`))
      ? candidate
      : localDate();
  const camera_ids = [...new Set(search.getAll("camera_ids"))].sort();
  const subject = search.get("subject_id");
  // Stable day endpoints keep cursors and query keys valid as time passes.
  // The API clips today's statistics and buckets to its current server time.
  const range = dayRange(date, Infinity);
  const update = (key: string, value: string | string[]) => {
    const next = new URLSearchParams(search);
    next.delete(key);
    for (const v of Array.isArray(value) ? value : [value])
      if (v) next.append(key, v);
    next.delete("cursor");
    next.delete("revision");
    setSearch(next);
  };
  return {
    search,
    setSearch,
    date,
    camera_ids,
    subject,
    query: {
      ...range,
      camera_ids: camera_ids.length ? camera_ids : undefined,
      subject_id: subject && subject !== "all" ? subject : undefined,
    },
    update,
  };
}
export function ScopeLink({
  to,
  children,
  className = "",
  ...rest
}: {
  to: string;
  children: ReactNode;
  className?: string;
  [key: string]: unknown;
}) {
  const { search } = useLocation();
  const source = new URLSearchParams(search),
    target = new URL(to, "http://local");
  for (const key of ["date", "camera_ids", "subject_id"])
    if (!target.searchParams.has(key))
      for (const value of source.getAll(key))
        target.searchParams.append(key, value);
  return (
    <Link
      to={
        target.pathname +
        (target.searchParams.size ? `?${target.searchParams}` : "")
      }
      className={className}
      {...rest}
    >
      {children}
    </Link>
  );
}
export function ScopePicker({
  person = true,
  cameras = true,
}: {
  person?: boolean;
  cameras?: boolean;
}) {
  const scope = useScope(),
    entity = useApi("listEntities", { query: { limit: 100 } }),
    camera = useApi("listCameras", { query: { limit: 100 } });
  const personal = Boolean(scope.subject && scope.subject !== "all");
  useEffect(() => {
    if (
      cameras &&
      scope.camera_ids.length === 0 &&
      (!scope.subject || scope.subject === "all")
    ) {
      const first =
        camera.data?.data.find((c) => c.enabled && !c.deleted_at) ??
        camera.data?.data.find((c) => !c.deleted_at);
      if (first) scope.update("camera_ids", [first.id]);
    }
  }, [camera.data, scope.camera_ids.join(","), scope.subject, cameras]);
  return (
    <div className="scope-bar">
      <div className="scope-fields">
        <label className="scope-field">
          <span>日期</span>
          <input
            aria-label="北京时间日期"
            type="date"
            max={localDate()}
            value={scope.date}
            onChange={(e) => {
              if (e.target.value) scope.update("date", e.target.value);
            }}
          />
        </label>
        {person && (
          <label className="scope-field">
            <span>人物</span>
            <select
              aria-label="人物范围"
              value={scope.subject ?? "all"}
              onChange={(e) => {
                const next = new URLSearchParams(scope.search);
                next.set("subject_id", e.target.value);
                next.delete("camera_ids");
                next.delete("cursor");
                scope.setSearch(next);
              }}
            >
              <option value="all">画面主要活动</option>
              {entity.data?.data
                .filter((e) => e.kind === "person")
                .map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name}
                  </option>
                ))}
            </select>
          </label>
        )}
        {cameras && (
          <details className="camera-filter">
            <summary>
              机位
              <span>
                {scope.camera_ids.length
                  ? `已选 ${scope.camera_ids.length} 台`
                  : personal
                    ? "全部相关机位"
                    : "选择机位"}
              </span>
            </summary>
            <div className="camera-filter-menu">
              {personal && (
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={!scope.camera_ids.length}
                    onChange={() => scope.update("camera_ids", [])}
                  />
                  全部机位
                </label>
              )}
              {camera.data?.data.map((c) => (
                <label className="check-row" key={c.id}>
                  <input
                    type={personal ? "checkbox" : "radio"}
                    name="scope-camera"
                    checked={scope.camera_ids.includes(c.id)}
                    onChange={(e) =>
                      scope.update(
                        "camera_ids",
                        !personal
                          ? [c.id]
                          : e.target.checked
                            ? [...scope.camera_ids, c.id]
                            : scope.camera_ids.filter((id) => id !== c.id),
                      )
                    }
                  />
                  {c.name}
                </label>
              ))}
              {!camera.data?.data.length && <small>尚未配置机位</small>}
            </div>
          </details>
        )}
      </div>
      <span className="timezone">
        <span className="status-dot" />
        北京时间 · UTC+8
      </span>
    </div>
  );
}
const coverageTypes = [
  ["classified_ms", "已归类", "classified"],
  ["unknown_ms", "无具体活动", "unknown"],
  ["unanalysed_ms", "暂无可用分析结果", "pending"],
  ["gap_ms", "未获取到画面", "gap"],
] as const;
export function CoverageTrack({
  coverage,
  compact = false,
}: {
  coverage: Coverage;
  compact?: boolean;
}) {
  const total = coverage.planned_ms ?? 0,
    sum = coverageTypes.reduce((s, [key]) => s + (coverage[key] ?? 0), 0);
  return (
    <div className={`coverage ${compact ? "compact" : ""}`}>
      <div
        className="coverage-track"
        role="img"
        aria-label={coverageTypes
          .map(
            ([key, label]) => `${label} ${duration(coverage[key] ?? 0, false)}`,
          )
          .join("，")}
      >
        {total > 0 ? (
          coverageTypes.map(([key, label, kind]) => (
            <span
              key={key}
              className={`coverage-segment ${kind}`}
              title={`${label} ${duration(coverage[key] ?? 0, false)}`}
              style={{ width: `${((coverage[key] ?? 0) / total) * 100}%` }}
            />
          ))
        ) : (
          <span className="coverage-zero" />
        )}
      </div>
      <div className="coverage-legend">
        {coverageTypes.map(([key, label, kind]) => (
          <div key={key}>
            <i className={`swatch ${kind}`} />
            <span>{label}</span>
            <strong>{duration(coverage[key] ?? 0, false)}</strong>
          </div>
        ))}
      </div>
      {!compact && (
        <p className="caption muted">
          计划内已发生范围 {duration(total, false)} · 仅展示后端互斥统计
          {total !== sum ? " · 统计正在更新，请刷新查看" : ""}
        </p>
      )}
      {(coverage.unanalysed_pending_ms ?? 0) > 0 && (
        <p className="caption">
          其中分析处理中 {duration(coverage.unanalysed_pending_ms, false)}
        </p>
      )}
      {(coverage.unanalysed_failed_ms ?? 0) > 0 && (
        <p className="caption">
          其中分析失败 {duration(coverage.unanalysed_failed_ms, false)}
        </p>
      )}
      {(coverage.unanalysed_expired_ms ?? 0) > 0 && (
        <p className="caption">
          其中现场图已过期，未形成分析结果{" "}
          {duration(coverage.unanalysed_expired_ms, false)}
        </p>
      )}
      {(coverage.inferred_ms ?? 0) > 0 && (
        <p className="caption">
          活动推断覆盖 {duration(coverage.inferred_ms, false)}；该时长不增加实际采集量
        </p>
      )}
    </div>
  );
}
export function ActivityChart({ items }: { items: ActivityDuration[] }) {
  const max = Math.max(...items.map((x) => x.duration_ms), 1);
  return (
    <div className="activity-chart">
      {items.length ? (
        items.map((a) => (
          <div className="activity-row" key={a.activity}>
            <div className="row-between">
              <span>
                <i className="activity-dot" style={activityStyle(a.activity)} />
                {activityLabel(a.activity)}
              </span>
              <strong>
                {duration(a.duration_ms)}
                {a.estimated_ms ? `（其中 ${duration(a.estimated_ms)} 为估算）` : ""}
              </strong>
            </div>
            <div className="activity-rail">
              <div
                className="activity-fill"
                style={{
                  ...activityStyle(a.activity),
                  width: `${(a.duration_ms / max) * 100}%`,
                }}
              />
            </div>
          </div>
        ))
      ) : (
        <p className="muted">尚无已归类活动；暂无可用分析结果，请查看覆盖明细。</p>
      )}
    </div>
  );
}
export function EventList({
  events,
  limit,
}: {
  events: EventSummaryDto[];
  limit?: number;
}) {
  const entity = useApi("listEntities", { query: { limit: 100 } }),
    cameras = useApi("listCameras", { query: { limit: 100 } });
  return (
    <div className="event-list">
      {events.slice(0, limit).map((e) => (
        <ScopeLink
          className="event-row"
          to={`/events/${encodeURIComponent(e.id)}`}
          key={e.id}
          state={{ eventDrawer: true }}
        >
          <div className="event-symbol" style={activityStyle(e.activity)}>
            <span className="activity-dot" />
          </div>
          <div className="event-main">
            <div className="row-wrap">
              <strong>{activityLabel(e.activity)}</strong>
              <span className="caption muted">
                {e.identity_status === "matched"
                  ? (entity.data?.data.find((x) => x.id === e.subject_id)
                      ?.name ?? "已删除人物")
                  : e.identity_status === "possible"
                    ? "人物可能匹配"
                    : "画面活动"}
              </span>
              {e.has_manual_revision && <Badge>人工修订</Badge>}
            </div>
            <div className="caption muted">
              {clock(e.start_at, e.duration_ms < 60000)}—
              {clock(e.end_at, e.duration_ms < 60000)} ·{" "}
              {e.camera_ids
                .map(
                  (id) =>
                    cameras.data?.data.find((c) => c.id === id)?.name ??
                    "已删除机位",
                )
                .join("、")}
            </div>
          </div>
          <div className="event-duration">
            <strong>{duration(e.duration_ms)}</strong>
            <small>{evidenceLabels[e.evidence_state]}</small>
          </div>
          <ChevronRight className="event-arrow" size={17} />
        </ScopeLink>
      ))}
    </div>
  );
}
export function RevisionBanner({
  revision,
  historical,
  stale,
  onCurrent,
}: {
  revision: number;
  historical?: boolean;
  stale?: boolean;
  onCurrent?: () => void;
}) {
  return (
    <div className={`revision-banner ${historical || stale ? "warning" : ""}`}>
      <span>
        {historical ? "历史版本 · 只读" : "当前版本"}{" "}
        <span className="mono">v{revision}</span>
        {stale ? " · 来源已更新" : ""}
      </span>
      {historical && <Button onClick={onCurrent}>返回当前版本</Button>}
    </div>
  );
}
export function RevisionPicker({
  current,
  versions,
}: {
  current: number;
  versions?: number[];
}) {
  const [s, set] = useSearchParams();
  const selected = s.get("revision");
  return (
    <label className="inline-field">
      版本
      <select
        aria-label="记录版本"
        value={selected ?? ""}
        onChange={(e) => {
          const n = new URLSearchParams(s);
          e.target.value
            ? n.set("revision", e.target.value)
            : n.delete("revision");
          set(n);
        }}
      >
        <option value="">当前版本</option>
        {(
          versions ??
          Array.from({ length: Math.min(current, 50) }, (_, i) => current - i)
        )
          .filter((n) => n > 0)
          .map((n) => (
            <option key={n} value={n}>
              历史 v{n}
            </option>
          ))}
      </select>
    </label>
  );
}
export function Pager({
  hasMore,
  nextCursor,
}: {
  hasMore: boolean;
  nextCursor: string | null;
}) {
  const { search, setSearch } = useScope();
  return (
    <div className="pager">
      <Button
        disabled={!search.has("cursor")}
        onClick={() => {
          const next = new URLSearchParams(search);
          next.delete("cursor");
          setSearch(next);
        }}
      >
        <ChevronLeft size={16} />
        返回首页
      </Button>
      <Button
        disabled={!hasMore || !nextCursor}
        onClick={() => {
          const next = new URLSearchParams(search);
          next.set("cursor", nextCursor!);
          setSearch(next);
          window.scrollTo({ top: 0 });
        }}
      >
        下一页
        <ChevronRight size={16} />
      </Button>
    </div>
  );
}
export function JobProgress({
  id,
  onClose,
}: {
  id: string;
  onClose?: () => void;
}) {
  const query = useApi(
    "getJob",
    { path: { job_id: id } },
    {
      poll: (d, age) =>
        d &&
        ["succeeded", "partial", "failed", "expired", "cancelled"].includes(
          d.data.job_state,
        )
          ? false
          : age < 30000
            ? 2000
            : 5000,
    },
  );
  const cancel = useAction<unknown>(),
    finished = useRef(false);
  useEffect(() => {
    if (
      query.data &&
      ["succeeded", "partial", "failed", "expired", "cancelled"].includes(
        query.data.data.job_state,
      ) &&
      !finished.current
    ) {
      finished.current = true;
      void invalidate(
        ...recordOps,
        "getCamera",
        "listCameras",
        "getStatus",
        "listProviders",
        "getSettings",
        "getEntity",
        "listEntities",
      );
    }
  }, [query.data]);
  return (
    <div className="job-progress">
      <QueryState query={query}>
        {({ data: j }) => (
          <>
            <div className="row-between">
              <Badge
                tone={
                  j.job_state === "succeeded"
                    ? "success"
                    : ["failed", "expired"].includes(j.job_state)
                      ? "danger"
                      : "neutral"
                }
              >
                {jobLabels[j.job_state]}
              </Badge>
              <span className="caption">{phaseLabels[j.phase] ?? j.phase}</span>
            </div>
            <p>{j.queue_reason ? queueLabels[j.queue_reason] : ""}</p>
            {j.progress.total_units != null && j.progress.total_units > 0 ? (
              <>
                <progress
                  value={j.progress.completed_units ?? 0}
                  max={j.progress.total_units}
                />
                <p className="caption">
                  {j.progress.completed_units ?? 0} / {j.progress.total_units} ·
                  失败 {j.progress.failed_units ?? 0} · 过期{" "}
                  {j.progress.expired_units ?? 0}
                </p>
              </>
            ) : (
              <p className="caption muted">总量尚未确定，不显示百分比。</p>
            )}
            {j.error && (
              <Notice tone="warning">
                <p>{String(j.error.message ?? "任务处理失败")}</p>
                {typeof j.error.next_retry_at === "string" && (
                  <p>将在 {dateTime(j.error.next_retry_at)} 重试</p>
                )}
              </Notice>
            )}
            <p className="caption mono">任务 {j.id}</p>
            {j.result_links.map((r, i) => {
              const href = sourceHref(r);
              return href ? (
                <ScopeLink className="text-link" key={i} to={href}>
                  查看任务结果
                  <ArrowRight size={14} />
                </ScopeLink>
              ) : null;
            })}
            <ErrorNotice error={cancel.error} />
            <div className="actions">
              {["queued", "running"].includes(j.job_state) && (
                <Button
                  busy={cancel.pending}
                  disabled={j.cancel_requested}
                  onClick={() =>
                    void cancel.run(["cancel", id], async (key) => {
                      await api("cancelJob", {
                        path: { job_id: id },
                        intent: key,
                      });
                      await query.refetch();
                    })
                  }
                >
                  {j.cancel_requested ? "正在请求取消" : "取消任务"}
                </Button>
              )}
              {onClose && <Button onClick={onClose}>关闭进度</Button>}
            </div>
            <p className="caption muted">
              关闭面板后任务继续在服务器执行。已发出的调用可能仍计入用量。
            </p>
          </>
        )}
      </QueryState>
    </div>
  );
}
export function JobModal({ id, onClose }: { id: string; onClose: () => void }) {
  return (
    <Modal title="任务进度" onClose={onClose}>
      <JobProgress id={id} onClose={onClose} />
    </Modal>
  );
}
export function ImpactDetails({ impact }: { impact: ImpactPreviewDto }) {
  return (
    <div className="stack">
      <dl className="definition-list">
        <div>
          <dt>影响机位</dt>
          <dd>{impact.affected_camera_ids.length} 台</dd>
        </div>
        <div>
          <dt>将立即到期图片</dt>
          <dd>{impact.will_expire_frame_count} 张</dd>
        </div>
        <div>
          <dt>需要重新连接</dt>
          <dd>{impact.requires_reconnect ? "是" : "否"}</dd>
        </div>
        <div>
          <dt>需要复查区域</dt>
          <dd>{impact.requires_roi_review ? "是" : "否"}</dd>
        </div>
        <div>
          <dt>确认有效至</dt>
          <dd>{clock(impact.expires_at, true)} 北京时间</dd>
        </div>
      </dl>
      {Object.entries(impact.estimated_media_bytes).map(([key, value]) => (
        <p className="caption" key={key}>
          预计空间 {key}：{String(value)}
        </p>
      ))}
      {impact.warnings.map((w) => (
        <Notice key={w.code} tone="warning">
          {w.message}
        </Notice>
      ))}
      {impact.blocking_errors.map((e, i) => (
        <Notice key={i} tone="danger">
          {String(e.message ?? e.code ?? "预检未通过")}
        </Notice>
      ))}
    </div>
  );
}
export function sourceHref(ref: Record<string, unknown>) {
  const kind = ref.kind ?? ref.type ?? ref.resource_kind;
  const id =
    ref.id ??
    ref.event_id ??
    ref.minute_id ??
    ref.summary_id ??
    ref.resource_id;
  const prefix =
    kind === "event"
      ? "events"
      : kind === "minute"
        ? "minutes"
        : kind === "hour" || kind === "day" || kind === "summary"
          ? "reports"
          : kind === "camera"
            ? "cameras"
            : kind === "entity"
              ? "entities"
              : null;
  if (kind === "provider" && typeof id === "string") return "/settings/ai";
  if (kind === "job" && typeof id === "string")
    return `/status?job_id=${encodeURIComponent(id)}`;
  if (!prefix || typeof id !== "string" || !id) return null;
  return `/${prefix}/${encodeURIComponent(id)}${typeof ref.revision === "number" && ref.revision > 0 ? `?revision=${ref.revision}` : ""}`;
}
export function SourceLinks({ refs }: { refs: Record<string, unknown>[] }) {
  return (
    <div className="source-links">
      {refs.map((ref, i) => {
        const href = sourceHref(ref);
        return href ? (
          <ScopeLink key={i} to={href}>
            {String(ref.name ?? ref.id ?? ref.minute_id ?? "来源记录")}{" "}
            {typeof ref.revision === "number" ? `· v${ref.revision}` : ""}
            <ArrowRight size={14} />
          </ScopeLink>
        ) : (
          <span className="caption muted" key={i}>
            来源元数据：{String(ref.kind ?? ref.type ?? "未知资源")}
          </span>
        );
      })}
    </div>
  );
}
export function CheckItem({
  done,
  children,
}: {
  done: boolean;
  children: ReactNode;
}) {
  return (
    <div className={`setup-step ${done ? "done" : ""}`}>
      <span>{done ? <Check size={16} /> : <span className="step-dot" />}</span>
      {children}
    </div>
  );
}
export function BackLink({
  to,
  label = "返回列表",
}: {
  to: string;
  label?: string;
}) {
  return (
    <ScopeLink className="back-link" to={to}>
      <ChevronLeft size={16} />
      {label}
    </ScopeLink>
  );
}
