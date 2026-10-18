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
} from "../../shared/components";
import { ProtectedImage } from "../../shared/media/EphemeralImage";

export function EntityDetail() {
  const { entityId = "" } = useParams();
  const q = useApi("getEntity", { path: { entity_id: entityId } });
  return (
    <>
      <BackLink to="/entities" label="返回人物与物品" />
      <QueryState query={q}>
        {({ data: e }) => <EntityForm entity={e} />}
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
    [primary, setPrimary] = useState(entity.is_primary_subject),
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
    is_primary_subject: primary,
  };
  const dirty =
    JSON.stringify(patch) !==
    JSON.stringify({
      name: base.name,
      description: base.description,
      camera_ids: base.camera_ids,
      enabled: base.enabled,
      is_primary_subject: base.is_primary_subject,
    });
  const blocker = useUnsaved(dirty);
  // Reference mutations increment the entity revision; update baseline only with no unsaved edits.
  useEffect(() => {
    if (!dirty) setBase(entity);
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
              {base.kind === "person" && (
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={primary}
                    onChange={(e) => setPrimary(e.target.checked)}
                  />
                  设为唯一主人物，更新默认统计范围
                </label>
              )}
              {primary &&
                settings.data?.data.config.primary_subject_id !== base.id && (
                  <Notice tone="warning">
                    保存后将替换当前主人物；已有历史人物 ID 与名称快照保留。
                  </Notice>
                )}
              {(enabled || primary) &&
                !entity.references.some((r) => r.enabled) && (
                  <Notice tone="warning">
                    请先上传并启用至少一张参考图；没有参考图的档案不能宣称已具备识别资料。
                  </Notice>
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
                <dt>适用范围</dt>
                <dd>
                  {entity.camera_ids.length
                    ? `${entity.camera_ids.length} 台机位`
                    : "所有机位"}
                </dd>
              </div>
            </dl>
            <p className="caption muted">
              实际可用上限受当前服务验证能力、现场图预留与请求体上限共同约束；启用时由后端校验，不静默减图。
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
            <p>
              将撤销此档案参考图的后续读取；历史记录保留名称和版本。
              {base.is_primary_subject
                ? "主人物会改为未指定，不会自动指定其他人物。"
                : ""}
            </p>
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
                height: 200,
                overflow: "hidden",
                position: "relative",
                background: "#f0f3ed",
              }}
            >
              <img
                src={preview}
                alt="上传图与裁剪范围预览"
                style={{ width: "100%", height: "100%", objectFit: "contain" }}
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
