import { useState } from "react";
import { api, ApiError } from "../../api/client";
import { invalidate, invalidateMedia, useApi } from "../../api/query";
import type {
  SettingsDto,
  SettingsConfig,
  ImpactPreviewDto,
} from "../../api/generated/types";
import { fields } from "../../api/generated/fields";
import { useAction, useUnsaved } from "../../shared/hooks";
import {
  Badge,
  Button,
  Confirm,
  ErrorNotice,
  Field,
  ImpactDetails,
  JobModal,
  Modal,
  Notice,
  PageHeading,
  Panel,
  QueryState,
} from "../../shared/components";
import { serverNow } from "../../shared/time";
const groups: {
  title: string;
  items: [keyof SettingsConfig, string, string?][];
}[] = [
  {
    title: "图片保存",
    items: [
      [
        "frame_retention_minutes",
        "现场图保存（分钟）",
        "缩短会使部分现存图片立即到期",
      ],
      ["frame_max_long_edge", "现场图最长边（像素）"],
      ["cleanup_interval_seconds", "到期清理间隔（秒）"],
    ],
  },
  {
    title: "分析频率与变化检测",
    items: [
      ["sparse_submit_interval_minutes", "稀疏批量提交周期（分钟）"],
      ["pre_trigger_seconds", "变化前回溯（秒）"],
      ["stable_before_settling_seconds", "进入稳定前等待（秒）"],
      ["post_trigger_seconds", "变化后补采（秒）"],
      ["motion_pixel_delta", "像素变化阈值"],
      ["motion_area_ratio", "连续帧变化面积比"],
      ["motion_single_frame_ratio", "单帧变化面积比"],
    ],
  },
  {
    title: "事件合并与推断",
    items: [
      ["merge_gap_seconds", "允许的短中断（秒）"],
      ["max_inference_gap_seconds", "最大推断间隔（秒）"],
    ],
  },
  {
    title: "汇总计划",
    items: [
      ["hourly_summary_delay_minutes", "小时汇总延迟（分钟）"],
      ["daily_summary_local_time", "日报生成时间（北京时间）"],
    ],
  },
  {
    title: "AI 资源限制",
    items: [
      ["ai_concurrency", "AI 并发数"],
      ["max_capture_images_per_request", "单次现场图上限"],
      ["max_total_images_per_request", "单次总图上限"],
      ["max_request_body_mib", "单次请求体（MiB）"],
      ["ai_timeout_seconds", "AI 超时（秒）"],
      ["ai_retry_count", "AI 重试次数"],
    ],
  },
  {
    title: "磁盘与诊断",
    items: [
      ["media_quota_gb", "现场图配额（GB）"],
      ["reference_quota_gb", "参考图配额（GB）"],
      ["disk_pressure_free_gb", "磁盘压力阈值（GB）"],
      ["disk_stop_free_gb", "停止新图阈值（GB）"],
      ["source_stale_seconds", "画面停止更新阈值（秒）"],
      ["detailed_audit_days", "详细诊断保留（天）"],
    ],
  },
];
export function AnalysisSettings() {
  const q = useApi("getSettings");
  return (
    <>
      <PageHeading
        kicker="SETTINGS / ANALYSIS & RETENTION"
        title="分析与保存"
        description="参数、默认值和有效上限来自服务器；保存前检查具体影响。"
      />
      <QueryState query={q}>
        {({ data }) => <SettingsForm settings={data} />}
      </QueryState>
    </>
  );
}
function SettingsForm({ settings }: { settings: SettingsDto }) {
  const [base, setBase] = useState(settings),
    [draft, setDraft] = useState<SettingsConfig>(settings.config),
    [impact, setImpact] = useState<ImpactPreviewDto | null>(null),
    [pending, setPending] = useState<Record<string, unknown> | null>(null),
    [message, setMessage] = useState(""),
    [job, setJob] = useState<string | null>(null),
    [latest, setLatest] = useState<SettingsDto | null>(null);
  const providers = useApi("listProviders", { query: { limit: 100 } }),
    action = useAction<unknown>();
  const patch = Object.fromEntries(
    Object.entries(draft).filter(
      ([key, value]) =>
        JSON.stringify(value) !==
        JSON.stringify(base.config[key as keyof SettingsConfig]),
    ),
  );
  const dirty = Object.keys(patch).length > 0,
    blocker = useUnsaved(dirty);
  const update = (key: keyof SettingsConfig, value: unknown) =>
    setDraft({ ...draft, [key]: value });
  const preflight = async (p: Record<string, unknown>) => {
    const r = await api("previewSettings", {
      body: { base_revision: base.revision, patch: p },
    });
    setImpact(r.data);
    setPending(p);
  };
  return (
    <>
      <Panel title="当前采集与分析策略">
        <div className="row-wrap">
          <Badge>逐秒采集 {settings.config.capture_fps} fps</Badge>
          <Badge>
            稀疏 {settings.config.sparse_sample_offsets_seconds?.length ?? "—"}{" "}
            点 / 分钟
          </Badge>
          <Badge>
            每 {settings.config.sparse_submit_interval_minutes} 分钟批量复核
          </Badge>
          <Badge>现场图 {settings.config.frame_retention_minutes} 分钟</Badge>
          <Badge>北京时间</Badge>
        </div>
        <p className="caption muted" style={{ marginTop: 12 }}>
          采集频率固定为每秒一张，时区固定为北京时间。现场图保留{" "}
          {settings.config.frame_retention_minutes} 分钟；文字记录永久保留。
        </p>
      </Panel>
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          void action.run([base.revision, patch], () => preflight(patch));
        }}
      >
        <Panel title="AI 服务用途">
          <div className="form-grid">
            {(["vision_provider_id", "summary_provider_id"] as const).map(
              (key) => (
                <Field
                  key={key}
                  label={
                    key === "vision_provider_id"
                      ? "视觉分析服务"
                      : "小时 / 日报服务"
                  }
                >
                  <select
                    value={draft[key] ?? ""}
                    onChange={(e) => update(key, e.target.value || null)}
                  >
                    <option value="">未配置</option>
                    {providers.data?.data.map((p) => (
                      <option
                        value={p.id}
                        key={p.id}
                        disabled={
                          p.tested_config_revision !== p.revision ||
                          !p.capabilities.vision
                        }
                      >
                        {p.name} · {p.model}
                        {p.tested_config_revision !== p.revision
                          ? "（当前配置待测试）"
                          : ""}
                      </option>
                    ))}
                  </select>
                </Field>
              ),
            )}
          </div>
        </Panel>
        {groups.map((group) => (
          <Panel title={group.title} key={group.title}>
            <div className="form-grid">
              {group.items.map(([key, label, hint]) => {
                const def = fields.SettingsConfig[
                  key as keyof typeof fields.SettingsConfig
                ] as {
                  minimum?: number;
                  maximum?: number;
                  exclusiveMinimum?: number;
                  type?: string;
                };
                const value = draft[key];
                return (
                  <Field
                    label={label}
                    key={key}
                    hint={
                      <>
                        {hint}
                        {key === "frame_retention_minutes" &&
                          typeof settings.validation_hints
                            .minimum_retention_minutes === "number" && (
                            <span>
                              {" "}
                              当前已保存配置最低{" "}
                              {
                                settings.validation_hints
                                  .minimum_retention_minutes
                              }{" "}
                              分钟；改变超时、采样或重试后，草稿会重新预检。
                            </span>
                          )}
                        {base.defaults[key] != null && (
                          <Button
                            type="button"
                            variant="ghost"
                            onClick={() => update(key, base.defaults[key])}
                          >
                            恢复默认 {String(base.defaults[key])}
                          </Button>
                        )}
                      </>
                    }
                  >
                    <input
                      name={key}
                      required
                      type={
                        key === "daily_summary_local_time" ? "time" : "number"
                      }
                      min={
                        def.minimum ??
                        (def.exclusiveMinimum != null
                          ? def.exclusiveMinimum + 0.001
                          : undefined)
                      }
                      max={def.maximum}
                      step={
                        key === "daily_summary_local_time"
                          ? undefined
                          : def.type === "integer"
                            ? 1
                            : "any"
                      }
                      value={value == null ? "" : String(value)}
                      onChange={(e) =>
                        update(
                          key,
                          key === "daily_summary_local_time"
                            ? e.target.value
                            : e.target.value === ""
                              ? undefined
                              : Number(e.target.value),
                        )
                      }
                    />
                  </Field>
                );
              })}
            </div>
            {group.title === "分析频率与变化检测" && (
              <Field
                label="分钟内稀疏采样秒位"
                hint="升序、不重复，0–59，至少 4 个；最大间隔受推断参数约束。"
              >
                <input
                  name="sparse_sample_offsets_seconds"
                  required
                  pattern="[0-9 ,，]+"
                  value={draft.sparse_sample_offsets_seconds?.join(", ") ?? ""}
                  onChange={(e) =>
                    update(
                      "sparse_sample_offsets_seconds",
                      e.target.value
                        .split(/[,， ]+/)
                        .filter(Boolean)
                        .map(Number),
                    )
                  }
                />
              </Field>
            )}
          </Panel>
        ))}
        <Panel title="自动人物与物品档案">
          <label className="check-row">
            <input
              type="checkbox"
              checked={draft.auto_discovery_enabled ?? true}
              onChange={(e) =>
                update("auto_discovery_enabled", e.target.checked)
              }
            />
            随画面分析自动建档并保存代表图
          </label>
          <p className="caption muted">
            自动记录人物和活动相关物品，之后可改名、合并或拆分。关闭后仍正常记录活动，已有档案继续保留。
          </p>
        </Panel>
        <Panel title="附加活动标签（可选）">
          <Field
            label="标签词表"
            hint="留空时由模型自由生成。每行一个，填写后仅限制附加标签，不限制活动名称。"
          >
            <textarea
              name="activity_labels"
              value={draft.activity_labels?.join("\n") ?? ""}
              onChange={(e) =>
                update(
                  "activity_labels",
                  e.target.value.split("\n").filter(Boolean),
                )
              }
            />
          </Field>
        </Panel>
        <Panel title="实际有效上限">
          <p className="caption muted">
            全局配置与当前 Provider 配置共同限制；以下为后端计算结果。
          </p>
          <div className="stack" style={{ gap: 16 }}>
            <div>
              <strong>视觉分析</strong>
              {settings.effective_limits.vision &&
              typeof settings.effective_limits.vision === "object" ? (
                <dl className="definition-list" style={{ marginTop: 8 }}>
                  <div>
                    <dt>总图上限</dt>
                    <dd>
                      {String(
                        (
                          settings.effective_limits.vision as Record<
                            string,
                            unknown
                          >
                        ).max_total_images,
                      )}{" "}
                      张
                    </dd>
                  </div>
                  <div>
                    <dt>现场图上限</dt>
                    <dd>
                      {String(
                        (
                          settings.effective_limits.vision as Record<
                            string,
                            unknown
                          >
                        ).max_capture_images,
                      )}{" "}
                      张
                    </dd>
                  </div>
                  <div>
                    <dt>请求体上限</dt>
                    <dd>
                      {(
                        Number(
                          (
                            settings.effective_limits.vision as Record<
                              string,
                              unknown
                            >
                          ).max_request_body_bytes,
                        ) /
                        1024 /
                        1024
                      ).toFixed(1)}{" "}
                      MiB（
                      {Number(
                        (
                          settings.effective_limits.vision as Record<
                            string,
                            unknown
                          >
                        ).max_request_body_bytes,
                      ).toLocaleString()}{" "}
                      字节）
                    </dd>
                  </div>
                  <div>
                    <dt>超时</dt>
                    <dd>
                      {String(
                        (
                          settings.effective_limits.vision as Record<
                            string,
                            unknown
                          >
                        ).timeout_seconds,
                      )}{" "}
                      秒
                    </dd>
                  </div>
                </dl>
              ) : (
                <p className="caption muted" style={{ marginTop: 6 }}>
                  尚未选择视觉服务
                </p>
              )}
            </div>
            <div>
              <strong>汇总</strong>
              {settings.effective_limits.summary &&
              typeof settings.effective_limits.summary === "object" ? (
                <dl className="definition-list" style={{ marginTop: 8 }}>
                  <div>
                    <dt>总图上限</dt>
                    <dd>
                      {String(
                        (
                          settings.effective_limits.summary as Record<
                            string,
                            unknown
                          >
                        ).max_total_images,
                      )}{" "}
                      张
                    </dd>
                  </div>
                  <div>
                    <dt>现场图上限</dt>
                    <dd>
                      {String(
                        (
                          settings.effective_limits.summary as Record<
                            string,
                            unknown
                          >
                        ).max_capture_images,
                      )}{" "}
                      张
                    </dd>
                  </div>
                  <div>
                    <dt>请求体上限</dt>
                    <dd>
                      {(
                        Number(
                          (
                            settings.effective_limits.summary as Record<
                              string,
                              unknown
                            >
                          ).max_request_body_bytes,
                        ) /
                        1024 /
                        1024
                      ).toFixed(1)}{" "}
                      MiB（
                      {Number(
                        (
                          settings.effective_limits.summary as Record<
                            string,
                            unknown
                          >
                        ).max_request_body_bytes,
                      ).toLocaleString()}{" "}
                      字节）
                    </dd>
                  </div>
                  <div>
                    <dt>超时</dt>
                    <dd>
                      {String(
                        (
                          settings.effective_limits.summary as Record<
                            string,
                            unknown
                          >
                        ).timeout_seconds,
                      )}{" "}
                      秒
                    </dd>
                  </div>
                </dl>
              ) : (
                <p className="caption muted" style={{ marginTop: 6 }}>
                  尚未选择汇总服务
                </p>
              )}
            </div>
          </div>
          <details className="diagnostic">
            <summary>查看服务端校验提示</summary>
            <pre>{JSON.stringify(settings.validation_hints, null, 2)}</pre>
          </details>
        </Panel>
        <ErrorNotice error={action.error} />
        {message && <Notice tone="warning">{message}</Notice>}
        {action.error instanceof ApiError && action.error.status === 412 && (
          <Button
            type="button"
            onClick={() =>
              void api("getSettings").then((r) => setLatest(r.data))
            }
          >
            读取新版，对比并保留草稿
          </Button>
        )}
        {latest && (
          <Panel title={`服务器新版 v${latest.revision}`}>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>字段</th>
                    <th>原基线</th>
                    <th>服务器新版</th>
                    <th>当前草稿</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.keys(latest.config)
                    .filter(
                      (k) =>
                        JSON.stringify(
                          latest.config[k as keyof SettingsConfig],
                        ) !==
                        JSON.stringify(base.config[k as keyof SettingsConfig]),
                    )
                    .map((k) => (
                      <tr key={k}>
                        <td>{k}</td>
                        <td>
                          {JSON.stringify(
                            base.config[k as keyof SettingsConfig],
                          )}
                        </td>
                        <td>
                          {JSON.stringify(
                            latest.config[k as keyof SettingsConfig],
                          )}
                        </td>
                        <td>
                          {JSON.stringify(draft[k as keyof SettingsConfig])}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
            <Button
              type="button"
              onClick={() => {
                setBase(latest);
                setDraft({ ...latest.config, ...patch });
                setLatest(null);
                setImpact(null);
                action.setError(null);
              }}
            >
              已核对差异，将草稿变更应用于新版基线
            </Button>
          </Panel>
        )}
        <div className="save-bar">
          <p>
            v{base.revision} ·{" "}
            {dirty
              ? `${Object.keys(patch).length} 项未保存变更`
              : "当前设置已保存"}
          </p>
          <Button
            type="submit"
            variant="primary"
            busy={action.pending}
            disabled={!dirty || !!latest}
          >
            预检保存影响
          </Button>
        </div>
      </form>
      {impact && pending && (
        <Modal title="确认设置影响" onClose={() => setImpact(null)}>
          <div className="stack">
            <ImpactDetails impact={impact} />
            <ErrorNotice error={action.error} />
            {message && <Notice tone="warning">{message}</Notice>}
            <div className="actions end">
              <Button onClick={() => setImpact(null)}>取消</Button>
              <Button
                variant="primary"
                busy={action.pending}
                disabled={impact.blocking_errors.length > 0}
                onClick={() =>
                  void action.run(
                    [base.revision, pending, impact.preview_token],
                    async (key) => {
                      if (Date.parse(impact.expires_at) <= serverNow()) {
                        await preflight(pending);
                        setMessage(
                          "预检已过期，已重新读取影响。请核对后再次确认。",
                        );
                        return;
                      }
                      try {
                        const r = await api("updateSettings", {
                          body: {
                            patch: pending,
                            preview_token: impact.preview_token,
                          },
                          revision: base.revision,
                          intent: key,
                        });
                        setBase(r.data.settings);
                        setDraft(r.data.settings.config);
                        setImpact(null);
                        setPending(null);
                        setMessage("设置已保存。实际生效进度由后台任务确认。");
                        if (r.data.apply_job) setJob(r.data.apply_job.id);
                        await invalidate(
                          "getSettings",
                          "listCameras",
                          "getCamera",
                          "getStatus",
                          "listEntities",
                          "getOverview",
                          "getTimeline",
                        );
                        await invalidateMedia();
                      } catch (e) {
                        if (e instanceof ApiError && e.status === 409) {
                          await preflight(pending);
                          setMessage("预检来源变化，请重新核对新影响。");
                        }
                        throw e;
                      }
                    },
                  )
                }
              >
                {impact.will_expire_frame_count > 0
                  ? "保存并使这些图片到期"
                  : "保存设置"}
              </Button>
            </div>
          </div>
        </Modal>
      )}
      {job && <JobModal id={job} onClose={() => setJob(null)} />}{" "}
      {blocker.state === "blocked" && (
        <Confirm
          title="离开未保存的设置？"
          description={<p>草稿尚未提交到服务器。</p>}
          onClose={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
          label="放弃草稿并离开"
        />
      )}
    </>
  );
}
