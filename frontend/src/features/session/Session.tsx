import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowRight, LockKeyhole, ShieldCheck, Video } from "lucide-react";
import { api, ApiError, safeReturnTo, setCsrf } from "../../api/client";
import { queryClient, stopPolling, useApi } from "../../api/query";
import { useAction } from "../../shared/hooks";
import {
  Button,
  ErrorNotice,
  Field,
  PageHeading,
  Panel,
  Notice,
} from "../../shared/components";
import { dateTime } from "../../shared/time";
export function Login() {
  const [search] = useSearchParams(),
    navigate = useNavigate(),
    action = useAction<unknown>();
  const [username, setUsername] = useState(""),
    [password, setPassword] = useState("");
  return (
    <div className="login-page">
      <div className="login-intro">
        <div className="brand">
          <span className="brand-mark">
            <Video size={25} />
          </span>
          <span>
            Camera / AI<small>个人活动工作台</small>
          </span>
        </div>
        <div>
          <span className="eyebrow">YOUR DAY, WITH CONTEXT</span>
          <h1>
            看见时间，
            <br />
            也看见依据。
          </h1>
          <p>从画面到活动记录，保留事实、来源与不确定性。</p>
        </div>
        <span className="caption">
          <ShieldCheck size={16} />
          同源会话保护 · 图片按保留期到期
        </span>
      </div>
      <main className="login-form">
        <span className="eyebrow">WORKSPACE / SIGN IN</span>
        <h2>登录工作台</h2>
        <p className="muted">使用你的个人账户继续。</p>
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            void action.run("login", async () => {
              let session;
              try {
                session = await api("login", { body: { username, password } });
              } catch (error) {
                if (
                  error instanceof ApiError &&
                  error.code === "INVALID_CREDENTIALS"
                )
                  setPassword("");
                throw error;
              }
              queryClient.clear();
              queryClient.setQueryData(["getSession", {}], session);
              setPassword("");
              navigate(safeReturnTo(search.get("return_to")), {
                replace: true,
              });
            });
          }}
        >
          <Field label="用户名">
            <input
              name="username"
              autoComplete="username"
              required
              maxLength={128}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </Field>
          <Field label="密码">
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <ErrorNotice error={action.error} />
          <Button type="submit" variant="primary" busy={action.pending}>
            登录
            <ArrowRight size={17} />
          </Button>
        </form>
        <p className="caption muted">
          <LockKeyhole size={13} />
          会话由安全 Cookie 管理，凭据不持久保存到浏览器。
        </p>
      </main>
    </div>
  );
}
export function Account() {
  const session = useApi("getSession"),
    action = useAction<unknown>(),
    navigate = useNavigate();
  const [current, setCurrent] = useState(""),
    [next, setNext] = useState(""),
    [confirm, setConfirm] = useState("");
  const clear = () => {
    stopPolling();
    window.dispatchEvent(new Event("media-invalidated"));
    void queryClient.cancelQueries();
    queryClient.clear();
    setCsrf("");
    navigate("/login", { replace: true });
  };
  return (
    <>
      <PageHeading title="账户" description="管理当前会话与登录密码。" />
      <div className="settings-layout">
        <Panel title="当前会话">
          <dl className="definition-list">
            <div>
              <dt>账户</dt>
              <dd>{session.data?.data.display_name}</dd>
            </div>
            <div>
              <dt>会话到期</dt>
              <dd>{dateTime(session.data?.data.expires_at)}</dd>
            </div>
          </dl>
          <Button
            onClick={() =>
              void action.run("logout", async () => {
                await api("logout");
                clear();
              })
            }
            busy={action.pending}
          >
            退出登录
          </Button>
        </Panel>
        <Panel title="修改密码">
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              void action.run("password", async () => {
                await api("updatePassword", {
                  body: { current_password: current, new_password: next },
                });
                setCurrent("");
                setNext("");
                setConfirm("");
                clear();
              });
            }}
          >
            <Field label="当前密码">
              <input
                autoComplete="current-password"
                type="password"
                required
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
              />
            </Field>
            <Field label="新密码" hint="至少 12 个字符">
              <input
                autoComplete="new-password"
                type="password"
                required
                minLength={12}
                maxLength={256}
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
            </Field>
            <Field
              label="再次输入新密码"
              error={
                confirm && confirm !== next ? "两次输入的密码不一致" : undefined
              }
            >
              <input
                autoComplete="new-password"
                type="password"
                required
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </Field>
            <Notice>修改成功后所有浏览器会话失效，需要重新登录。</Notice>
            <Button
              type="submit"
              variant="primary"
              busy={action.pending}
              disabled={next !== confirm}
            >
              修改密码并退出所有会话
            </Button>
          </form>
        </Panel>
      </div>
      <ErrorNotice error={action.error} />
    </>
  );
}
