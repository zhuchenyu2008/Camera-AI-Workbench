import { useState } from "react";
import { useParams } from "react-router-dom";
import { FilePenLine, ImageOff, ScanLine } from "lucide-react";
import { api, ApiError } from "../../api/client";
import { invalidate, recordOps, useApi } from "../../api/query";
import type {
  CorrectionInterval,
  EventDto,
  EventUpdateDto,
} from "../../api/generated/types";
import { activities, evidenceLabels } from "../../shared/labels";
import { useAction } from "../../shared/hooks";
import {
  BackLink,
  Badge,
  Button,
  ErrorNotice,
  Field,
  Modal,
  Notice,
  PageHeading,
  Panel,
  QueryState,
  RevisionBanner,
  RevisionPicker,
  ScopeLink,
  SourceLinks,
  useScope,
  JobProgress,
} from "../../shared/components";
import {
  beijingInput,
  clock,
  dateTime,
  duration,
  toInput,
} from "../../shared/time";
import { EvidenceViewer } from "../../shared/media/EvidenceViewer";
import { AnalysisDialog } from "../analysis/AnalysisDialog";

function editableIntervals(event: EventDto): CorrectionInterval[] {
  const source = event.segments.length
    ? event.segments.map((s) => ({
        subject_id: s.subject_id,
        activity: s.activity,
        labels: s.labels,
        object_ids: s.object_ids,
        start_at: s.start_at,
        end_at: s.end_at,
      }))
    : [
        {
          subject_id: event.subject_id,
          activity: event.activity,
          labels: event.labels,
          object_ids: event.object_ids,
          start_at: event.start_at,
          end_at: event.end_at,
        },
      ];
  const merged: CorrectionInterval[] = [];
  for (const s of source) {
    const prev = merged.at(-1);
    if (
      prev &&
      prev.end_at === s.start_at &&
      prev.subject_id === s.subject_id &&
      prev.activity === s.activity &&
      JSON.stringify(prev.labels) === JSON.stringify(s.labels) &&
      JSON.stringify(prev.object_ids) === JSON.stringify(s.object_ids)
    )
      prev.end_at = s.end_at;
    else merged.push({ ...s });
  }
  return merged;
}

export function EventDetail() {
  const { eventId = "" } = useParams(),
    scope = useScope(),
    revision = Number(scope.search.get("revision")) || undefined;
  const q = useApi("getEvent", {
      path: { event_id: eventId },
      query: { revision },
    }),
    [editing, setEditing] = useState(false),
    [analysis, setAnalysis] = useState<"visual" | "records_only" | null>(null),
    [image, setImage] = useState<number | null>(null);
  return (
    <>
      <BackLink to="/timeline" label="返回时间线" />
      <QueryState query={q}>
        {({ data: e }) => {
          const ids = [
              ...new Set(e.segments.flatMap((s) => s.evidence_ids)),
            ].slice(0, 3),
            historical = revision != null;
          return (
            <>
              <PageHeading
                kicker="ACTIVITY / EVIDENCE"
                title={activities[e.activity]}
                description={`${clock(e.start_at, true)}—${clock(e.end_at, true)} · ${duration(e.duration_ms)} · 北京时间`}
                action={<RevisionPicker current={e.revision} />}
              />
              <RevisionBanner
                revision={e.revision}
                historical={historical}
                stale={e.source_state === "stale"}
                onCurrent={() => scope.update("revision", "")}
              />
              {e.identity_status !== "matched" && (
                <Notice tone="warning">
                  {e.identity_status === "possible"
                    ? "人物可能匹配"
                    : "人物未确认"}
                  ，未计入该人物的确定活动时长。
                </Notice>
              )}
              {e.replacement_event_ids.length > 0 && (
                <Notice title="此事件已被拆分或合并">
                  <p>历史内容仍可阅读，请查看替代事件：</p>
                  {e.replacement_event_ids.map((id) => (
                    <ScopeLink
                      className="text-link"
                      key={id}
                      to={`/events/${id}`}
                    >
                      {id}
                    </ScopeLink>
                  ))}
                </Notice>
              )}
              <div className="detail-columns">
                <div className="stack">
                  <Panel
                    title="活动与中断"
                    action={
                      <Badge
                        tone={e.has_manual_revision ? "success" : "neutral"}
                      >
                        {e.has_manual_revision ? "人工修订" : "AI 观察"}
                      </Badge>
                    }
                  >
                    <dl className="definition-list">
                      <div>
                        <dt>起止时间</dt>
                        <dd>
                          {clock(e.start_at, true)}—{clock(e.end_at, true)}
                        </dd>
                      </div>
                      <div>
                        <dt>有效活动时长</dt>
                        <dd>{duration(e.duration_ms)}</dd>
                      </div>
                      <div>
                        <dt>证据状态</dt>
                        <dd>{evidenceLabels[e.evidence_state]}</dd>
                      </div>
                    </dl>
                    {e.interruptions.length > 0 ? (
                      <div className="stack">
                        {e.interruptions.map((x, i) => (
                          <Notice key={i}>
                            中间{" "}
                            {typeof x.duration_ms === "number"
                              ? duration(x.duration_ms, false)
                              : `${typeof x.start_at === "string" ? clock(x.start_at, true) : ""}—${typeof x.end_at === "string" ? clock(x.end_at, true) : ""}`}{" "}
                            {String(x.reason ?? x.activity ?? "无法判断")}
                            ；有效时长已排除此段。
                          </Notice>
                        ))}
                      </div>
                    ) : (
                      <p className="caption muted">
                        有效时长取自服务端记录，不能按起止时间相减计入活动。
                      </p>
                    )}
                    <div className="observation">
                      {e.segments.map((s, i) => (
                        <p key={s.id || i}>
                          {s.observation || "此片段没有附加可见事实描述。"}
                        </p>
                      ))}
                    </div>
                    {e.labels.length > 0 && (
                      <div className="row-wrap">
                        {e.labels.map((l) => (
                          <Badge key={l}>{l}</Badge>
                        ))}
                      </div>
                    )}
                  </Panel>
                  <Panel title="证据样本">
                    <p className="caption muted">
                      先提供至多 3 个样本入口。完整 0–59
                      秒证据格在来源分钟中查看。
                    </p>
                    {ids.length &&
                    ["available", "partial"].includes(e.evidence_state) ? (
                      <div className="actions">
                        {ids.map((id, i) => (
                          <Button key={id} onClick={() => setImage(i)}>
                            查看样本 {i + 1}
                          </Button>
                        ))}
                      </div>
                    ) : (
                      <EmptyEvidence state={e.evidence_state} />
                    )}
                    <p className="caption muted">
                      原图过期后，活动卡与文字记录仍然保留。
                    </p>
                  </Panel>
                  <Panel title="来源分钟">
                    <SourceLinks
                      refs={e.minute_refs.map((r) => ({
                        ...r,
                        kind: "minute",
                        id: r.minute_id ?? r.id,
                      }))}
                    />
                  </Panel>
                </div>
                <div className="stack">
                  <Panel title="记录操作">
                    <div className="stack">
                      <Button
                        disabled={
                          historical ||
                          !["available", "partial"].includes(e.evidence_state)
                        }
                        onClick={() => setAnalysis("visual")}
                      >
                        <ScanLine size={16} />
                        用仍有效的原图重新分析
                      </Button>
                      {!["available", "partial"].includes(e.evidence_state) && (
                        <small className="muted">
                          {evidenceLabels[e.evidence_state]}
                          ，无法重新识别旧画面。
                        </small>
                      )}
                      <Button onClick={() => setAnalysis("records_only")}>
                        按现有记录重新汇总
                      </Button>
                      <Button
                        disabled={historical}
                        onClick={() => setEditing(true)}
                      >
                        <FilePenLine size={16} />
                        人工修订
                      </Button>
                    </div>
                  </Panel>
                  <Panel title="版本与来源">
                    <dl className="definition-list">
                      <div>
                        <dt>记录版本</dt>
                        <dd>v{e.revision}</dd>
                      </div>
                      <div>
                        <dt>来源版本</dt>
                        <dd>{e.source_revision}</dd>
                      </div>
                      <div>
                        <dt>人工修订</dt>
                        <dd>
                          {e.has_manual_revision
                            ? "当前人工结果有效"
                            : "无人工修订"}
                        </dd>
                      </div>
                    </dl>
                    <p className="caption muted">
                      历史版本可通过上方选择器只读查看；重新运行 AI
                      不会自动撤销有效人工修订。
                    </p>
                  </Panel>
                </div>
              </div>
              {editing && (
                <CorrectionEditor event={e} onClose={() => setEditing(false)} />
              )}{" "}
              {analysis && (
                <AnalysisDialog
                  mode={analysis}
                  cameraIds={e.camera_ids}
                  from={e.start_at}
                  to={e.end_at}
                  onClose={() => setAnalysis(null)}
                />
              )}{" "}
              {image != null && (
                <EvidenceViewer
                  ids={ids}
                  index={image}
                  onIndex={setImage}
                  onClose={() => setImage(null)}
                />
              )}
            </>
          );
        }}
      </QueryState>
    </>
  );
}
function EmptyEvidence({ state }: { state: string }) {
  return (
    <div className="empty-state">
      <ImageOff size={26} />
      <p>{evidenceLabels[state]}；文字记录仍可查看。</p>
    </div>
  );
}
function CorrectionEditor({
  event,
  onClose,
}: {
  event: EventDto;
  onClose: () => void;
}) {
  const entities = useApi("listEntities", { query: { limit: 100 } }),
    settings = useApi("getSettings");
  const [intervals, setIntervals] = useState<CorrectionInterval[]>(() =>
    editableIntervals(event),
  );
  const [reason, setReason] = useState(""),
    [baseline, setBaseline] = useState(event),
    [latest, setLatest] = useState<EventDto | null>(null),
    [jobs, setJobs] = useState<string[]>([]);
  const action = useAction<EventUpdateDto>();
  const change = (i: number, patch: Partial<CorrectionInterval>) =>
    setIntervals((v) => v.map((r, n) => (n === i ? { ...r, ...patch } : r)));
  const valid =
    intervals.length > 0 &&
    intervals.every(
      (r, i) =>
        Date.parse(r.end_at) > Date.parse(r.start_at) &&
        Date.parse(r.start_at) >= Date.parse(baseline.start_at) &&
        Date.parse(r.end_at) <= Date.parse(baseline.end_at) &&
        (i === 0 ||
          Date.parse(r.start_at) >= Date.parse(intervals[i - 1].end_at)),
    );
  return (
    <Modal title="人工修订活动" onClose={onClose} wide>
      {jobs.length ? (
        <div className="stack">
          <Notice>修订已保存，正在更新关联视图和汇总。</Notice>
          {jobs.map((id) => (
            <JobProgress id={id} key={id} />
          ))}
        </div>
      ) : (
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            void action.run(
              [baseline.revision, intervals, reason],
              async (key) => {
                const result = await api("updateEvent", {
                  path: { event_id: event.id },
                  revision: baseline.revision,
                  intent: key,
                  body: {
                    base_source_revision: baseline.source_revision,
                    intervals,
                    reason: reason.trim(),
                  },
                });
                await invalidate(...recordOps);
                if (result.data.jobs.length)
                  setJobs(result.data.jobs.map((j) => j.id));
                else onClose();
                return result.data;
              },
            );
          }}
        >
          <Notice>
            会更新相关分钟视图、事件和汇总。只能修订已有画面支持的范围；采集缺口与计划外区域仍由后端排除。
          </Notice>
          <p className="caption muted">
            原记录：{activities[baseline.activity]} ·{" "}
            {clock(baseline.start_at, true)}—{clock(baseline.end_at, true)} ·{" "}
            {duration(baseline.duration_ms)} · v{baseline.revision}
          </p>
          {intervals.map((r, i) => (
            <div className="schedule-rule" key={i}>
              <div className="row-between">
                <strong>片段 {i + 1}</strong>
                <div className="actions">
                  <Button
                    type="button"
                    onClick={() => {
                      const half =
                        Math.floor(
                          (Date.parse(r.start_at) + Date.parse(r.end_at)) /
                            2000,
                        ) * 1000;
                      if (
                        half <= Date.parse(r.start_at) ||
                        half >= Date.parse(r.end_at)
                      )
                        return;
                      setIntervals((v) => [
                        ...v.slice(0, i),
                        { ...r, end_at: new Date(half).toISOString() },
                        { ...r, start_at: new Date(half).toISOString() },
                        ...v.slice(i + 1),
                      ]);
                    }}
                  >
                    拆成两段
                  </Button>
                  <Button
                    type="button"
                    disabled={intervals.length === 1}
                    onClick={() =>
                      setIntervals((v) => v.filter((_, n) => n !== i))
                    }
                  >
                    移除此段
                  </Button>
                </div>
              </div>
              <div className="form-grid">
                <Field label="人物">
                  <select
                    value={r.subject_id ?? ""}
                    onChange={(e) =>
                      change(i, { subject_id: e.target.value || null })
                    }
                  >
                    <option value="">人物未确认</option>
                    {entities.data?.data
                      .filter((x) => x.kind === "person")
                      .map((x) => (
                        <option value={x.id} key={x.id}>
                          {x.name}
                        </option>
                      ))}
                  </select>
                </Field>
                <Field label="主要活动">
                  <select
                    value={r.activity}
                    onChange={(e) =>
                      change(i, {
                        activity: e.target
                          .value as CorrectionInterval["activity"],
                      })
                    }
                  >
                    {Object.entries(activities).map(([k, v]) => (
                      <option key={k} value={k}>
                        {v}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="开始（北京时间）">
                  <input
                    type="datetime-local"
                    step="1"
                    required
                    value={toInput(r.start_at)}
                    onChange={(e) => {
                      if (e.target.value)
                        change(i, { start_at: beijingInput(e.target.value) });
                    }}
                  />
                </Field>
                <Field label="结束（北京时间）">
                  <input
                    type="datetime-local"
                    step="1"
                    required
                    value={toInput(r.end_at)}
                    onChange={(e) => {
                      if (e.target.value)
                        change(i, { end_at: beijingInput(e.target.value) });
                    }}
                  />
                </Field>
              </div>
              <p className="caption muted">
                编辑跨度{" "}
                {duration(Date.parse(r.end_at) - Date.parse(r.start_at), false)}
                ；最终有效时长以提交后服务端返回为准。
              </p>
              <div className="row-wrap">
                {settings.data?.data.config.activity_labels?.map((label) => (
                  <label className="check-row" key={label}>
                    <input
                      type="checkbox"
                      checked={r.labels?.includes(label) ?? false}
                      onChange={(e) =>
                        change(i, {
                          labels: e.target.checked
                            ? [...(r.labels ?? []), label]
                            : (r.labels ?? []).filter((x) => x !== label),
                        })
                      }
                    />
                    {label}
                  </label>
                ))}
              </div>
            </div>
          ))}
          {!valid && (
            <Notice tone="danger">
              片段须按时间排序、不重叠，且位于原事件边界内。不能增加未经画面支持的计时。
            </Notice>
          )}
          <Field label="修订原因" hint="1–500 字；历史版本中保留修订来源。">
            <textarea
              required
              minLength={1}
              maxLength={500}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <ErrorNotice error={action.error} />
          {action.error instanceof ApiError && action.error.status === 412 && (
            <Button
              type="button"
              onClick={() =>
                void api("getEvent", { path: { event_id: event.id } }).then(
                  (r) => setLatest(r.data),
                )
              }
            >
              读取服务器新版并对比
            </Button>
          )}
          {latest && (
            <>
              <div className="conflict-diff">
                <div>
                  <strong>草稿基线 v{baseline.revision}</strong>
                  <p>
                    {activities[baseline.activity]} ·{" "}
                    {duration(baseline.duration_ms)}
                  </p>
                </div>
                <div>
                  <strong>服务器 v{latest.revision}</strong>
                  <p>
                    {activities[latest.activity]} ·{" "}
                    {duration(latest.duration_ms)}
                  </p>
                  <p>
                    {clock(latest.start_at, true)}—{clock(latest.end_at, true)}
                  </p>
                  {latest.segments.map((s) => (
                    <p key={s.id}>{s.observation}</p>
                  ))}
                </div>
              </div>
              <Button
                type="button"
                onClick={() => {
                  setBaseline(latest);
                  setLatest(null);
                  action.setError(null);
                }}
              >
                已核对差异，保留草稿并采用新版基线
              </Button>
            </>
          )}
          <div className="actions end">
            <Button type="button" onClick={onClose}>
              取消
            </Button>
            <Button
              type="submit"
              variant="primary"
              busy={action.pending}
              disabled={
                !valid ||
                !reason.trim() ||
                Boolean(latest) ||
                (action.error instanceof ApiError &&
                  action.error.status === 412)
              }
            >
              保存人工修订
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
