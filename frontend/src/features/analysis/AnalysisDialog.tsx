import { useState } from "react";
import { api, ApiError } from "../../api/client";
import { useApi } from "../../api/query";
import type {
  AnalysisPreviewDto,
  AnalysisRequest,
} from "../../api/generated/types";
import { useAction } from "../../shared/hooks";
import {
  Button,
  ErrorNotice,
  Field,
  JobProgress,
  Modal,
  Notice,
  useScope,
} from "../../shared/components";
import {
  beijingInput,
  clock,
  dateTime,
  serverNow,
  toInput,
} from "../../shared/time";
export function AnalysisDialog({
  mode,
  cameraIds,
  from,
  to,
  onClose,
}: {
  mode: "visual" | "records_only";
  cameraIds: string[];
  from: string;
  to: string;
  onClose: () => void;
}) {
  const [start, setStart] = useState(toInput(from)),
    [end, setEnd] = useState(
      toInput(new Date(Math.min(Date.parse(to), serverNow())).toISOString()),
    ),
    [partial, setPartial] = useState(false),
    [preview, setPreview] = useState<AnalysisPreviewDto | null>(null),
    [job, setJob] = useState<string | null>(null),
    [message, setMessage] = useState("");
  const action = useAction<unknown>();
  const cameras = useApi("listCameras", { query: { limit: 100 } }),
    scope = useScope();
  const resolvedIds = cameraIds.length
    ? cameraIds
    : (cameras.data?.data.map((c) => c.id) ?? []);
  const request: AnalysisRequest = {
    mode,
    camera_ids: resolvedIds,
    from: beijingInput(start),
    to: beijingInput(end),
    allow_partial: partial,
    reference_policy: "current",
    subject_id: mode === "records_only" ? scope.query.subject_id : undefined,
  };
  const invalid =
    !resolvedIds.length ||
    Date.parse(request.to) > serverNow() + 1000 ||
    Date.parse(request.to) <= Date.parse(request.from) ||
    Date.parse(request.to) - Date.parse(request.from) >
      (mode === "visual" ? 3600000 : 86400000) ||
    (mode === "visual" && resolvedIds.length !== 1);
  const preflight = async () => {
    const r = await api("previewAnalysis", { body: request });
    setPreview(r.data);
    return r.data;
  };
  return (
    <Modal
      title={
        mode === "visual" ? "用仍有效的原图重新分析" : "按现有记录重新汇总"
      }
      onClose={onClose}
    >
      {job ? (
        <JobProgress id={job} onClose={onClose} />
      ) : (
        <div className="stack">
          <Notice>
            {mode === "visual"
              ? "重新识别可观察活动，使用当前适用参考图。单次仅允许一路机位、最多 60 分钟。"
              : "根据保存的记录与统计生成汇总文字。不会重新识别已过期画面；原图过期仍可执行。"}
          </Notice>
          <div className="form-grid">
            <Field label="开始（北京时间）">
              <input
                type="datetime-local"
                step="1"
                value={start}
                onChange={(e) => {
                  if (e.target.value) {
                    setStart(e.target.value);
                    setPreview(null);
                  }
                }}
              />
            </Field>
            <Field label="结束（北京时间）">
              <input
                type="datetime-local"
                step="1"
                value={end}
                onChange={(e) => {
                  if (e.target.value) {
                    setEnd(e.target.value);
                    setPreview(null);
                  }
                }}
              />
            </Field>
          </div>
          {invalid && (
            <Notice tone="warning">
              请选择有序且有效的范围；原图模式限单路 60 分钟，文字模式最多一天。
            </Notice>
          )}
          {mode === "visual" && (
            <label className="check-row">
              <input
                type="checkbox"
                checked={partial}
                onChange={(e) => {
                  setPartial(e.target.checked);
                  setPreview(null);
                }}
              />
              只分析仍有原图的部分（明确允许部分范围）
            </label>
          )}
          {preview && (
            <>
              <dl className="definition-list">
                <div>
                  <dt>可用现场图</dt>
                  <dd>{preview.available_frame_count} 张</dd>
                </div>
                <div>
                  <dt>预计批次</dt>
                  <dd>{preview.estimated_batches}</dd>
                </div>
                <div>
                  <dt>最早到期</dt>
                  <dd>{dateTime(preview.earliest_expires_at)}</dd>
                </div>
                <div>
                  <dt>预检有效至</dt>
                  <dd>{clock(preview.expires_at, true)} 北京时间</dd>
                </div>
              </dl>
              {preview.missing_ranges.length > 0 && (
                <Notice tone="warning">
                  <p>以下范围缺少可用原图：</p>
                  {preview.missing_ranges.map((r, i) => (
                    <p key={i}>
                      {clock(r.from, true)}—{clock(r.to, true)}
                    </p>
                  ))}
                </Notice>
              )}
              {preview.warnings.map((w) => (
                <Notice tone="warning" key={w.code}>
                  {w.message}
                </Notice>
              ))}
            </>
          )}
          {message && <Notice tone="warning">{message}</Notice>}
          <ErrorNotice error={action.error} />
          <div className="actions end">
            <Button onClick={onClose}>取消</Button>
            {!preview ? (
              <Button
                variant="primary"
                disabled={invalid}
                busy={action.pending}
                onClick={() => void action.run(request, preflight)}
              >
                检查范围与资料
              </Button>
            ) : (
              <Button
                variant="primary"
                disabled={
                  invalid ||
                  (!partial &&
                    mode === "visual" &&
                    preview.missing_ranges.length > 0)
                }
                busy={action.pending}
                onClick={() =>
                  void action.run(
                    [request, preview.preview_token],
                    async (key) => {
                      if (Date.parse(preview.expires_at) <= serverNow()) {
                        await preflight();
                        setMessage(
                          "预检已过期，已重新检查。请核对新的范围后再次确认。",
                        );
                        return;
                      }
                      try {
                        const r = await api("createAnalysisJob", {
                          body: {
                            ...request,
                            preview_token: preview.preview_token,
                          },
                          intent: key,
                        });
                        setJob(r.data.id);
                      } catch (e) {
                        if (e instanceof ApiError && e.status === 409) {
                          await preflight();
                          setMessage(
                            "图片或参考资料已变化，请核对新预检后再次确认。",
                          );
                        }
                        throw e;
                      }
                    },
                  )
                }
              >
                确认创建任务
              </Button>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
