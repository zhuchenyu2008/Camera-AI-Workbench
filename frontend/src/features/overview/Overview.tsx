import { ArrowRight, Camera, Clock3, Plus, ScanLine } from "lucide-react";
import { useAllApi, useApi } from "../../api/query";
import {
  ActivityChart,
  Badge,
  CoverageTrack,
  EmptyState,
  EventList,
  Notice,
  PageHeading,
  Panel,
  QueryState,
  ScopeLink,
  ScopePicker,
  useScope,
} from "../../shared/components";
import {
  compactDuration,
  dateLabel,
  duration,
  localDate,
} from "../../shared/time";
import { captureLabel } from "../../shared/labels";
import { CameraPreview } from "../../shared/media/CameraPreview";
import { CameraControl } from "../cameras/CameraControl";
import { TimelineStrip } from "../timeline/TimelineStrip";

export function Overview() {
  const scope = useScope();
  const cameras = useAllApi("listCameras", undefined, { poll: 5000 }),
    settings = useApi("getSettings");
  const defaultCamera =
    cameras.data?.find((c) => c.enabled && !c.deleted_at) ?? cameras.data?.[0];
  const query = {
    ...scope.query,
    camera_ids:
      scope.query.camera_ids ??
      ((!scope.subject || scope.subject === "all") && defaultCamera
        ? [defaultCamera.id]
        : undefined),
  };
  const q = useApi(
    "getOverview",
    { query },
    {
      enabled: cameras.isSuccess,
      poll: scope.date === localDate() ? 30000 : undefined,
    },
  );
  const timeline = useApi(
    "getTimeline",
    { query: { ...query, bucket_seconds: 300 } },
    {
      enabled: cameras.isSuccess,
      poll: scope.date === localDate() ? 30000 : undefined,
    },
  );

  const cameraList = cameras.data ?? [];
  const camera =
    cameraList.find((c) => c.id === scope.camera_ids[0]) ?? defaultCamera;

  return (
    <>
      <PageHeading
        kicker="YOUR DAY / OVERVIEW"
        title={scope.date === localDate() ? "今日概览" : "活动概览"}
        description={`${dateLabel(scope.date)} · 活动来自画面观察，时长为估计值。`}
        action={
          <ScopeLink to="/reports" className="button secondary">
            查看报告
            <ArrowRight size={16} />
          </ScopeLink>
        }
      />
      <ScopePicker />
      {cameras.isSuccess && cameraList.length === 0 && (
        <Panel title="机位与画面" kicker="CAMERA / LIVE VIEW">
          <EmptyState
            title="尚未配置摄像头"
            description="添加机位后，这里会显示画面与活动记录。"
            action={
              <ScopeLink className="button primary" to="/cameras/new">
                <Plus size={17} />
                添加摄像头
              </ScopeLink>
            }
          />
        </Panel>
      )}
      {cameraList.length > 0 && (
        <div className="overview-top">
          <Panel
            className="preview-panel"
            kicker="CAMERA / LIVE VIEW"
            title="当前画面"
            action={
              cameraList.length >= 2 ? (
                <label className="sr-labeled-select">
                  <span className="sr-only">预览机位</span>
                  <select
                    value={camera?.id ?? ""}
                    onChange={(e) => {
                      scope.update("camera_ids", [e.target.value]);
                    }}
                  >
                    {cameraList.map((c) => (
                      <option value={c.id} key={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </label>
              ) : cameraList.length === 1 ? (
                <span className="caption muted">{cameraList[0].name}</span>
              ) : null
            }
          >
            {cameras.isLoading ? (
              <p className="caption muted">正在读取机位...</p>
            ) : camera ? (
              <>
                <CameraPreview camera={camera} />
                <div className="capture-footer">
                  <Badge
                    tone={
                      camera.runtime.capture_state === "online"
                        ? "success"
                        : "neutral"
                    }
                    dot={camera.runtime.capture_state === "online"}
                  >
                    {captureLabel(camera.runtime)}
                  </Badge>
                  <CameraControl camera={camera} />
                </div>
              </>
            ) : null}
          </Panel>
          <Panel
            title="活动时长"
            kicker="TODAY / ACTIVITY"
            action={<Badge>抽样估计</Badge>}
          >
            <QueryState query={q}>
              {({ data }) => (
                <>
                  <div className="activity-headline">
                    <span className="metric-value">
                      {compactDuration(data.coverage.classified_ms ?? 0)}
                    </span>
                    <p>
                      已归类有效活动 ·{" "}
                      {duration(data.coverage.classified_ms ?? 0)}
                    </p>
                  </div>
                  <ActivityChart items={data.activities} />
                  <div className="uncertainty-grid">
                    <div>
                      <span>无法判断</span>
                      <strong>
                        {duration(data.coverage.unknown_ms ?? 0, false)}
                      </strong>
                    </div>
                    <div>
                      <span>等待分析</span>
                      <strong>
                        {duration(data.coverage.unanalysed_ms ?? 0, false)}
                      </strong>
                    </div>
                    <div>
                      <span>采集缺口</span>
                      <strong>
                        {duration(data.coverage.gap_ms ?? 0, false)}
                      </strong>
                    </div>
                  </div>
                </>
              )}
            </QueryState>
          </Panel>
        </div>
      )}
      {cameraList.length > 0 &&
      !settings.data?.data.config.vision_provider_id ? (
        <Notice tone="warning">
          摄像头可独立采集，AI 服务尚未配置；有画面的范围将等待分析。
          <ScopeLink to="/settings/ai" className="text-link">
            配置 AI 服务
          </ScopeLink>
        </Notice>
      ) : null}
      <Panel
        title="时间覆盖"
        kicker="COVERAGE / BEIJING TIME"
        action={
          <ScopeLink className="text-link" to="/timeline">
            打开时间线
            <ArrowRight size={15} />
          </ScopeLink>
        }
      >
        <QueryState query={q}>
          {({ data }) => <CoverageTrack coverage={data.coverage} />}
        </QueryState>
        <QueryState query={timeline}>
          {({ data }) => (
            <TimelineStrip
              timeline={data}
              displayFrom={scope.query.from}
              displayTo={scope.query.to}
            />
          )}
        </QueryState>
      </Panel>
      <div className="overview-bottom">
        <Panel
          title="最近事件"
          kicker="RECENT / EVENTS"
          action={
            <ScopeLink className="text-link" to="/timeline">
              查看全部
              <ArrowRight size={15} />
            </ScopeLink>
          }
        >
          <QueryState query={q}>
            {({ data }) =>
              data.latest_events.length ? (
                <EventList events={data.latest_events} limit={10} />
              ) : (
                <EmptyState
                  title="此范围尚无活动事件"
                  description="已采集的画面可能仍在等待分析；请查看上方时间覆盖。"
                  action={
                    <ScopeLink to="/minutes" className="text-link">
                      查看分钟记录
                    </ScopeLink>
                  }
                />
              )
            }
          </QueryState>
        </Panel>
        <Panel title="处理状态" kicker="PIPELINE / STATUS">
          <div className="pipeline">
            <div>
              <span>
                <Camera size={18} />
              </span>
              <div>
                <strong>采集画面</strong>
                <p>
                  {cameraList.length
                    ? `${cameraList.filter((c) => c.runtime.capture_state === "online").length} / ${cameraList.length} 台正在采集`
                    : "尚未配置机位"}
                </p>
              </div>
            </div>
            <div>
              <span>
                <ScanLine size={18} />
              </span>
              <div>
                <strong>活动分析</strong>
                <p>
                  {settings.data?.data.config.vision_provider_id
                    ? "已配置视觉服务"
                    : "等待配置视觉服务"}
                </p>
              </div>
            </div>
            <div>
              <span>
                <Clock3 size={18} />
              </span>
              <div>
                <strong>现场图与记录保存</strong>
                <p>
                  现场图保留{" "}
                  {settings.data?.data.config.frame_retention_minutes ?? "—"}{" "}
                  分钟；文字记录永久保留
                </p>
              </div>
            </div>
          </div>
          <ScopeLink className="text-link" to="/status">
            查看设备与任务
            <ArrowRight size={15} />
          </ScopeLink>
        </Panel>
      </div>
      {q.data?.data.warnings.map((w) => (
        <Notice tone="warning" key={w.code}>
          {w.message}
        </Notice>
      ))}
    </>
  );
}
