import { useEffect, useRef, useState } from "react";
import type { Regions, Polygon } from "../../api/generated/types";
import { Button, Field, Notice } from "../../shared/components";
import {
  containedRect,
  normalizePoint,
  polygonError,
  type Point,
} from "../../shared/roi";
import { useEphemeralImage } from "../../shared/media/EphemeralImage";
export function RoiEditor({
  value,
  onChange,
  imageUrl,
  expiresAt,
}: {
  value: Regions;
  onChange: (regions: Regions) => void;
  imageUrl: string | null;
  expiresAt?: string;
}) {
  const [kind, setKind] = useState<"include" | "exclude">("include"),
    [shape, setShape] = useState<"rectangle" | "polygon">("rectangle"),
    [points, setPoints] = useState<Point[]>([]),
    [selected, setSelected] = useState<string | null>(null),
    [size, setSize] = useState({ width: 0, height: 0 }),
    [natural, setNatural] = useState({ width: 16, height: 9 }),
    [error, setError] = useState(""),
    [zoom, setZoom] = useState(1);
  const container = useRef<HTMLDivElement>(null),
    drag = useRef<{ id: string; point: number } | null>(null),
    rectangleStart = useRef<Point | null>(null);
  const image = useEphemeralImage(imageUrl, { expiresAt });
  useEffect(() => {
    const observer = new ResizeObserver((entries) => {
      setSize({
        width: entries[0].contentRect.width,
        height: entries[0].contentRect.height,
      });
    });
    observer.observe(container.current!);
    return () => observer.disconnect();
  }, []);
  const rect = containedRect(size, natural),
    polys = [
      ...(value.include ?? []).map((p) => ({ ...p, kind: "include" as const })),
      ...(value.exclude ?? []).map((p) => ({ ...p, kind: "exclude" as const })),
    ];
  const apply = (pts: Point[]) => {
    const issue = polygonError(pts);
    if (issue) {
      setError(issue);
      return;
    }
    if ((value[kind]?.length ?? 0) >= 8) {
      setError("包含区与排除区各最多 8 个");
      return;
    }
    onChange({
      ...value,
      source_aspect_ratio: `${natural.width}:${natural.height}`,
      [kind]: [
        ...(value[kind] ?? []),
        { id: `roi_${crypto.randomUUID().slice(0, 8)}`, points: pts },
      ],
    });
    setPoints([]);
    setError("");
  };
  const coords = (event: React.PointerEvent): Point | null => {
    const bounds = container.current!.getBoundingClientRect();
    return normalizePoint(
      (event.clientX - bounds.left) / zoom,
      (event.clientY - bounds.top) / zoom,
      rect,
    );
  };
  const finishDrag = () => {
    const p = polys.find((p) => p.id === drag.current?.id);
    if (p) {
      const issue = polygonError(p.points);
      setError(issue ?? "");
    }
    drag.current = null;
    rectangleStart.current = null;
  };
  return (
    <div>
      <div className="roi-tools">
        <label className="inline-field">
          区域
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as typeof kind)}
          >
            <option value="include">包含区</option>
            <option value="exclude">排除区</option>
          </select>
        </label>
        <label className="inline-field">
          形状
          <select
            value={shape}
            onChange={(e) => {
              setShape(e.target.value as typeof shape);
              setPoints([]);
            }}
          >
            <option value="rectangle">矩形</option>
            <option value="polygon">多边形</option>
          </select>
        </label>
        <Button
          type="button"
          disabled={!image.url}
          onClick={() => setZoom((z) => (z === 1 ? 1.5 : 1))}
        >
          画布 {zoom === 1 ? "放大" : "还原"}
        </Button>
        <Button
          type="button"
          onClick={() => {
            onChange({
              source_aspect_ratio: `${natural.width}:${natural.height}`,
              include: [],
              exclude: [],
            });
            setPoints([]);
            setError("");
          }}
        >
          还原全画面
        </Button>
      </div>
      <div style={{ overflow: "auto", maxWidth: "100%" }}>
        <div
          className="roi-stage"
          ref={container}
          style={{
            transform: `scale(${zoom})`,
            transformOrigin: "top left",
            marginBottom: zoom > 1 ? 120 : 0,
          }}
        >
          {image.url ? (
            <img
              src={image.url}
              alt="检测区域绘制底图"
              onLoad={(e) =>
                setNatural({
                  width: e.currentTarget.naturalWidth,
                  height: e.currentTarget.naturalHeight,
                })
              }
            />
          ) : (
            <div className="preview-empty">
              请先完成连接测试，或在采集时读取一张快照。没有底图时仅可保留全画面。
            </div>
          )}
          <svg
            role="application"
            aria-label="检测区域画布；下方可用数字坐标编辑顶点"
            onPointerDown={(event) => {
              if (!image.url || drag.current) return;
              const point = coords(event);
              if (!point) return;
              event.currentTarget.setPointerCapture(event.pointerId);
              if (shape === "rectangle") {
                rectangleStart.current = point;
                setPoints([point, point, point, point]);
              } else if (points.length < 32) setPoints((p) => [...p, point]);
            }}
            onPointerMove={(event) => {
              const point = coords(event);
              if (!point) return;
              if (drag.current) {
                const { id, point: index } = drag.current;
                const p = polys.find((p) => p.id === id)!;
                onChange({
                  ...value,
                  [p.kind]: (value[p.kind] ?? []).map((r) =>
                    r.id === id
                      ? {
                          ...r,
                          points: r.points.map((v, i) =>
                            i === index ? point : v,
                          ),
                        }
                      : r,
                  ),
                });
              } else if (rectangleStart.current) {
                const a = rectangleStart.current;
                setPoints([a, [point[0], a[1]], point, [a[0], point[1]]]);
              }
            }}
            onPointerUp={() => {
              if (rectangleStart.current && points.length === 4) apply(points);
              finishDrag();
            }}
            onPointerCancel={finishDrag}
          >
            <defs>
              <pattern
                id="roi-hatch"
                width="8"
                height="8"
                patternUnits="userSpaceOnUse"
              >
                <path
                  d="M-2 2L2-2M0 8L8 0M6 10L10 6"
                  stroke="#e8bc70"
                  strokeWidth="2"
                />
              </pattern>
            </defs>
            {polys.map((p) => (
              <g key={p.id}>
                <polygon
                  points={p.points
                    .map(
                      ([x, y]) =>
                        `${rect.x + x * rect.width},${rect.y + y * rect.height}`,
                    )
                    .join(" ")}
                  fill={p.kind === "exclude" ? "url(#roi-hatch)" : "#2eab7533"}
                  stroke={p.kind === "exclude" ? "#edc88b" : "#9bd6b5"}
                  strokeWidth={2}
                />
                {p.points.map(([x, y], i) => (
                  <circle
                    key={i}
                    cx={rect.x + x * rect.width}
                    cy={rect.y + y * rect.height}
                    r={7}
                    fill={p.kind === "exclude" ? "#edc88b" : "#9bd6b5"}
                    onPointerDown={(event) => {
                      event.stopPropagation();
                      drag.current = { id: p.id, point: i };
                      setSelected(p.id);
                      event.currentTarget.setPointerCapture(event.pointerId);
                    }}
                  />
                ))}
              </g>
            ))}
            {points.length > 0 && (
              <polyline
                points={points
                  .map(
                    ([x, y]) =>
                      `${rect.x + x * rect.width},${rect.y + y * rect.height}`,
                  )
                  .join(" ")}
                fill="#9bd6b533"
                stroke="#ffffff"
                strokeWidth="2"
              />
            )}
          </svg>
        </div>
      </div>
      {shape === "polygon" && (
        <Button
          type="button"
          disabled={points.length < 3}
          onClick={() => apply(points)}
        >
          完成多边形（{points.length} 个点）
        </Button>
      )}
      <div className="roi-region-list">
        {polys.map((p) => (
          <Button
            type="button"
            key={p.id}
            onClick={() => setSelected(selected === p.id ? null : p.id)}
          >
            {p.kind === "include" ? "包含" : "排除"} · {p.id.slice(-5)}
          </Button>
        ))}
      </div>
      {selected && polys.find((p) => p.id === selected) && (
        <div className="inset stack" style={{ marginTop: 12 }}>
          <strong>精确编辑归一化顶点（0–1）</strong>
          {polys
            .find((p) => p.id === selected)!
            .points.map(([x, y], i) => (
              <div className="form-grid" key={i}>
                <Field label={`顶点 ${i + 1} X`}>
                  <input
                    type="number"
                    min="0"
                    max="1"
                    step="0.001"
                    value={Number(x.toFixed(3))}
                    onChange={(e) => {
                      const p = polys.find((p) => p.id === selected)!;
                      onChange({
                        ...value,
                        [p.kind]: (value[p.kind] ?? []).map((r) =>
                          r.id === p.id
                            ? {
                                ...r,
                                points: r.points.map((v, n) =>
                                  n === i ? [Number(e.target.value), v[1]] : v,
                                ),
                              }
                            : r,
                        ),
                      });
                    }}
                  />
                </Field>
                <Field label={`顶点 ${i + 1} Y`}>
                  <input
                    type="number"
                    min="0"
                    max="1"
                    step="0.001"
                    value={Number(y.toFixed(3))}
                    onChange={(e) => {
                      const p = polys.find((p) => p.id === selected)!;
                      onChange({
                        ...value,
                        [p.kind]: (value[p.kind] ?? []).map((r) =>
                          r.id === p.id
                            ? {
                                ...r,
                                points: r.points.map((v, n) =>
                                  n === i ? [v[0], Number(e.target.value)] : v,
                                ),
                              }
                            : r,
                        ),
                      });
                    }}
                  />
                </Field>
              </div>
            ))}
          <Button
            type="button"
            variant="danger"
            onClick={() => {
              const p = polys.find((p) => p.id === selected)!;
              onChange({
                ...value,
                [p.kind]: (value[p.kind] ?? []).filter(
                  (r) => r.id !== selected,
                ),
              });
              setSelected(null);
            }}
          >
            删除选中区域
          </Button>
        </div>
      )}
      {(error || polys.some((p) => polygonError(p.points))) && (
        <Notice tone="danger">
          {error || "区域坐标无效，请修正自交、越界或零面积区域。"}
        </Notice>
      )}
      <p className="caption muted" style={{ marginTop: 12 }}>
        留白不生成顶点；点位基于实际图像显示矩形。手机可放大后点选，也可用数字编辑；缩放不改变已存坐标。无包含区表示全图。
      </p>
    </div>
  );
}
