import { useState } from "react";
import { Pause, Play, RotateCcw } from "lucide-react";
import { api } from "../../api/client";
import type { CameraDto } from "../../api/generated/types";
import { invalidate } from "../../api/query";
import {
  Button,
  ErrorNotice,
  Field,
  Modal,
  Notice,
} from "../../shared/components";
import { useAction } from "../../shared/hooks";
import { defaultOverrideEnd } from "../../shared/schedule";
import { beijingInput, serverNow, toInput } from "../../shared/time";
export function CameraControl({ camera }: { camera: CameraDto }) {
  const [mode, setMode] = useState<"start" | "stop" | "resume" | null>(null),
    [expires, setExpires] = useState(
      toInput(
        new Date(
          defaultOverrideEnd(camera.schedule, serverNow()),
        ).toISOString(),
      ),
    );
  const action = useAction<unknown>();
  const waiting =
    (camera.runtime.control_revision ?? 0) >
    (camera.runtime.applied_revision ?? 0);
  return (
    <>
      <div className="actions">
        {camera.runtime.override_mode && (
          <Button disabled={waiting} onClick={() => setMode("resume")}>
            <RotateCcw size={14} />
            恢复计划
          </Button>
        )}
        <Button
          disabled={!camera.enabled || waiting}
          onClick={() => {
            setMode(
              camera.runtime.capture_state === "online" ? "stop" : "start",
            );
            setExpires(
              toInput(
                new Date(
                  defaultOverrideEnd(camera.schedule, serverNow()),
                ).toISOString(),
              ),
            );
          }}
        >
          {camera.runtime.capture_state === "online" ? (
            <Pause size={14} />
          ) : (
            <Play size={14} />
          )}
          <span>
            {waiting
              ? "等待控制命令应用"
              : camera.runtime.capture_state === "online"
                ? "临时停止"
                : "临时开始"}
          </span>
        </Button>
      </div>
      {mode && (
        <Modal
          title={
            mode === "resume"
              ? "恢复周计划"
              : mode === "stop"
                ? "临时停止采集"
                : "临时开始采集"
          }
          onClose={() => {
            if (!action.pending) setMode(null);
          }}
        >
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              void action.run(
                [mode, camera.id, camera.revision, expires],
                async (key) => {
                  const args = {
                    path: { camera_id: camera.id },
                    revision: camera.revision,
                    intent: key,
                  };
                  if (mode === "resume")
                    await api("resumeCameraSchedule", args);
                  else
                    await api(mode === "start" ? "startCamera" : "stopCamera", {
                      ...args,
                      body: { expires_at: beijingInput(expires) },
                    });
                  await invalidate(
                    "getCamera",
                    "listCameras",
                    "getStatus",
                    "getPreviewStatus",
                    "getOverview",
                  );
                  setMode(null);
                },
              );
            }}
          >
            {mode !== "resume" ? (
              <>
                <Field
                  label="覆盖结束时间（北京时间）"
                  hint="最长 24 小时；结束后恢复周计划。"
                >
                  <input
                    type="datetime-local"
                    step="1"
                    required
                    value={expires}
                    min={toInput(new Date(serverNow()).toISOString())}
                    max={toInput(
                      new Date(serverNow() + 86400000).toISOString(),
                    )}
                    onChange={(e) => setExpires(e.target.value)}
                  />
                </Field>
                <Notice>
                  {mode === "stop" ? "暂停新图采集" : "临时覆盖计划开始采集"}
                  ，至 {expires.replace("T", " ")}
                  （北京时间）。此操作不会长期停用机位。
                </Notice>
              </>
            ) : (
              <Notice>
                立即清除临时覆盖，按当前北京时间的周计划决定是否采集。
              </Notice>
            )}
            <p className="caption muted">
              提交后等待实际应用确认；收到请求受理不等于摄像头已开始或停止。
            </p>
            <ErrorNotice error={action.error} />
            <div className="actions end">
              <Button type="button" onClick={() => setMode(null)}>
                取消
              </Button>
              <Button type="submit" variant="primary" busy={action.pending}>
                确认
                {mode === "resume"
                  ? "恢复计划"
                  : mode === "stop"
                    ? "临时停止"
                    : "临时开始"}
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}
