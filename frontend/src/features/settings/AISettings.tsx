import { useState } from "react";
import { CheckCircle2, Plus, Workflow } from "lucide-react";
import { api, ApiError } from "../../api/client";
import { invalidate, useApi } from "../../api/query";
import type { ProviderDto, ProviderWrite } from "../../api/generated/types";
import { fields } from "../../api/generated/fields";
import { useAction } from "../../shared/hooks";
import {
  Badge,
  Button,
  Confirm,
  EmptyState,
  ErrorNotice,
  Field,
  JobModal,
  Modal,
  Notice,
  PageHeading,
  Pager,
  Panel,
  QueryState,
  useScope,
} from "../../shared/components";
import { dateTime } from "../../shared/time";
export function AISettings() {
  const scope = useScope(),
    q = useApi("listProviders", {
      query: { limit: 50, cursor: scope.search.get("cursor") },
    }),
    [editing, setEditing] = useState<ProviderDto | "new" | null>(null),
    [job, setJob] = useState<string | null>(null),
    [deleting, setDeleting] = useState<ProviderDto | null>(null),
    action = useAction<unknown>();
  return (
    <>
      <PageHeading
        kicker="SETTINGS / AI CONNECTIONS"
        title="AI 服务"
        description="验证实际视觉与结构化输出能力，再选择服务用途。"
        action={
          <Button variant="primary" onClick={() => setEditing("new")}>
            <Plus size={17} />
            添加服务
          </Button>
        }
      />
      <Panel title="已配置服务">
        <QueryState query={q}>
          {({ data, page }) => (
            <>
              {data.length ? (
                <div className="resource-list">
                  {data.map((p) => (
                    <div className="resource-row" key={p.id}>
                      <span className="resource-thumbnail">
                        <Workflow size={26} />
                      </span>
                      <div>
                        <div className="row-wrap">
                          <h3>{p.name}</h3>
                          <Badge
                            tone={
                              p.tested_config_revision === p.revision &&
                              p.capabilities.vision
                                ? "success"
                                : "warning"
                            }
                          >
                            {p.tested_config_revision === p.revision &&
                            p.capabilities.vision
                              ? "当前配置已验证"
                              : "待测试当前配置"}
                          </Badge>
                        </div>
                        <p className="caption muted">
                          {p.model} ·{" "}
                          {p.api_key_configured ? "密钥已配置" : "未配置密钥"}
                        </p>
                        <p
                          className="caption muted"
                          style={{ overflowWrap: "anywhere" }}
                        >
                          {p.base_url}
                        </p>
                        <p className="caption muted">
                          测试 {dateTime(p.tested_at)} · 已验证{" "}
                          {p.capabilities.verified_max_images ?? "未知"} 张图 ·
                          v{p.revision}
                        </p>
                        <div className="row-wrap">
                          <Badge>
                            {p.capabilities.vision
                              ? "视觉已验证"
                              : "视觉待验证"}
                          </Badge>
                          <Badge>
                            {p.capabilities.data_url
                              ? "图片输入已验证"
                              : "图片输入待验证"}
                          </Badge>
                          <Badge>
                            结构化{" "}
                            {p.capabilities.structured_output_modes?.join(
                              "、",
                            ) || "待验证"}
                          </Badge>
                        </div>
                        {p.last_test_error && (
                          <p className="caption">
                            最近测试错误：{p.last_test_error}
                          </p>
                        )}
                      </div>
                      <div className="actions">
                        <Button onClick={() => setEditing(p)}>编辑配置</Button>
                        <Button
                          busy={action.pending}
                          onClick={() =>
                            void action.run(
                              ["test", p.id, p.revision],
                              async (key) => {
                                const r = await api("testProvider", {
                                  intent: key,
                                  body: {
                                    provider_id: p.id,
                                    config_revision: p.revision,
                                  },
                                });
                                setJob(r.data.id);
                              },
                            )
                          }
                        >
                          测试实际能力
                        </Button>
                        <Button variant="danger" onClick={() => setDeleting(p)}>
                          删除
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <EmptyState
                  title="尚未配置 AI 服务"
                  description="填写 OpenAI 兼容服务的 API 前缀、模型与密钥，随后执行能力测试。"
                />
              )}
              <Pager hasMore={page.has_more} nextCursor={page.next_cursor} />
            </>
          )}
        </QueryState>
      </Panel>
      <Notice>
        测试会实际检查单图、多图、参考资料加现场图与结构化汇总输出。仅列出模型名称或服务可连接，不能代表视觉分析可用。测试调用会计入用量。
      </Notice>
      <ErrorNotice error={action.error} />
      {editing && (
        <ProviderEditor
          provider={editing === "new" ? undefined : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void invalidate("listProviders", "getSettings");
          }}
        />
      )}
      {job && <JobModal id={job} onClose={() => setJob(null)} />}{" "}
      {deleting && (
        <Confirm
          title={`删除「${deleting.name}」？`}
          description={
            <p>
              正在被视觉或汇总用途引用的服务需先在“分析与保存”中更换。删除不会清除历史用量。
            </p>
          }
          onClose={() => setDeleting(null)}
          error={action.error}
          busy={action.pending}
          danger
          label="删除服务"
          onConfirm={() =>
            void action.run(
              ["delete", deleting.id, deleting.revision],
              async (key) => {
                await api("deleteProvider", {
                  path: { provider_id: deleting.id },
                  revision: deleting.revision,
                  intent: key,
                });
                setDeleting(null);
                await invalidate("listProviders", "getSettings");
              },
            )
          }
        />
      )}
    </>
  );
}
function ProviderEditor({
  provider,
  onClose,
  onSaved,
}: {
  provider?: ProviderDto;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [base, setBase] = useState(provider),
    [name, setName] = useState(provider?.name ?? ""),
    [baseUrl, setBaseUrl] = useState(provider?.base_url ?? ""),
    [model, setModel] = useState(provider?.model ?? ""),
    [key, setKey] = useState(""),
    [clearKey, setClearKey] = useState(false),
    [latest, setLatest] = useState<ProviderDto | null>(null);
  const defaults = Object.fromEntries(
    Object.entries(fields.ProviderWrite)
      .filter(([, v]) => "default" in v)
      .map(([k, v]) => [k, (v as { default: unknown }).default]),
  );
  const [config, setConfig] = useState<Record<string, unknown>>({
    ...defaults,
    ...provider?.config,
  });
  const action = useAction<unknown>();
  const numeric: [keyof ProviderWrite, string][] = [
    ["timeout_seconds", "请求超时（秒）"],
    ["max_capture_images", "现场图上限"],
    ["max_total_images", "总图上限（含参考图）"],
    ["max_request_body_mib", "请求体上限（MiB）"],
    ["output_token_limit", "输出 Token 上限"],
  ];
  return (
    <Modal
      title={base ? "编辑 AI 服务" : "添加 AI 服务"}
      onClose={onClose}
      wide
    >
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          const body: ProviderWrite = {
            ...config,
            name,
            base_url: baseUrl,
            model,
            api_key_action: clearKey ? "clear" : "keep",
            ...(key ? { api_key: key } : {}),
          } as ProviderWrite;
          void action.run([base?.revision, body], async (intent) => {
            if (base)
              await api("updateProvider", {
                path: { provider_id: base.id },
                revision: base.revision,
                intent,
                body,
              });
            else await api("createProvider", { intent, body });
            setKey("");
            onSaved();
          });
        }}
      >
        <div className="form-grid">
          <Field label="服务名称">
            <input
              name="name"
              required
              maxLength={64}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field
            label="API 前缀"
            hint="填写完整 /v1 等 API 前缀，主机须在部署白名单内。"
          >
            <input
              name="base_url"
              type="url"
              required
              maxLength={2048}
              placeholder="https://api.example.com/v1"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </Field>
          <Field label="模型名称">
            <input
              name="model"
              required
              maxLength={200}
              value={model}
              onChange={(e) => setModel(e.target.value)}
            />
          </Field>
          <Field
            label="API Key"
            hint={
              base?.api_key_configured
                ? "密钥已配置，留空保持原值"
                : "提交后仅显示是否已配置，不回显密钥"
            }
          >
            <input
              type="password"
              autoComplete="new-password"
              maxLength={4096}
              value={key}
              onChange={(e) => setKey(e.target.value)}
            />
          </Field>
          {numeric.map(([field, label]) => {
            const rule = fields.ProviderWrite[
              field as keyof typeof fields.ProviderWrite
            ] as { minimum?: number; maximum?: number };
            return (
              <Field key={field} label={label}>
                <input
                  type="number"
                  name={field}
                  required
                  min={rule.minimum}
                  max={rule.maximum}
                  value={Number(config[field] ?? "")}
                  onChange={(e) =>
                    setConfig({ ...config, [field]: Number(e.target.value) })
                  }
                />
              </Field>
            );
          })}
          <Field label="结构化输出格式">
            <select
              value={String(config.response_format_mode)}
              onChange={(e) =>
                setConfig({ ...config, response_format_mode: e.target.value })
              }
            >
              <option value="json_schema">JSON Schema</option>
              <option value="json_object">JSON Object</option>
              <option value="prompt_json">提示词 JSON</option>
            </select>
          </Field>
          <Field label="输出 Token 参数">
            <select
              value={String(config.output_token_parameter)}
              onChange={(e) =>
                setConfig({ ...config, output_token_parameter: e.target.value })
              }
            >
              <option value="max_completion_tokens">
                max_completion_tokens
              </option>
              <option value="max_tokens">max_tokens</option>
            </select>
          </Field>
          <Field label="图像 detail 参数">
            <select
              value={String(config.detail_mode)}
              onChange={(e) =>
                setConfig({ ...config, detail_mode: e.target.value })
              }
            >
              {["auto", "low", "high", "omit"].map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </Field>
        </div>
        {base?.api_key_configured && (
          <label className="check-row">
            <input
              type="checkbox"
              checked={clearKey}
              onChange={(e) => setClearKey(e.target.checked)}
            />
            明确清除已保存密钥
          </label>
        )}
        <Notice>
          修改模型、图数、格式或连接信息后，需要重新测试当前配置的能力；旧测试不能证明新配置可用。
        </Notice>
        <ErrorNotice error={action.error} />
        {action.error instanceof ApiError &&
          action.error.status === 412 &&
          base && (
            <Button
              type="button"
              onClick={() =>
                void api("listProviders", { query: { limit: 100 } }).then((r) =>
                  setLatest(r.data.find((p) => p.id === base.id) ?? null),
                )
              }
            >
              读取服务器新版，保留草稿
            </Button>
          )}
        {latest && (
          <div className="inset stack">
            <p>
              服务器 v{latest.revision}：{latest.name} · {latest.model} ·{" "}
              {latest.base_url}
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
        <div className="actions end">
          <Button type="button" onClick={onClose}>
            取消
          </Button>
          <Button
            type="submit"
            variant="primary"
            busy={action.pending}
            disabled={!!latest}
          >
            保存配置
          </Button>
        </div>
      </form>
    </Modal>
  );
}
