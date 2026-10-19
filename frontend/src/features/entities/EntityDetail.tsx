import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import { invalidate, useApi } from "../../api/query";
import type { EntityDto, ReferenceDto, Crop } from "../../api/generated/types";
import { useAction, useUnsaved } from "../../shared/hooks";
import {
  BackLink,
  Badge,
  Button,
  Confirm,
  ErrorNotice,
  Field,
  JobModal,
  Modal,
  Notice,
  PageHeading,
  Panel,
  QueryState,
  ScopeLink,
} from "../../shared/components";
import { ProtectedImage } from "../../shared/media/EphemeralImage";
import { dateTime, localDate } from "../../shared/time";

export function EntityDetail() {
  const { entityId = "" } = useParams();
  const q = useApi("getEntity", { path: { entity_id: entityId } });
  return (
    <>
      <BackLink to="/entities" label="返回人物与物品" />
      <QueryState query={q}>
        {({ data: e }) => <EntityForm key={e.id} entity={e} />}
      </QueryState>
    </>
  );
}
function EntityForm({ entity }: { entity: EntityDto }) {
  const navigate = useNavigate(),
    cameras = useApi("listCameras", { query: { limit: 100 } }),
    settings = useApi("getSettings");
  const [base, setBase] = useState(entity),
    [name, setName] = useState(entity.name),
    [description, setDescription] = useState(entity.description),
    [cameraIds, setCameraIds] = useState(entity.camera_ids),
    [enabled, setEnabled] = useState(entity.enabled),
    [upload, setUpload] = useState(false),
    [deleting, setDeleting] = useState(false),
    [job, setJob] = useState<string | null>(null),
    [message, setMessage] = useState(""),
    [latest, setLatest] = useState<EntityDto | null>(null);
  const action = useAction<unknown>();
  const patch = {
    name,
    description,
    camera_ids: cameraIds,
    enabled,
  };
  const dirty =
    JSON.stringify(patch) !==
    JSON.stringify({
      name: base.name,
      description: base.description,
      camera_ids: base.camera_ids,
      enabled: base.enabled,
    });
  const blocker = useUnsaved(dirty);
  // Reference mutations increment the entity revision; update baseline only with no unsaved edits.
  useEffect(() => {
    if (!dirty) {
      setBase(entity);
      setName(entity.name);
      setDescription(entity.description);
      setCameraIds(entity.camera_ids);
      setEnabled(entity.enabled);
    }
  }, [entity.revision]);
  const refresh = () =>
    invalidate(
      "getEntity",
      "listEntities",
      "getSettings",
      "listProviders",
      "getOverview",
    );
  const limit = settings.data?.data.effective_limits;
  return (
    <>
      <PageHeading
        kicker="REFERENCE / PROFILE"
        title={base.name}
        description={`${base.kind === "person" ? "人物" : "物品"} · v${entity.revision} · 长期参考资产`}
        action={
          <Badge tone={entity.enabled ? "success" : "neutral"}>
            {entity.enabled ? "已启用" : "草稿"}
          </Badge>
        }
      />
      {entity.merged_into_id && (
        <Notice>
          此档案已合并。
          <ScopeLink to={`/entities/${entity.merged_into_id}`}>
            查看合并后的档案
          </ScopeLink>
        </Notice>
      )}
      <div className="detail-columns">
        <div className="stack">
          <Panel title="档案信息">
            <form
              className="stack"
              onSubmit={(e) => {
                e.preventDefault();
                void action.run([base.revision, patch], async (key) => {
                  const r = await api("updateEntity", {
                    path: { entity_id: base.id },
                    revision: base.revision,
                    intent: key,
                    body: patch,
                  });
                  setBase(r.data);
                  setMessage(
                    "档案已保存。当前适用参考资料将用于后续分析与汇总。",
                  );
                  await refresh();
                });
              }}
            >
              <Field label="名称">
                <input
                  name="name"
                  required
                  maxLength={64}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </Field>
              <Field
                label="识别描述"
                hint="只填写可用于识别的事实描述，最多 1000 字。"
              >
                <textarea
                  maxLength={1000}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </Field>
              <div>
                <strong className="caption">适用机位</strong>
                <p className="caption muted">不勾选任何机位表示“所有机位”。</p>
                {cameras.data?.data.map((c) => (
                  <label className="check-row" key={c.id}>
                    <input
                      type="checkbox"
                      checked={cameraIds.includes(c.id)}
                      onChange={(e) =>
                        setCameraIds(
                          e.target.checked
                            ? [...cameraIds, c.id]
                            : cameraIds.filter((id) => id !== c.id),
                        )
                      }
                    />
                    {c.name}
                  </label>
                ))}
              </div>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                />
                启用此档案
              </label>
              {!entity.references.some((r) => r.enabled) && (
                <p className="caption muted">
                  暂缺代表图，活动记录正常。可手动补图，自动档案也会在后续画面中尝试补图。
                </p>
              )}
              <ErrorNotice error={action.error} />
              {action.error instanceof ApiError &&
                action.error.status === 412 && (
                  <Button
                    type="button"
                    onClick={() =>
                      void api("getEntity", {
                        path: { entity_id: base.id },
                      }).then((r) => setLatest(r.data))
                    }
                  >
                    读取新版并比较
                  </Button>
                )}
              {latest && (
                <div className="inset">
                  <p>
                    服务器 v{latest.revision}：{latest.name} ·{" "}
                    {latest.enabled ? "启用" : "未启用"} · {latest.description}
                  </p>
                  <Button
                    type="button"
                    onClick={() => {
                      setBase(latest);
                      setLatest(null);
                      action.setError(null);
                    }}
                  >
                    已核对差异，保留草稿采用新版基线
                  </Button>
                </div>
              )}
              <Button
                type="submit"
                variant="primary"
                busy={action.pending}
                disabled={!dirty || Boolean(latest)}
              >
                保存档案
              </Button>
              {message && <Notice>{message}</Notice>}
            </form>
          </Panel>
          <Panel
            title="参考图片"
            action={
              <Button onClick={() => setUpload(true)} disabled={dirty}>
                上传参考图
              </Button>
            }
          >
            {dirty && (
              <p className="caption muted">
                先保存档案变更，再操作参考图片，以保持版本基线一致。
              </p>
            )}
            <div className="reference-grid">
              {entity.references.map((r) => (
                <ReferenceCard
                  key={`${r.asset_id}-${r.revision}`}
                  reference={r}
                  disabled={dirty}
                  onChange={refresh}
                />
              ))}
            </div>
            {!entity.references.length && (
              <p className="muted">
                尚无参考图。支持 JPEG、PNG、WebP，单图 ≤8 MiB、≤1600 万像素。
              </p>
            )}
          </Panel>
        </div>
        <div className="stack">
          <Panel title="容量与保存边界">
            <dl className="definition-list">
              <div>
                <dt>启用参考图</dt>
                <dd>{entity.references.filter((r) => r.enabled).length} 张</dd>
              </div>
              <div>
                <dt>视觉请求有效总图上限</dt>
                <dd>
                  {limit?.vision && typeof limit.vision === "object"
                    ? String(
                        (limit.vision as Record<string, unknown>)
                          .max_total_images ?? "未知",
                      )
                    : "未配置"}
                </dd>
              </div>
              <div>
                <dt>适用范围</dt>
                <dd>
                  {entity.camera_ids.length
                    ? `${entity.camera_ids.length} 台机位`
                    : "所有机位"}
                </dd>
              </div>
            </dl>
            <p className="caption muted">
              实际可用上限受当前服务验证能力、现场图预留与请求体上限共同约束；后端按相关性选取代表图，档案不会因模型容量不足而阻断活动分析。
            </p>
            {limit && (
              <details className="diagnostic">
                <summary>查看当前有效上限</summary>
                <pre>{JSON.stringify(limit, null, 2)}</pre>
              </details>
            )}
            <Notice>
              主动保存的参考图属于独立长期资产，现场图 60 分钟 TTL 不适用于它。
            </Notice>
          </Panel>
          <div className="danger-zone">
            <Button
              variant="danger"
              onClick={() => setDeleting(true)}
              disabled={dirty}
            >
              删除档案
            </Button>
          </div>
        </div>
      </div>
      <EntityAssociations entity={entity} onChanged={refresh} />
      {upload && (
        <UploadReference
          entity={entity}
          onClose={() => setUpload(false)}
          onSuccess={() => {
            setUpload(false);
            void refresh();
          }}
        />
      )}
      {deleting && (
        <Confirm
          title={`删除「${base.name}」？`}
          description={
            <p>将撤销此档案参考图的后续读取；历史记录保留名称和版本。</p>
          }
          onClose={() => setDeleting(false)}
          error={action.error}
          busy={action.pending}
          danger
          label="确认删除"
          onConfirm={() =>
            void action.run(["delete", base.id, base.revision], async (key) => {
              const r = await api("deleteEntity", {
                path: { entity_id: base.id },
                intent: key,
                revision: base.revision,
              });
              setDeleting(false);
              setJob(r.data.id);
              await refresh();
            })
          }
        />
      )}{" "}
      {job && (
        <JobModal
          id={job}
          onClose={() => {
            setJob(null);
            blocker.allowNavigation();
            navigate("/entities");
          }}
        />
      )}
      {blocker.state === "blocked" && (
        <Confirm
          title="离开未保存的档案？"
          description={<p>当前表单变更尚未保存。</p>}
          onClose={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
          label="放弃变更并离开"
        />
      )}
    </>
  );
}
function ReferenceCard({
  reference: r,
  disabled,
  onChange,
}: {
  reference: ReferenceDto;
  disabled: boolean;
  onChange: () => Promise<unknown>;
}) {
  const [name, setName] = useState(r.name_snapshot),
    [replace, setReplace] = useState(false),
    [deleting, setDeleting] = useState(false),
    [job, setJob] = useState<string | null>(null),
    action = useAction<unknown>();
  const update = (body: { name?: string; enabled?: boolean }) =>
    action.run([r.asset_id, r.revision, body], async (key) => {
      await api("updateReference", {
        path: { asset_id: r.asset_id },
        revision: r.revision,
        intent: key,
        body,
      });
      await onChange();
    });
  return (
    <div className="reference-item">
      <div className="reference-photo">
        <ProtectedImage url={r.content_url} alt={r.name_snapshot} />
      </div>
      <Field label="图片名称">
        <input
          aria-label={`参考图名称 ${r.name_snapshot}`}
          maxLength={64}
          value={name}
          disabled={disabled}
          onChange={(e) => setName(e.target.value)}
        />
      </Field>
      <div className="row-between">
        <Badge tone={r.enabled ? "success" : "neutral"}>
          {r.enabled ? "启用" : "未启用"}
        </Badge>
        <span className="caption">资产 v{r.asset_version}</span>
      </div>
      <div className="actions">
        <Button
          disabled={disabled || !name.trim() || name === r.name_snapshot}
          busy={action.pending}
          onClick={() => void update({ name })}
        >
          改名
        </Button>
        <Button
          disabled={disabled}
          busy={action.pending}
          onClick={() => void update({ enabled: !r.enabled })}
        >
          {r.enabled ? "停用" : "启用"}
        </Button>
        <Button disabled={disabled} onClick={() => setReplace(true)}>
          换图
        </Button>
        <Button
          variant="danger"
          disabled={disabled}
          onClick={() => setDeleting(true)}
        >
          删除
        </Button>
      </div>
      <ErrorNotice error={action.error} />
      {replace && (
        <UploadReference
          entity={{ id: r.entity_id, revision: r.entity_revision } as EntityDto}
          reference={r}
          onClose={() => setReplace(false)}
          onSuccess={() => {
            setReplace(false);
            void onChange();
          }}
        />
      )}
      {deleting && (
        <Confirm
          title="删除参考图片？"
          description={
            <p>
              撤销旧任务对该资产的后续读取，历史文字保留名称和版本，原参考图将不可再查看。
            </p>
          }
          danger
          error={action.error}
          busy={action.pending}
          onClose={() => setDeleting(false)}
          onConfirm={() =>
            void action.run(["delete", r.asset_id, r.revision], async (key) => {
              const res = await api("deleteReference", {
                path: { asset_id: r.asset_id },
                revision: r.revision,
                intent: key,
              });
              setDeleting(false);
              setJob(res.data.id);
              await onChange();
            })
          }
          label="删除参考图"
        />
      )}
      {job && <JobModal id={job} onClose={() => setJob(null)} />}
    </div>
  );
}
function UploadReference({
  entity,
  reference,
  onClose,
  onSuccess,
}: {
  entity: EntityDto;
  reference?: ReferenceDto;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [file, setFile] = useState<File | null>(null),
    [preview, setPreview] = useState<string | null>(null),
    [name, setName] = useState(""),
    [issue, setIssue] = useState(""),
    [crop, setCrop] = useState<Crop>({ x: 0, y: 0, width: 1, height: 1 }),
    [dimensions, setDimensions] = useState({ width: 0, height: 0 });
  const action = useAction<unknown>(),
    url = useRef<string | null>(null);
  useEffect(
    () => () => {
      if (url.current) URL.revokeObjectURL(url.current);
    },
    [],
  );
  const select = async (value: File | undefined) => {
    if (url.current) URL.revokeObjectURL(url.current);
    setFile(null);
    setPreview(null);
    setIssue("");
    if (!value) return;
    if (
      !["image/jpeg", "image/png", "image/webp"].includes(value.type) ||
      value.size > 8 * 1024 * 1024
    ) {
      setIssue("请选择 ≤8 MiB 的 JPEG、PNG 或 WebP 图片。");
      return;
    }
    const u = URL.createObjectURL(value);
    url.current = u;
    const image = new Image();
    image.src = u;
    try {
      await image.decode();
      if (image.naturalWidth * image.naturalHeight > 16000000) {
        setIssue("图片不能超过 1600 万像素");
        URL.revokeObjectURL(u);
        return;
      }
      setDimensions({ width: image.naturalWidth, height: image.naturalHeight });
      setFile(value);
      setPreview(u);
      setName(value.name.replace(/\.[^.]+$/, "").slice(0, 64));
      setCrop({ x: 0, y: 0, width: 1, height: 1 });
    } catch {
      setIssue("无法解码这张图片");
      URL.revokeObjectURL(u);
    }
  };
  const valid =
    crop.x >= 0 &&
    crop.y >= 0 &&
    crop.width > 0 &&
    crop.height > 0 &&
    crop.x + crop.width <= 1.000001 &&
    crop.y + crop.height <= 1.000001;
  return (
    <Modal
      title={reference ? "替换参考图片" : "上传长期参考图片"}
      onClose={onClose}
    >
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          if (!file || !valid) return;
          const data = new FormData();
          data.append("file", file);
          data.append("crop", JSON.stringify(crop));
          if (!reference) {
            data.append("name", name);
            data.append("enabled", "false");
          }
          void action.run(
            [
              entity.id,
              reference?.asset_id,
              file.name,
              file.size,
              file.lastModified,
              crop,
              name,
            ],
            async (key) => {
              if (reference)
                await api("replaceReference", {
                  path: { asset_id: reference.asset_id },
                  revision: reference.revision,
                  intent: key,
                  body: data,
                });
              else
                await api("uploadReference", {
                  path: { entity_id: entity.id },
                  revision: entity.revision,
                  intent: key,
                  body: data,
                });
              onSuccess();
            },
          );
        }}
      >
        <Field label="选择图片">
          <input
            type="file"
            accept="image/jpeg,image/png,image/webp"
            onChange={(e) => void select(e.target.files?.[0])}
          />
        </Field>
        {issue && <Notice tone="danger">{issue}</Notice>}
        {preview && (
          <>
            <div
              className="reference-photo"
              style={{
                aspectRatio: `${dimensions.width}/${dimensions.height}`,
                overflow: "hidden",
                position: "relative",
                background: "#f0f3ed",
              }}
            >
              <img
                src={preview}
                alt="上传图与裁剪范围预览"
                style={{ width: "100%", height: "100%" }}
              />
              <span
                style={{
                  position: "absolute",
                  left: `${crop.x * 100}%`,
                  top: `${crop.y * 100}%`,
                  width: `${crop.width * 100}%`,
                  height: `${crop.height * 100}%`,
                  border: "2px solid #176B5B",
                  pointerEvents: "none",
                }}
              />
            </div>
            <p className="caption muted">
              {dimensions.width} × {dimensions.height} · 按 EXIF
              方向预览；裁剪按归一化坐标提交。
            </p>
            {!reference && (
              <Field label="参考图名称">
                <input
                  required
                  maxLength={64}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </Field>
            )}
            <div className="form-grid">
              {(["x", "y", "width", "height"] as const).map((key) => (
                <Field
                  label={
                    { x: "左边界", y: "上边界", width: "宽度", height: "高度" }[
                      key
                    ]
                  }
                  key={key}
                >
                  <input
                    type="number"
                    min={key === "x" || key === "y" ? 0 : 0.001}
                    max="1"
                    step="0.001"
                    value={crop[key]}
                    onChange={(e) =>
                      setCrop({ ...crop, [key]: Number(e.target.value) })
                    }
                  />
                </Field>
              ))}
            </div>
            {!valid && (
              <Notice tone="danger">裁剪须在图片内，且宽高大于 0。</Notice>
            )}
          </>
        )}
        <Notice>
          {reference
            ? "换图会创建新的资产版本，不覆盖历史版本标识。"
            : "新参考图先保存为未启用资料，可检查后再启用。"}
          长期参考资产不会随现场图 TTL 到期。
        </Notice>
        <ErrorNotice error={action.error} />
        <Button
          type="submit"
          variant="primary"
          disabled={!file || !valid}
          busy={action.pending}
        >
          {reference ? "确认替换为新版本" : "上传未启用参考图"}
        </Button>
      </form>
    </Modal>
  );
}

function EntityAssociations({
  entity,
  onChanged,
}: {
  entity: EntityDto;
  onChanged: () => unknown;
}) {
  const navigate = useNavigate();
  const [cursor, setCursor] = useState<string | undefined>(),
    [selected, setSelected] = useState<string[]>([]),
    [target, setTarget] = useState(""),
    [mode, setMode] = useState<"merge" | "split" | null>(null),
    [name, setName] = useState("");
  const occurrences = useApi("listEntityOccurrences", {
    path: { entity_id: entity.id },
    query: { limit: 30, cursor },
  });
  const entities = useApi("listEntities", {
    query: { kind: entity.kind, limit: 100 },
  });
  const action = useAction<unknown>();
  const other = entities.data?.data.find((e) => e.id === target);
  const submit = async () => {
    await action.run(
      [mode, entity.revision, target, selected, name],
      async (key) => {
        const result =
          mode === "merge" && other
            ? await api("mergeEntity", {
                path: { entity_id: entity.id },
                revision: entity.revision,
                intent: key,
                body: {
                  target_entity_id: other.id,
                  target_revision: other.revision,
                },
              })
            : await api("splitEntity", {
                path: { entity_id: entity.id },
                revision: entity.revision,
                intent: key,
                body: { name: name.trim(), occurrence_ids: selected },
              });
        await invalidate(
          "getEntity",
          "listEntities",
          "listEntityOccurrences",
          "getOverview",
          "getTimeline",
          "listEvents",
          "getEvent",
          "listMinutes",
          "getMinute",
          "listSummaries",
          "getSummary",
        );
        onChanged();
        setMode(null);
        setSelected([]);
        navigate(`/entities/${result.data.id}`);
      },
    );
  };
  if (entity.merged_into_id) return null;
  return (
    <Panel title="出现记录与关联修订">
      <p className="caption muted">
        名字和特征可在上方修改。合并保留目标档案名称；误认的出现记录可以拆为另一档案，活动时长不因未命名而丢失。
      </p>
      <div className="row-wrap">
        <label className="inline-field">
          合并到
          <select
            aria-label="合并到"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="">选择同类型档案</option>
            {entities.data?.data
              .filter((e) => e.id !== entity.id)
              .map((e) => (
                <option key={e.id} value={e.id}>
                  {e.name}
                </option>
              ))}
          </select>
        </label>
        <Button disabled={!other} onClick={() => setMode("merge")}>
          合并档案
        </Button>
      </div>
      <QueryState query={occurrences}>
        {({ data, page }) => (
          <div className="stack">
            {data.length === 0 && (
              <p className="muted">
                尚无自动出现记录；定期画面分析后会逐步补充。
              </p>
            )}
            {data.map((o) => (
              <label className="check-row" key={o.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(o.id)}
                  onChange={(e) =>
                    setSelected(
                      e.target.checked
                        ? [...selected, o.id]
                        : selected.filter((id) => id !== o.id),
                    )
                  }
                />
                {dateTime(o.captured_at)} · {o.manual ? "人工修订" : "自动关联"}
                <ScopeLink
                  className="text-link"
                  to={`/minutes?date=${localDate(o.captured_at)}&camera_id=${encodeURIComponent(o.camera_id)}`}
                >
                  查看记录
                </ScopeLink>
              </label>
            ))}
            <div className="row-wrap">
              <Button
                disabled={!selected.length}
                onClick={() => {
                  setName(
                    `${entity.kind === "person" ? "人物" : "物品"}（拆分）`,
                  );
                  setMode("split");
                }}
              >
                将所选 {selected.length} 次出现拆为新档案
              </Button>
              {page.has_more && (
                <Button
                  onClick={() => {
                    setCursor(page.next_cursor ?? undefined);
                    setSelected([]);
                  }}
                >
                  下一页
                </Button>
              )}
              {cursor && (
                <Button
                  onClick={() => {
                    setCursor(undefined);
                    setSelected([]);
                  }}
                >
                  返回最新
                </Button>
              )}
            </div>
          </div>
        )}
      </QueryState>
      {mode && (
        <Modal
          title={
            mode === "merge"
              ? `将「${entity.name}」合并到「${other?.name}」`
              : "拆分所选出现记录"
          }
          onClose={() => setMode(null)}
        >
          <div className="stack">
            {mode === "split" ? (
              <Field label="新档案名称">
                <input
                  required
                  maxLength={64}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </Field>
            ) : (
              <p>目标名称和已有历史版本会保留。此操作会更新当前关联记录。</p>
            )}
            <ErrorNotice error={action.error} />
            <div className="actions end">
              <Button onClick={() => setMode(null)}>取消</Button>
              <Button
                variant="primary"
                busy={action.pending}
                disabled={mode === "split" && !name.trim()}
                onClick={() => void submit()}
              >
                确认{mode === "merge" ? "合并" : "拆分"}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </Panel>
  );
}
