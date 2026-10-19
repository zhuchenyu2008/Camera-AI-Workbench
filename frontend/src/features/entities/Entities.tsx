import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Plus, UserRound } from "lucide-react";
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
    action = useAction<unknown>();
  const filter = scope.search.get("kind");
  const q = useApi("listEntities", {
    query: {
      kind: filter === "person" ? filter : undefined,
      limit: 50,
      cursor: scope.search.get("cursor"),
    },
  });
  return (
    <>
      <PageHeading
        kicker="REFERENCE / PEOPLE"
        title="人物档案"
        description="人物参考图独立保存，不随现场图到期。"
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
          <span className="caption muted">仅包含人物</span>
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
                        ) : (
                          <UserRound size={25} />
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
                          人物 ·{" "}
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
                  description="开启自动建档后，画面分析会生成可辨认的人物；也可以手动添加人物参考资料。"
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
        <Modal title="添加人物" onClose={() => setAdding(false)}>
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              void action.run([name], async (key) => {
                const r = await api("createEntity", {
                  body: {
                    name,
                    kind: "person",
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
