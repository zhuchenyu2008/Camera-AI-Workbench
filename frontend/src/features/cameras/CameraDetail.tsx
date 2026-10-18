import { lazy, Suspense, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import { invalidate, invalidateMedia, useApi } from "../../api/query";
import type {
  CameraDto,
  CameraCreate,
  CameraPatch,
  ImpactPreviewDto,
  ScheduleRule,
  Regions,
} from "../../api/generated/types";
import { useAction, useUnsaved } from "../../shared/hooks";
import {
  BackLink,
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
  Skeleton,
} from "../../shared/components";
import { clock, dateTime, serverNow } from "../../shared/time";
import { captureLabel } from "../../shared/labels";
import { CameraPreview } from "../../shared/media/CameraPreview";
import { polygonError } from "../../shared/roi";
import { CameraControl } from "./CameraControl";
import { mergedWeek, scheduleLabel } from "../../shared/schedule";
const RoiEditor = lazy(() =>
  import("./RoiEditor").then((m) => ({ default: m.RoiEditor })),
);
export function CameraDetail() {
  const { cameraId } = useParams();
  const q = useApi(
    "getCamera",
    { path: { camera_id: cameraId ?? "" } },
    { enabled: !!cameraId, poll: 5000 },
  );
  return (
    <>
      <BackLink to="/cameras" label="返回摄像头" />
      {cameraId ? (
        <QueryState query={q}>
          {({ data }) => <CameraForm key={cameraId} camera={data} />}
        </QueryState>
      ) : (
        <CameraForm key="new" />
      )}
    </>
  );
}
function CameraForm({ camera }: { camera?: CameraDto }) {
  const navigate = useNavigate(),
    settings = useApi("getSettings");
  const [base, setBase] = useState(camera),
    [name, setName] = useState(camera?.name ?? ""),
    [port, setPort] = useState(Number(camera?.source.port ?? 15554)),
    [path, setPath] = useState(String(camera?.source.path ?? "/live")),
    [username, setUsername] = useState(String(camera?.source.username ?? "")),
    [password, setPassword] = useState(""),
    [clearSecret, setClearSecret] = useState(false),
    [schedule, setSchedule] = useState<ScheduleRule[]>(camera?.schedule ?? []),
    [regions, setRegions] = useState<Regions>(camera?.regions ?? {}),
    [retention, setRetention] = useState(
      camera?.retention_minutes?.toString() ?? "",
    ),
    [enabled, setEnabled] = useState(camera?.enabled ?? false);
  const [impact, setImpact] = useState<ImpactPreviewDto | null>(null),
    [pendingPatch, setPendingPatch] = useState<CameraPatch | null>(null),
    [job, setJob] = useState<string | null>(null),
    [deleting, setDeleting] = useState(false),
    [message, setMessage] = useState(""),
    [saved, setSaved] = useState(false),
    [roiOpen, setRoiOpen] = useState(false),
    [latest, setLatest] = useState<CameraDto | null>(null);
  const action = useAction<unknown>();
  const draft = {
    name,
    port,
    path,
    username,
    password,
    clearSecret,
    schedule,
    regions,
    retention,
    enabled,
  };
  const original = {
    name: base?.name ?? "",
    port: Number(base?.source.port ?? 15554),
    path: String(base?.source.path ?? "/live"),
    username: String(base?.source.username ?? ""),
    password: "",
    clearSecret: false,
    schedule: base?.schedule ?? [],
    regions: base?.regions ?? {},
    retention: base?.retention_minutes?.toString() ?? "",
    enabled: base?.enabled ?? false,
  };
  const dirty = JSON.stringify(draft) !== JSON.stringify(original) && !saved;
  const blocker = useUnsaved(dirty);
  useEffect(() => {
    setSaved(false);
  }, [JSON.stringify(draft)]);
  const invalidSchedule = schedule.some(
    (r) =>
      !r.weekdays.length ||
      !r.start ||
      !r.end ||
      (!r.end_day_offset && r.end <= r.start),
  );
  const invalidRoi = [
    ...(regions.include ?? []),
    ...(regions.exclude ?? []),
  ].some((p) => polygonError(p.points));
  const evidenceId =
    typeof camera?.last_test?.evidence_id === "string"
      ? camera.last_test.evidence_id
      : null;
  const evidence = useApi(
    "getEvidenceMetadata",
    { path: { evidence_id: evidenceId ?? "" } },
    { enabled: !!evidenceId && roiOpen, gcTime: 0 },
  );
  const patch = (): CameraPatch => {
    const sourceChanged =
      !base ||
      port !== base.source.port ||
      path !== base.source.path ||
      username !== base.source.username ||
      password ||
      clearSecret;
    return {
      ...(name !== base?.name ? { name } : {}),
      ...(sourceChanged
        ? {
            source: {
              host: "127.0.0.1",
              port,
              path,
              username,
              ...(password ? { password } : {}),
              credential_action: clearSecret ? "clear" : "keep",
            },
          }
        : {}),
      ...(JSON.stringify(schedule) !== JSON.stringify(base?.schedule)
        ? { schedule }
        : {}),
      ...(JSON.stringify(regions) !== JSON.stringify(base?.regions)
        ? { regions }
        : {}),
      ...((retention ? Number(retention) : null) !== base?.retention_minutes
        ? { retention_minutes: retention ? Number(retention) : null }
        : {}),
      ...(enabled !== base?.enabled ? { enabled } : {}),
    };
  };
  const save = async (key: string, p: CameraPatch, token?: string) => {
    const r = await api("updateCamera", {
      path: { camera_id: base!.id },
      body: { ...p, ...(token ? { preview_token: token } : {}) },
      revision: base!.revision,
      intent: key,
    });
    setBase(r.data);
    setPassword("");
    setClearSecret(false);
    setImpact(null);
    setPendingPatch(null);
    setSaved(true);
    setMessage("配置已保存，实际采集状态等待后台应用。");
    await invalidate("getCamera", "listCameras", "getStatus", "getOverview");
    await invalidateMedia();
  };
  const prepare = async () => {
    const p = patch();
    if (!Object.keys(p).length) return;
    const needs = ["source", "enabled", "retention_minutes"].some(
      (k) => k in p,
    );
    if (needs) {
      const r = await api("previewCameraUpdate", {
        path: { camera_id: base!.id },
        body: { base_revision: base!.revision, patch: p },
      });
      setPendingPatch(p);
      setImpact(r.data);
    } else await action.run([base!.revision, p], (key) => save(key, p));
  };
  return (
    <>
      <PageHeading
        kicker="CAMERA / CONFIGURATION"
        title={base ? base.name : "添加摄像头"}
        description="先保存停用草稿，测试通过后配置计划、检测区域和启用预检。"
        action={
          camera ? (
            <Badge
              tone={
                camera.runtime.capture_state === "online"
                  ? "success"
                  : "neutral"
              }
            >
              {captureLabel(camera.runtime)}
            </Badge>
          ) : undefined
        }
      />
      {camera && (
        <Panel title="当前预览" action={<CameraControl camera={camera} />}>
          <CameraPreview camera={camera} />
          {camera.last_test && (
            <div className="resource-meta">
              <span>测试：{dateTime(String(camera.last_test.tested_at))}</span>
              <span>编码 {String(camera.last_test.codec ?? "未知")}</span>
              <span>
                {String(camera.last_test.width ?? "—")} ×{" "}
                {String(camera.last_test.height ?? "—")}
              </span>
            </div>
          )}
        </Panel>
      )}
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          if (invalidSchedule || invalidRoi) return;
          void action.run([base?.revision, draft], async (key) => {
            if (!base) {
              const body: CameraCreate = {
                name,
                source: {
                  host: "127.0.0.1",
                  port,
                  path,
                  username,
                  ...(password ? { password } : {}),
                },
                schedule,
                enabled: false,
                retention_minutes: retention ? Number(retention) : null,
                regions,
              };
              const r = await api("createCamera", { body, intent: key });
              setSaved(true);
              blocker.allowNavigation();
              await invalidate("listCameras");
              navigate(`/cameras/${r.data.id}`, { replace: true });
            } else {
              const p = patch();
              if (
                ["source", "enabled", "retention_minutes"].some((k) => k in p)
              ) {
                const r = await api("previewCameraUpdate", {
                  path: { camera_id: base.id },
                  body: { base_revision: base.revision, patch: p },
                });
                setPendingPatch(p);
                setImpact(r.data);
              } else await save(key, p);
            }
          });
        }}
      >
        <Panel title="1 · 名称与视频来源">
          <div className="form-grid">
            <Field label="机位名称">
              <input
                name="name"
                required
                maxLength={64}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field label="Visitor 主机">
              <input readOnly value="127.0.0.1" />
            </Field>
            <Field
              label="允许的本机端口"
              hint="端口须已在部署白名单中配置，例如 15554。"
            >
              <input
                name="source.port"
                type="number"
                min="1"
                max="65535"
                required
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
              />
            </Field>
            <Field
              label="RTSP 路径"
              hint="只填写 / 开头的路径及查询，不填写完整 RTSP URL。"
            >
              <input
                name="source.path"
                required
                pattern="/[^\\s]*"
                maxLength={2048}
                value={path}
                onChange={(e) => setPath(e.target.value)}
              />
            </Field>
            <Field label="摄像头用户名">
              <input
                autoComplete="off"
                value={username}
                maxLength={128}
                onChange={(e) => setUsername(e.target.value)}
              />
            </Field>
            <Field
              label="摄像头密码"
              hint={
                base?.credential_configured
                  ? "已配置；留空表示不修改"
                  : "密码仅提交到后端，不写入浏览器存储"
              }
            >
              <input
                type="password"
                autoComplete="new-password"
                maxLength={256}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
          </div>
          {base?.credential_configured && (
            <label className="check-row">
              <input
                type="checkbox"
                checked={clearSecret}
                onChange={(e) => setClearSecret(e.target.checked)}
              />
              明确清除已保存密码
            </label>
          )}
          <p className="caption muted" style={{ marginTop: 16 }}>
            家庭摄像头经软路由和 frp 接入此端口，RTSP 只由后端读取。
            <a
              className="text-link"
              href="https://github.com/zhuchenyu2008/Camera-AI-Workbench/blob/main/docs/02-%E7%9B%91%E6%8E%A7%E8%A7%86%E9%A2%91%E6%B5%81%E8%BF%9C%E7%A8%8B%E6%8E%A5%E5%85%A5%E4%B8%8E%E9%83%A8%E7%BD%B2%E6%96%B9%E6%A1%88.md"
              target="_blank"
              rel="noreferrer"
            >
              远程接入说明 ↗
            </a>
          </p>
        </Panel>
        {base && (
          <Panel title="2 · 有限时连接测试">
            <p className="caption muted">
              使用已保存的来源，测试连接、编码、尺寸和取图。测试成功不会自动启用。H.265
              无法浏览器播放时仍可使用快照。
            </p>
            <Button
              type="button"
              disabled={dirty}
              busy={action.pending}
              onClick={() =>
                void action.run(
                  ["test", base.id, base.source_revision],
                  async (key) => {
                    const r = await api("testCamera", {
                      path: { camera_id: base.id },
                      intent: key,
                    });
                    setJob(r.data.id);
                  },
                )
              }
            >
              测试已保存的视频来源
            </Button>
            {dirty && (
              <p className="caption muted">请先保存草稿变更，再测试。</p>
            )}
          </Panel>
        )}
        <Panel
          title="3 · 周计划"
          action={
            <Button
              type="button"
              onClick={() =>
                setSchedule((v) => [
                  ...v,
                  {
                    weekdays: [1, 2, 3, 4, 5, 6, 7],
                    start: "08:00",
                    end: "22:00",
                    end_day_offset: 0,
                  },
                ])
              }
            >
              增加时段
            </Button>
          }
        >
          <div className="stack">
            <p className="caption muted">
              所有时段按北京时间。空计划不会自动采集，保留临时开始能力。重叠范围在保存时按并集合并，不重复计时。
            </p>
            {schedule.map((r, i) => (
              <div key={i} className="schedule-rule">
                <div className="weekday-picker">
                  {["一", "二", "三", "四", "五", "六", "日"].map((day, n) => (
                    <label key={day}>
                      <input
                        type="checkbox"
                        checked={r.weekdays.includes(n + 1)}
                        onChange={(e) =>
                          setSchedule((v) =>
                            v.map((s, j) =>
                              i === j
                                ? {
                                    ...s,
                                    weekdays: e.target.checked
                                      ? [...s.weekdays, n + 1].sort()
                                      : s.weekdays.filter((d) => d !== n + 1),
                                  }
                                : s,
                            ),
                          )
                        }
                      />
                      {day}
                    </label>
                  ))}
                </div>
                <div className="row-wrap">
                  <Field label="开始">
                    <input
                      type="time"
                      required
                      value={r.start}
                      onChange={(e) =>
                        setSchedule((v) =>
                          v.map((s, j) =>
                            j === i ? { ...s, start: e.target.value } : s,
                          ),
                        )
                      }
                    />
                  </Field>
                  <Field label="结束">
                    <input
                      type="time"
                      required
                      value={r.end}
                      onChange={(e) =>
                        setSchedule((v) =>
                          v.map((s, j) =>
                            j === i ? { ...s, end: e.target.value } : s,
                          ),
                        )
                      }
                    />
                  </Field>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={r.end_day_offset === 1}
                      onChange={(e) =>
                        setSchedule((v) =>
                          v.map((s, j) =>
                            j === i
                              ? {
                                  ...s,
                                  end_day_offset: e.target.checked ? 1 : 0,
                                }
                              : s,
                          ),
                        )
                      }
                    />
                    结束于次日
                  </label>
                  <Button
                    type="button"
                    onClick={() =>
                      setSchedule((v) =>
                        v.map((s, j) =>
                          j === i
                            ? {
                                ...s,
                                start: "00:00",
                                end: "00:00",
                                end_day_offset: 1,
                              }
                            : s,
                        ),
                      )
                    }
                  >
                    全天
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() =>
                      setSchedule((v) => v.filter((_, j) => i !== j))
                    }
                  >
                    移除此时段
                  </Button>
                </div>
                <p className="caption muted">
                  周
                  {r.weekdays
                    .map(
                      (d) => ["一", "二", "三", "四", "五", "六", "日"][d - 1],
                    )
                    .join("、")}{" "}
                  {r.start}—{r.end_day_offset ? "次日 " : ""}
                  {r.end}
                </p>
              </div>
            ))}
            {invalidSchedule && (
              <Notice tone="danger">
                请选择星期；同日结束须晚于开始，跨午夜请选择“结束于次日”。
              </Notice>
            )}
          </div>
        </Panel>
        {!invalidSchedule && schedule.length > 0 && (
          <Panel title="合并后的周计划预览">
            <div className="stack">
              {mergedWeek(schedule).map((r, i) => (
                <p className="caption" key={i}>
                  {scheduleLabel(r)}
                </p>
              ))}
            </div>
          </Panel>
        )}
        <Panel title="4 · 检测区域与图片保存">
          <div className="stack">
            {camera?.regions_state === "needs_review" && (
              <Notice tone="warning">
                视频源尺寸或方向变化，需要复查检测区域。保存新区域后才视为已复查。
              </Notice>
            )}
            <Field
              label="现场图保留分钟"
              hint={`留空沿用全局设置（${settings.data?.data.config.frame_retention_minutes ?? "正在读取"} 分钟）。最小值由采样、超时和重试共同校验。`}
            >
              <input
                type="number"
                min="1"
                max="10080"
                value={retention}
                onChange={(e) => setRetention(e.target.value)}
              />
            </Field>
            <Button type="button" onClick={() => setRoiOpen((v) => !v)}>
              {roiOpen ? "收起区域编辑器" : "编辑包含区 / 排除区"}
            </Button>
            {roiOpen && (
              <Suspense fallback={<Skeleton />}>
                <RoiEditor
                  value={regions}
                  onChange={setRegions}
                  imageUrl={
                    evidence.data?.data.state === "available"
                      ? evidence.data.data.content_url
                      : camera?.runtime.capture_state === "online"
                        ? `/api/cameras/${camera.id}/snapshot`
                        : null
                  }
                  expiresAt={evidence.data?.data.expires_at}
                />
              </Suspense>
            )}
            {base && (
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                />
                启用机位（保存前检查容量、保留期与参考图）
              </label>
            )}
            {!base && (
              <Notice>
                首次保存始终为停用草稿。取得机位 ID 后继续测试和启用流程。
              </Notice>
            )}
          </div>
        </Panel>
        <ErrorNotice error={action.error} />
        {message && <Notice>{message}</Notice>}
        {action.error instanceof ApiError &&
          action.error.status === 412 &&
          base && (
            <Button
              type="button"
              onClick={() =>
                void api("getCamera", { path: { camera_id: base.id } }).then(
                  (r) => setLatest(r.data),
                )
              }
            >
              读取新版信息，保留草稿
            </Button>
          )}
        {latest && (
          <Panel title={`核对服务器新版 v${latest.revision}`}>
            <div className="conflict-diff">
              <div>
                <strong>草稿基线 v{base!.revision}</strong>
                <p>
                  {base!.name} · {base!.enabled ? "启用" : "停用"}
                </p>
                <p>
                  端口 {String(base!.source.port)} · {base!.schedule.length}{" "}
                  条计划 · 保存 {base!.effective_retention_minutes} 分钟
                </p>
              </div>
              <div>
                <strong>服务器 v{latest.revision}</strong>
                <p>
                  {latest.name} · {latest.enabled ? "启用" : "停用"}
                </p>
                <p>
                  端口 {String(latest.source.port)} · {latest.schedule.length}{" "}
                  条计划 · 保存 {latest.effective_retention_minutes} 分钟
                </p>
              </div>
            </div>
            <Button
              type="button"
              onClick={() => {
                const p = patch();
                setName(p.name ?? latest.name);
                setPort(Number(p.source?.port ?? latest.source.port));
                setPath(p.source?.path ?? String(latest.source.path));
                setUsername(
                  p.source?.username ?? String(latest.source.username ?? ""),
                );
                setSchedule(p.schedule ?? latest.schedule);
                setRegions(p.regions ?? latest.regions);
                setRetention(
                  "retention_minutes" in p
                    ? (p.retention_minutes?.toString() ?? "")
                    : (latest.retention_minutes?.toString() ?? ""),
                );
                setEnabled(p.enabled ?? latest.enabled);
                setBase(latest);
                setLatest(null);
                setImpact(null);
                action.setError(null);
              }}
            >
              已核对差异，保留草稿变更采用新版基线
            </Button>
          </Panel>
        )}
        <div className="save-bar">
          <p>
            {dirty
              ? "存在未保存变更"
              : base
                ? "配置已保存，运行状态独立显示"
                : "将保存为停用草稿"}
          </p>
          <Button
            type="submit"
            variant="primary"
            busy={action.pending}
            disabled={invalidSchedule || invalidRoi || Boolean(base && !dirty)}
          >
            {base ? "保存机位配置" : "保存停用草稿"}
          </Button>
        </div>
      </form>
      {base && (
        <div className="danger-zone">
          <Button variant="danger" onClick={() => setDeleting(true)}>
            删除机位
          </Button>
        </div>
      )}
      {impact && pendingPatch && (
        <Modal title="确认机位配置影响" onClose={() => setImpact(null)}>
          <div className="stack">
            <ImpactDetails impact={impact} />
            {message && <Notice tone="warning">{message}</Notice>}
            <ErrorNotice error={action.error} />
            <div className="actions end">
              <Button onClick={() => setImpact(null)}>取消</Button>
              <Button
                variant="primary"
                disabled={impact.blocking_errors.length > 0}
                busy={action.pending}
                onClick={() =>
                  void action.run(
                    [base!.revision, pendingPatch, impact.preview_token],
                    async (key) => {
                      if (Date.parse(impact.expires_at) <= serverNow()) {
                        const r = await api("previewCameraUpdate", {
                          path: { camera_id: base!.id },
                          body: {
                            base_revision: base!.revision,
                            patch: pendingPatch,
                          },
                        });
                        setImpact(r.data);
                        setMessage("预检已过期，已重新检查，请再次核对确认。");
                        return;
                      }
                      try {
                        await save(key, pendingPatch, impact.preview_token);
                      } catch (error) {
                        if (error instanceof ApiError && error.status === 409) {
                          setImpact(null);
                          setMessage(
                            "配置或参考资料已变化，请再次保存以重新预检。",
                          );
                        }
                        throw error;
                      }
                    },
                  )
                }
              >
                {impact.will_expire_frame_count
                  ? "保存并使这些图片到期"
                  : "确认保存"}
              </Button>
            </div>
          </div>
        </Modal>
      )}
      {job && <JobModal id={job} onClose={() => setJob(null)} />}{" "}
      {deleting && (
        <Confirm
          title={`删除「${base!.name}」？`}
          description={
            <p>
              停止并移除配置入口。历史文字记录保留，现存图片按原规则到期；不会清空历史。
            </p>
          }
          onClose={() => setDeleting(false)}
          busy={action.pending}
          error={action.error}
          danger
          label="删除机位并保留历史"
          onConfirm={() =>
            void action.run(
              ["delete", base!.id, base!.revision],
              async (key) => {
                const r = await api("deleteCamera", {
                  path: { camera_id: base!.id },
                  revision: base!.revision,
                  intent: key,
                });
                setSaved(true);
                setDeleting(false);
                setJob(r.data.id);
                await invalidate("listCameras", "getStatus");
              },
            )
          }
        />
      )}{" "}
      {blocker.state === "blocked" && (
        <Confirm
          title="离开未保存的配置？"
          description={<p>当前草稿将丢失。已保存的停用草稿仍在服务器。</p>}
          onClose={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
          label="放弃草稿并离开"
        />
      )}
    </>
  );
}
