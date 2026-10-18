import { lazy, Suspense, useEffect, useState } from "react";
import {
  createBrowserRouter,
  Navigate,
  Outlet,
  RouterProvider,
  useLocation,
  useNavigate,
  useRouteError,
  ScrollRestoration,
} from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import {
  Activity,
  Camera,
  CalendarDays,
  ChevronRight,
  CircleUserRound,
  Clock3,
  FileText,
  LayoutDashboard,
  MoreHorizontal,
  Radio,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Users,
  Video,
  Workflow,
} from "lucide-react";
import { useApi, queryClient, stopPolling } from "../api/query";
import { ApiError, sessionEvents, setCsrf } from "../api/client";
import {
  ErrorNotice,
  PageHeading,
  QueryState,
  ScopeLink,
  Skeleton,
  Modal,
} from "../shared/components";
import { dateLabel, localDate } from "../shared/time";

const load = <T extends Record<string, unknown>>(
  fn: () => Promise<T>,
  key: keyof T,
) => lazy(async () => ({ default: (await fn())[key] as React.ComponentType }));
const Overview = load(
  () => import("../features/overview/Overview"),
  "Overview",
);
const Timeline = load(
  () => import("../features/timeline/Timeline"),
  "Timeline",
);
const EventDetail = load(
  () => import("../features/timeline/EventDetail"),
  "EventDetail",
);
const Minutes = load(() => import("../features/minutes/Minutes"), "Minutes");
const MinuteDetail = load(
  () => import("../features/minutes/Minutes"),
  "MinuteDetail",
);
const Reports = load(() => import("../features/reports/Reports"), "Reports");
const ReportDetail = load(
  () => import("../features/reports/Reports"),
  "ReportDetail",
);
const Cameras = load(() => import("../features/cameras/Cameras"), "Cameras");
const CameraDetail = load(
  () => import("../features/cameras/CameraDetail"),
  "CameraDetail",
);
const Entities = load(
  () => import("../features/entities/Entities"),
  "Entities",
);
const EntityDetail = load(
  () => import("../features/entities/EntityDetail"),
  "EntityDetail",
);
const AISettings = load(
  () => import("../features/settings/AISettings"),
  "AISettings",
);
const AnalysisSettings = load(
  () => import("../features/settings/AnalysisSettings"),
  "AnalysisSettings",
);
const Status = load(
  () => import("../features/operations/Operations"),
  "Status",
);
const Usage = load(() => import("../features/operations/Operations"), "Usage");
const Login = load(() => import("../features/session/Session"), "Login");
const Account = load(() => import("../features/session/Session"), "Account");

export const navigation = [
  { to: "/", label: "今日概览", icon: LayoutDashboard, group: "查看" },
  { to: "/timeline", label: "事件时间线", icon: Clock3, group: "查看" },
  { to: "/minutes", label: "分钟记录", icon: CalendarDays, group: "查看" },
  { to: "/reports", label: "小时 / 日报", icon: FileText, group: "查看" },
  { to: "/cameras", label: "摄像头", icon: Camera, group: "管理" },
  { to: "/entities", label: "人物与物品", icon: Users, group: "管理" },
  { to: "/settings/ai", label: "AI 服务", icon: Workflow, group: "管理" },
  {
    to: "/settings/analysis",
    label: "分析与保存",
    icon: SlidersHorizontal,
    group: "管理",
  },
  { to: "/status", label: "设备与任务", icon: Activity, group: "管理" },
  { to: "/usage", label: "用量", icon: Radio, group: "管理" },
  { to: "/account", label: "账户", icon: CircleUserRound, group: "管理" },
];
function SessionBridge() {
  const navigate = useNavigate(),
    location = useLocation();
  useEffect(() => {
    const expired = () => {
      stopPolling();
      window.dispatchEvent(new Event("media-invalidated"));
      void queryClient.cancelQueries();
      setCsrf("");
      navigate(
        `/login?return_to=${encodeURIComponent(location.pathname + location.search)}`,
        { replace: true },
      );
      queryClient.clear();
    };
    const refresh = () => {
      void queryClient.invalidateQueries({ queryKey: ["getSession"] });
    };
    sessionEvents.addEventListener("expired", expired);
    sessionEvents.addEventListener("csrf", refresh);
    return () => {
      sessionEvents.removeEventListener("expired", expired);
      sessionEvents.removeEventListener("csrf", refresh);
    };
  }, [navigate, location.pathname, location.search]);
  return (
    <>
      <Outlet />
      <ScrollRestoration />
    </>
  );
}
function Protected() {
  const query = useApi("getSession"),
    location = useLocation();
  if (query.error instanceof ApiError && query.error.status === 401)
    return (
      <Navigate
        to={`/login?return_to=${encodeURIComponent(location.pathname + location.search)}`}
        replace
      />
    );
  return <QueryState query={query}>{() => <Shell />}</QueryState>;
}
function Shell() {
  const location = useLocation();
  const active = (path: string) =>
    path === "/"
      ? location.pathname === "/"
      : location.pathname.startsWith(path) ||
        (path === "/timeline" && location.pathname.startsWith("/events/"));
  const current = navigation.find((n) => active(n.to));
  const session = queryClient.getQueryData<{
    data: { display_name: string; username: string };
  }>(["getSession", {}]);
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside className="sidebar">
        <ScopeLink to="/" className="brand" aria-label="Camera AI 工作台">
          <span className="brand-mark">
            <Video size={24} />
          </span>
          <span>
            Camera<span className="brand-ai"> / AI</span>
            <small>个人活动工作台</small>
          </span>
        </ScopeLink>
        <nav aria-label="主导航">
          {["查看", "管理"].map((group) => (
            <div className="nav-group" key={group}>
              <p>{group}</p>
              {navigation
                .filter((n) => n.group === group && n.to !== "/account")
                .map((n) => (
                  <ScopeLink
                    to={n.to}
                    className={`nav-item ${active(n.to) ? "active" : ""}`}
                    key={n.to}
                    aria-current={active(n.to) ? "page" : undefined}
                  >
                    <n.icon size={18} strokeWidth={1.7} />
                    <span>{n.label}</span>
                    {active(n.to) && <span className="nav-active-dot" />}
                  </ScopeLink>
                ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="privacy-note">
            <ShieldCheck size={17} />
            <span>证据有期限，记录可追溯</span>
          </div>
          <ScopeLink to="/account" className="account-link">
            <span className="avatar">
              {(session?.data.display_name ?? "我").slice(0, 1)}
            </span>
            <span>
              <strong>{session?.data.display_name ?? "我的账户"}</strong>
              <small>个人工作空间</small>
            </span>
            <Settings2 size={16} />
          </ScopeLink>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div>
            <span className="topbar-mark">
              <Video size={18} />
            </span>
            <span>工作空间</span>
            <ChevronRight size={14} />
            <strong>
              {current?.label ??
                (location.pathname === "/more" ? "更多" : "记录详情")}
            </strong>
          </div>
          <span className="topbar-date">
            {dateLabel(localDate())}
            <span className="divider-dot">·</span>北京时间
          </span>
          <ScopeLink to="/status" className="topbar-status">
            <Activity size={16} />
            <span>运行状态</span>
          </ScopeLink>
        </header>
        <main id="main-content" className="main-content">
          <Suspense fallback={<Skeleton lines={6} />}>
            <div className="route-content" key={location.pathname}>
              <Outlet />
            </div>
          </Suspense>
        </main>
        <footer className="workspace-footer">
          <span>Camera AI Workbench</span>
          <span>可观察的事实 · 可追溯的记录</span>
        </footer>
      </div>
      <nav className="bottom-nav" aria-label="手机导航">
        {[
          { to: "/", label: "概览", icon: LayoutDashboard },
          { to: "/timeline", label: "时间线", icon: Clock3 },
          { to: "/reports", label: "报告", icon: FileText },
          { to: "/more", label: "更多", icon: MoreHorizontal },
        ].map((n) => (
          <ScopeLink
            key={n.to}
            to={n.to}
            className={
              active(n.to) ||
              (n.to === "/more" &&
                !["/", "/timeline", "/reports"].some((p) => active(p)))
                ? "active"
                : ""
            }
            aria-current={active(n.to) ? "page" : undefined}
          >
            <n.icon size={21} />
            <span>{n.label}</span>
          </ScopeLink>
        ))}
      </nav>
    </div>
  );
}
function More() {
  return (
    <>
      <PageHeading title="更多" description="分钟记录、管理与账户" />
      <div className="more-list">
        {navigation
          .filter((n) => !["/", "/timeline", "/reports"].includes(n.to))
          .map((n) => (
            <ScopeLink to={n.to} key={n.to}>
              <n.icon size={20} />
              <span>{n.label}</span>
              <ChevronRight size={18} />
            </ScopeLink>
          ))}
      </div>
    </>
  );
}
function RouteError() {
  const error = useRouteError();
  return (
    <div className="fatal-error">
      <PageHeading title="页面暂时无法打开" />
      <ErrorNotice error={error} />
      <a className="button primary" href="/">
        返回概览
      </a>
    </div>
  );
}
function NotFound() {
  return (
    <>
      <PageHeading
        title="页面不存在"
        description="此地址未对应可访问的工作台页面。"
      />
      <ScopeLink to="/" className="button primary">
        返回概览
      </ScopeLink>
    </>
  );
}
function EventRoute() {
  const location = useLocation(),
    navigate = useNavigate();
  const [wide, setWide] = useState(
    () => matchMedia("(min-width: 1200px)").matches,
  );
  useEffect(() => {
    const media = matchMedia("(min-width: 1200px)"),
      fn = () => setWide(media.matches);
    media.addEventListener("change", fn);
    return () => media.removeEventListener("change", fn);
  }, []);
  return wide && location.state?.eventDrawer ? (
    <>
      <PageHeading title="活动记录" description="查看活动详情与来源证据。" />
      <div className="detail-drawer">
        <Modal title="活动详情" onClose={() => navigate(-1)} wide>
          <EventDetail />
        </Modal>
      </div>
    </>
  ) : (
    <EventDetail />
  );
}
const router = createBrowserRouter([
  {
    element: <SessionBridge />,
    errorElement: <RouteError />,
    children: [
      { path: "login", element: <Login /> },
      {
        element: <Protected />,
        children: [
          { index: true, element: <Overview /> },
          { path: "timeline", element: <Timeline /> },
          { path: "events/:eventId", element: <EventRoute /> },
          { path: "minutes", element: <Minutes /> },
          { path: "minutes/:minuteId", element: <MinuteDetail /> },
          { path: "reports", element: <Reports /> },
          { path: "reports/:summaryId", element: <ReportDetail /> },
          { path: "cameras", element: <Cameras /> },
          { path: "cameras/new", element: <CameraDetail /> },
          { path: "cameras/:cameraId", element: <CameraDetail /> },
          { path: "entities", element: <Entities /> },
          { path: "entities/:entityId", element: <EntityDetail /> },
          { path: "settings/ai", element: <AISettings /> },
          { path: "settings/analysis", element: <AnalysisSettings /> },
          { path: "status", element: <Status /> },
          { path: "usage", element: <Usage /> },
          { path: "account", element: <Account /> },
          { path: "more", element: <More /> },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
  },
]);
export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Suspense fallback={<Skeleton lines={5} />}>
        <RouterProvider router={router} />
      </Suspense>
    </QueryClientProvider>
  );
}
