import { useState } from 'react'
import { BrowserRouter, Link, NavLink, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { AppProviders, useWorkspace } from './workspace'
import { Icon, BrandMark, StatusPill, Button } from './ui'
import { formatDateTime } from './utils'
import { DashboardPage } from './dashboard'
import { TimelinePage } from './timeline'
import { SummaryPage, UsagePage } from './reports'
import { SettingsRoutes } from './settings'
import './styles.css'

const primary = [
  { to: '/', label: '工作台', icon: 'home', end: true },
  { to: '/timeline', label: '时间轴', icon: 'timeline' },
  { to: '/summary', label: '总结', icon: 'summary' },
  { to: '/usage', label: '使用量', icon: 'usage' },
]
const management = [
  { to: '/settings/cameras', label: '摄像头', icon: 'camera' },
  { to: '/settings/ai', label: 'AI Provider', icon: 'ai' },
  { to: '/settings/analysis', label: '分析策略', icon: 'activity' },
  { to: '/settings/retention', label: '留存与存储', icon: 'database' },
  { to: '/settings/activities', label: '活动目录', icon: 'timeline' },
  { to: '/settings/persons', label: '匿名人物', icon: 'users' },
  { to: '/settings/system', label: '系统与健康', icon: 'system' },
]

export function App() {
  return <BrowserRouter><AppProviders><Routes>
    <Route element={<AppShell />}>
      <Route index element={<DashboardPage />} />
      <Route path="timeline" element={<TimelinePage />} />
      <Route path="timeline/:cameraId" element={<TimelinePage />} />
      <Route path="summary" element={<SummaryPage />} />
      <Route path="usage" element={<UsagePage />} />
      <Route path="settings/*" element={<SettingsRoutes />} />
      <Route path="*" element={<NotFound />} />
    </Route>
  </Routes></AppProviders></BrowserRouter>
}

function AppShell() {
  const { session, realtime, theme, setTheme, logout, timezone } = useWorkspace()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const inSettings = location.pathname.startsWith('/settings')
  const title = location.pathname === '/' ? '工作台' : location.pathname.startsWith('/timeline') ? '时间轴' : location.pathname === '/summary' ? '总结' : location.pathname === '/usage' ? '使用量与任务' : management.find((item) => location.pathname === item.to)?.label ?? '管理设置'
  const clock = formatDateTime(new Date(), { year: 'numeric', month: 'short', day: '2-digit', weekday: 'short', hour: '2-digit', minute: '2-digit' }, timezone)

  return <div className="app-shell">
    <aside className={`sidebar ${mobileOpen ? 'sidebar-open' : ''}`}>
      <div className="sidebar-brand"><BrandMark /></div>
      <div className="workspace-label"><span className="workspace-indicator" />本地工作区</div>
      <nav aria-label="主导航" className="primary-nav">
        <div className="nav-caption">工作区</div>
        {primary.map((item) => <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`} onClick={() => setMobileOpen(false)}><Icon name={item.icon} /><span>{item.label}</span>{item.to === '/usage' && <span className="nav-trailing">AI</span>}</NavLink>)}
      </nav>
      <nav aria-label="管理导航" className={`management-nav ${inSettings ? 'management-expanded' : ''}`}>
        <div className="nav-caption nav-caption-row"><span>管理</span><Icon name="chevron" size={14} /></div>
        {management.map((item) => <NavLink key={item.to} to={item.to} className={({ isActive }) => `nav-link nav-link-sub ${isActive ? 'active' : ''}`} onClick={() => setMobileOpen(false)}><Icon name={item.icon} /><span>{item.label}</span></NavLink>)}
      </nav>
      <div className="sidebar-spacer" />
      <div className="sidebar-footer">
        <div className="sidebar-health"><span className={`health-mark ${realtime === 'online' ? 'is-good' : realtime === 'offline' ? 'is-bad' : 'is-warn'}`} /><div><strong>实时数据</strong><small>{realtime === 'online' ? '事件流已连接' : realtime === 'delayed' ? '连接延迟 · 数据可能滞后' : realtime === 'offline' ? '连接断开 · 正在重试' : '正在连接'}</small></div></div>
        <div className="profile-row"><span className="avatar">{(session?.display_name || session?.username || 'U').slice(0, 1).toUpperCase()}</span><div><strong>{session?.display_name || session?.username || '本地用户'}</strong><small>单用户空间</small></div><button className="icon-button profile-action" aria-label="退出登录" onClick={() => void logout()}><Icon name="external" size={16} /></button></div>
      </div>
    </aside>
    {mobileOpen && <button className="mobile-scrim" onClick={() => setMobileOpen(false)} aria-label="关闭导航" />}
    <div className="app-main">
      <header className="topbar">
        <button className="icon-button mobile-menu-button" onClick={() => setMobileOpen((open) => !open)} aria-label="打开导航"><Icon name="menu" /></button>
        <div className="breadcrumb"><span>Camera AI</span><Icon name="chevron" size={14} /><strong>{title}</strong></div>
        <div className="topbar-meta"><span className="topbar-time"><Icon name="clock" size={15} />{clock}<small>{timezone}</small></span><StatusPill status={realtime === 'online' ? 'online' : realtime === 'offline' ? 'offline' : 'reconnecting'}>{realtime === 'online' ? '实时' : realtime === 'offline' ? '离线' : '同步中'}</StatusPill><button className="icon-button theme-toggle" aria-label={theme === 'light' ? '切换深色主题' : '切换浅色主题'} title={theme === 'light' ? '切换深色主题' : '切换浅色主题'} onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}><span aria-hidden="true">{theme === 'light' ? '◐' : '☼'}</span></button></div>
      </header>
      {realtime === 'delayed' || realtime === 'offline' ? <div className={`connection-banner ${realtime}`} role="status"><Icon name="warning" size={16} /><span>{realtime === 'offline' ? '实时连接已断开，数据可能延迟。已保留当前显示内容，并将尝试重新连接。' : '实时连接已断开超过 5 秒，当前数据可能延迟。'}</span></div> : null}
      <main className="main-content" key={location.pathname}><Outlet /></main>
      <nav className="mobile-bottom-nav" aria-label="移动端主导航">
        {primary.slice(0, 4).map((item) => <NavLink key={item.to} to={item.to} end={item.end}><Icon name={item.icon} size={19} /><span>{item.label}</span></NavLink>)}
        <NavLink to="/settings/cameras"><Icon name="settings" size={19} /><span>管理</span></NavLink>
      </nav>
    </div>
  </div>
}

function NotFound() {
  return <div className="not-found"><div className="eyebrow">404 · 未找到页面</div><h1>这个工作区暂不存在。</h1><p>返回真实数据对应的工作台入口。</p><Link to="/" className="button button-primary">回到工作台</Link></div>
}
