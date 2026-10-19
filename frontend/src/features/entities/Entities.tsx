import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Box, Plus, UserRound } from "lucide-react";
import { api } from "../../api/client";
import { invalidate, useApi } from "../../api/query";
import { useAction } from "../../shared/hooks";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNotice,
  Field,
  Modal,
  Notice,
  PageHeading,
  Pager,
  Panel,
  QueryState,
  ScopeLink,
  useScope,
} from "../../shared/components";
import { ProtectedImage } from "../../shared/media/EphemeralImage";
export function Entities() {
  const scope = useScope(),
    navigate = useNavigate(),
    [adding, setAdding] = useState(false),
    [name, setName] = useState(""),
    [kind, setKind] = useState<"person" | "object">("person"),
    action = useAction<unknown>();
  const filter = scope.search.get("kind");
  const q = useApi("listEntities", {
    query: {
      kind: filter === "person" || filter === "object" ? filter : undefined,
      limit: 50,
      cursor: scope.search.get("cursor"),
    },
  });
  return (
    <>
      <PageHeading
        kicker="REFERENCE / PEOPLE & OBJECTS"
        title="人物与物品"
        description="长期参考资料独立保存，不随现场图到期。"
        action={
          <Button variant="primary" onClick={() => setAdding(true)}>
            <Plus size={17} />
            添加档案
          </Button>
        }
      />
      <Panel
        title="参考档案"
        action={
          <label className="inline-field">
            类型
            <select
              aria-label="档案类型筛选"
              value={filter ?? ""}
              onChange={(e) => scope.update("kind", e.target.value)}
            >
              <option value="">全部类型</option>
              <option value="person">人物</option>
              <option value="object">物品</option>
            </select>
          </label>
        }
      >
        <QueryState query={q}>
          {({ data, page }) => (
            <>
              {data.length ? (
                <div className="resource-list">
                  {data.map((e) => (
                    <ScopeLink
                      className="resource-row"
                      to={`/entities/${e.id}`}
                      key={e.id}
                    >
                      <span className="resource-thumbnail">
                        {e.references[0]?.content_url ? (
                          <ProtectedImage
                            url={e.references[0].content_url}
                            alt={e.name}
                          />
                        ) : e.kind === "person" ? (
                          <UserRound size={25} />
                        ) : (
                          <Box size={25} />
                        )}
                      </span>
                      <div>
                        <div className="row-wrap">
                          <h3>{e.name}</h3>
                          <Badge>
                            {e.origin === "automatic" ? "自动生成" : "手动添加"}
                            {!e.reviewed ? " · 待整理" : ""}
                          </Badge>
                          <Badge>{e.enabled ? "已启用" : "未启用草稿"}</Badge>
                        </div>
                        <p className="caption muted">
                          {e.kind === "person" ? "人物" : "物品"} ·{" "}
                          {e.camera_ids.length
                            ? `${e.camera_ids.length} 台机位`
                            : "所有机位"}{" "}
                          · {e.references.filter((r) => r.enabled).length}{" "}
                          张启用参考图 · v{e.revision}
                        </p>
                      </div>
                      <span className="text-link">管理资料 →</span>
                    </ScopeLink>
                  ))}
                </div>
              ) : (
                <EmptyState
                  title="尚无参考档案"
                  description="开启自动建档后，画面分析会生成人物和相关物品；也可以手动添加参考资料。"
                />
              )}
              <Pager hasMore={page.has_more} nextCursor={page.next_cursor} />
            </>
          )}
        </QueryState>
      </Panel>
      <Notice>
        参考资料只辅助识别与命名，系统按相关性选取代表图。没有档案也能正常记录活动。
      </Notice>
      {adding && (
        <Modal title="添加人物或物品" onClose={() => setAdding(false)}>
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              void action.run([name, kind], async (key) => {
                const r = await api("createEntity", {
                  body: {
                    name,
                    kind,
                    enabled: false,
                    is_primary_subject: false,
                  },
                  intent: key,
                });
                await invalidate("listEntities");
                setAdding(false);
                navigate(`/entities/${r.data.id}`);
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
            <Field label="类型">
              <select
                value={kind}
                onChange={(e) => setKind(e.target.value as typeof kind)}
              >
                <option value="person">人物</option>
                <option value="object">物品</option>
              </select>
            </Field>
            <Notice>
              可选地上传参考图帮助识别；无须建立主人物，也不要求多角度照片。
            </Notice>
            <ErrorNotice error={action.error} />
            <Button type="submit" variant="primary" busy={action.pending}>
              创建草稿
            </Button>
          </form>
        </Modal>
      )}
    </>
  );
}
