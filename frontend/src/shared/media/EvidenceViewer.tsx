import { useEffect, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  ImageOff,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { api, ApiError } from "../../api/client";
import { useApi } from "../../api/query";
import { useAction } from "../hooks";
import {
  Button,
  ErrorNotice,
  Field,
  Modal,
  Notice,
  QueryState,
} from "../components";
import { dateTime } from "../time";
import { evidenceLabels } from "../labels";
import { useEphemeralImage } from "./EphemeralImage";
import type { Crop } from "../../api/generated/types";

export function EvidenceViewer({
  ids,
  index,
  onIndex,
  onClose,
}: {
  ids: string[];
  index: number;
  onIndex: (i: number) => void;
  onClose: () => void;
}) {
  const [zoom, setZoom] = useState(1),
    [promote, setPromote] = useState(false),
    [entity, setEntity] = useState(""),
    [name, setName] = useState(""),
    [keep, setKeep] = useState(false),
    [crop, setCrop] = useState<Crop>({ x: 0, y: 0, width: 1, height: 1 });
  const cropValid =
    crop.x >= 0 &&
    crop.y >= 0 &&
    crop.width > 0 &&
    crop.height > 0 &&
    crop.x + crop.width <= 1 &&
    crop.y + crop.height <= 1;
  const id = ids[index];
  const query = useApi(
      "getEvidenceMetadata",
      { path: { evidence_id: id } },
      { gcTime: 0 },
    ),
    entities = useApi("listEntities", { query: { limit: 100 } });
  const evidence = query.data?.data;
  const image = useEphemeralImage(
    evidence?.state === "available" ? evidence.content_url : null,
    { expiresAt: evidence?.expires_at },
  );
  const drag = useRef<{
      x: number;
      y: number;
      left: number;
      top: number;
    } | null>(null),
    scroller = useRef<HTMLDivElement>(null);
  const action = useAction<unknown>();
  const change = (next: number) => {
    if (next >= 0 && next < ids.length) {
      onIndex(next);
      setZoom(1);
      setPromote(false);
      setCrop({ x: 0, y: 0, width: 1, height: 1 });
    }
  };
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).matches("input,select,textarea")) return;
      if (e.key === "ArrowRight") {
        e.preventDefault();
        change(index + 1);
      }
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        change(index - 1);
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [index, ids.length]);
  return (
    <Modal title="现场原图" onClose={onClose} wide>
      <div className="stack">
        <QueryState query={query}>
          {({ data: e }) => (
            <>
              <div className="row-between">
                <span className="caption">
                  {index + 1} / {ids.length} · {dateTime(e.captured_at)}
                </span>
                <span className="caption">
                  {e.width ?? "—"} × {e.height ?? "—"} JPEG
                </span>
              </div>
              <div
                className="evidence-image-stage"
                ref={scroller}
                onPointerDown={(event) => {
                  if (zoom > 1) {
                    event.currentTarget.setPointerCapture(event.pointerId);
                    drag.current = {
                      x: event.clientX,
                      y: event.clientY,
                      left: event.currentTarget.scrollLeft,
                      top: event.currentTarget.scrollTop,
                    };
                  }
                }}
                onPointerMove={(event) => {
                  if (drag.current) {
                    event.currentTarget.scrollLeft =
                      drag.current.left + drag.current.x - event.clientX;
                    event.currentTarget.scrollTop =
                      drag.current.top + drag.current.y - event.clientY;
                  }
                }}
                onPointerUp={() => {
                  drag.current = null;
                }}
                onPointerCancel={() => {
                  drag.current = null;
                }}
              >
                {image.url ? (
                  <img
                    src={image.url}
                    alt={`采集于${dateTime(e.captured_at)}的现场证据`}
                    style={{ width: `${zoom * 100}%`, maxWidth: "none" }}
                    draggable={false}
                  />
                ) : (
                  <div className="preview-empty">
                    <ImageOff size={32} />
                    <p>
                      {e.state !== "available"
                        ? evidenceLabels[e.state]
                        : image.error
                          ? image.error instanceof ApiError &&
                            image.error.status === 410
                            ? "原图已过期"
                            : "原图已不可用"
                          : "正在读取图片"}
                    </p>
                    <small>文字记录仍可查看</small>
                  </div>
                )}
              </div>
              <ErrorNotice error={image.error} />
              <div className="evidence-controls">
                <Button
                  disabled={index === 0}
                  onClick={() => change(index - 1)}
                  aria-label="上一张原图"
                >
                  <ChevronLeft size={18} />
                  <span>上一张</span>
                </Button>
                <div className="actions">
                  <Button
                    onClick={() => setZoom((z) => Math.max(1, z - 0.5))}
                    disabled={!image.url || zoom === 1}
                    aria-label="缩小"
                  >
                    <ZoomOut size={18} />
                  </Button>
                  <span className="caption">{Math.round(zoom * 100)}%</span>
                  <Button
                    onClick={() => setZoom((z) => Math.min(4, z + 0.5))}
                    disabled={!image.url || zoom >= 4}
                    aria-label="放大"
                  >
                    <ZoomIn size={18} />
                  </Button>
                </div>
                <Button
                  disabled={index === ids.length - 1}
                  onClick={() => change(index + 1)}
                  aria-label="下一张原图"
                >
                  <span>下一张</span>
                  <ChevronRight size={18} />
                </Button>
              </div>
              <dl className="definition-list">
                <div>
                  <dt>证据 ID</dt>
                  <dd className="mono">{e.id}</dd>
                </div>
                <div>
                  <dt>原图到期</dt>
                  <dd>{dateTime(e.expires_at)}</dd>
                </div>
              </dl>
              <p className="caption muted">
                原图是实际保存的完整 JPEG，默认最长边 1280；并非摄像头无损码流。
              </p>
              {!promote ? (
                <Button disabled={!image.url} onClick={() => setPromote(true)}>
                  保存为长期参考图
                </Button>
              ) : (
                <form
                  className="stack inset"
                  onSubmit={(event) => {
                    event.preventDefault();
                    const target = entities.data?.data.find(
                      (e) => e.id === entity,
                    );
                    if (!target || !cropValid) return;
                    void action.run(
                      [id, entity, name, keep, crop, target.revision],
                      async (key) => {
                        await api("promoteReference", {
                          path: { entity_id: entity },
                          revision: target.revision,
                          intent: key,
                          body: {
                            evidence_id: id,
                            keep_as_reference: true,
                            name,
                            crop,
                            enabled: false,
                          },
                        });
                        setPromote(false);
                      },
                    );
                  }}
                >
                  <Field label="保存到人物">
                    <select
                      required
                      value={entity}
                      onChange={(e) => setEntity(e.target.value)}
                    >
                      <option value="">请选择档案</option>
                      {entities.data?.data.map((e) => (
                        <option key={e.id} value={e.id}>
                          {e.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="参考图名称">
                    <input
                      required
                      maxLength={64}
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                    />
                  </Field>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      required
                      checked={keep}
                      onChange={(e) => setKeep(e.target.checked)}
                    />
                    我确认单独长期保存此图，不受现场图保存期限影响
                  </label>
                  <div className="form-grid">
                    {(["x", "y", "width", "height"] as const).map((key) => (
                      <Field
                        label={
                          {
                            x: "参考裁剪左边界",
                            y: "参考裁剪上边界",
                            width: "参考裁剪宽度",
                            height: "参考裁剪高度",
                          }[key]
                        }
                        key={key}
                      >
                        <input
                          type="number"
                          min={key === "x" || key === "y" ? 0 : 0.001}
                          max="1"
                          step="0.001"
                          required
                          value={crop[key]}
                          onChange={(event) =>
                            setCrop({
                              ...crop,
                              [key]: Number(event.target.value),
                            })
                          }
                        />
                      </Field>
                    ))}
                  </div>
                  {!cropValid && (
                    <Notice tone="danger">
                      裁剪范围须位于图片内，且宽高大于 0。
                    </Notice>
                  )}
                  <Notice>
                    按当前裁剪范围另存为未启用参考图；检查后可在档案页启用。不会延长原现场图到期时间。
                  </Notice>
                  <ErrorNotice error={action.error} />
                  <Button
                    type="submit"
                    variant="primary"
                    busy={action.pending}
                    disabled={!image.url || !keep || !cropValid}
                  >
                    确认长期保存
                  </Button>
                </form>
              )}
            </>
          )}
        </QueryState>
      </div>
    </Modal>
  );
}
