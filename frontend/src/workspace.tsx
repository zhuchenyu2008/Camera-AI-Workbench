import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, getSession, login } from './api'
import type { Session, ThemeMode } from './types'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      retry: (count, error) => count < 1 && !(error as { status?: number })?.status,
    },
    mutations: { retry: false },
  },
})

type WorkspaceState = {
  session: Session | null
  theme: ThemeMode
  setTheme: (theme: ThemeMode) => void
  timezone: string
  setTimezone: (timezone: string) => void
  realtime: 'connecting' | 'online' | 'delayed' | 'offline'
  logout: () => Promise<void>
  refreshSession: () => void
}

const WorkspaceContext = createContext<WorkspaceState | null>(null)

export function useWorkspace() {
  const value = useContext(WorkspaceContext)
  if (!value) throw new Error('WorkspaceProvider is missing')
  return value
}

export function AppProviders({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}><SessionGate>{children}</SessionGate></QueryClientProvider>
}

function SessionGate({ children }: { children: ReactNode }) {
  const sessionQuery = useQuery({ queryKey: ['session'], queryFn: getSession, retry: false, staleTime: Infinity })
  const [sessionOverride, setSessionOverride] = useState<Session | null>(null)
  const [theme, setThemeState] = useState<ThemeMode>(() => {
    const stored = localStorage.getItem('camera-workbench-theme')
    if (stored === 'light' || stored === 'dark') return stored
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  })
  const [timezone, setTimezoneState] = useState(() => localStorage.getItem('camera-workbench-timezone') || 'Asia/Shanghai')
  const [realtime, setRealtime] = useState<WorkspaceState['realtime']>('connecting')
  const queryClient = useQueryClient()

  const session = sessionOverride ?? sessionQuery.data ?? null
  useEffect(() => api.setCsrf(session?.csrf_token), [session?.csrf_token])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    document.documentElement.style.colorScheme = theme
    localStorage.setItem('camera-workbench-theme', theme)
  }, [theme])

  useEffect(() => localStorage.setItem('camera-workbench-timezone', timezone), [timezone])

  useEffect(() => {
    if (!session) return
    setRealtime('connecting')
    const source = new EventSource('/api/v1/events', { withCredentials: true })
    let connected = false
    let disconnectedAt: number | null = null
    let timeout: number | undefined
    let pollTimer: number | undefined
    const invalidateFor = (name: string) => {
      if (name === 'camera.status' || name === 'frame.latest') {
        void queryClient.invalidateQueries({ queryKey: ['overview'] })
        void queryClient.invalidateQueries({ queryKey: ['cameras'] })
        void queryClient.invalidateQueries({ queryKey: ['camera-latest-frame'] })
      }
      if (name === 'job.updated') {
        void queryClient.invalidateQueries({ queryKey: ['jobs'] })
        void queryClient.invalidateQueries({ queryKey: ['overview'] })
      }
      if (name === 'timeline.updated') {
        void queryClient.invalidateQueries({ queryKey: ['timeline'] })
        void queryClient.invalidateQueries({ queryKey: ['segments'] })
        void queryClient.invalidateQueries({ queryKey: ['summaries'] })
      }
      if (name === 'summary.updated') void queryClient.invalidateQueries({ queryKey: ['summaries'] })
      if (name === 'storage.updated') {
        void queryClient.invalidateQueries({ queryKey: ['overview'] })
        void queryClient.invalidateQueries({ queryKey: ['segment'] })
      }
    }
    const eventNames = ['camera.status', 'frame.latest', 'job.updated', 'timeline.updated', 'summary.updated', 'storage.updated']
    const listeners = eventNames.map((name) => {
      const listener = () => invalidateFor(name)
      source.addEventListener(name, listener)
      return [name, listener] as const
    })
    source.onopen = () => {
      connected = true
      disconnectedAt = null
      setRealtime('online')
      void queryClient.invalidateQueries({ queryKey: ['overview'] })
      void queryClient.invalidateQueries({ queryKey: ['cameras'] })
      void queryClient.invalidateQueries({ queryKey: ['timeline'] })
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
      if (pollTimer) window.clearInterval(pollTimer)
    }
    source.onerror = () => {
      connected = false
      disconnectedAt ??= Date.now()
      if (timeout) window.clearTimeout(timeout)
      timeout = window.setTimeout(() => setRealtime(Date.now() - (disconnectedAt ?? Date.now()) > 30_000 ? 'offline' : 'delayed'), 5_000)
      if (!pollTimer) {
        let interval = 30_000
        pollTimer = window.setInterval(() => {
          if (connected) return
          void queryClient.invalidateQueries({ queryKey: ['overview'] })
          void queryClient.invalidateQueries({ queryKey: ['cameras'] })
          void queryClient.invalidateQueries({ queryKey: ['camera-latest-frame'] })
          void queryClient.invalidateQueries({ queryKey: ['timeline'] })
          void queryClient.invalidateQueries({ queryKey: ['jobs'] })
          interval = Math.min(interval * 2, 300_000)
        }, interval)
      }
    }
    return () => {
      source.close()
      listeners.forEach(([name, listener]) => source.removeEventListener(name, listener))
      if (timeout) window.clearTimeout(timeout)
      if (pollTimer) window.clearInterval(pollTimer)
    }
  }, [queryClient, session?.username])

  const value = useMemo<WorkspaceState>(() => ({
    session,
    theme,
    setTheme: setThemeState,
    timezone,
    setTimezone: setTimezoneState,
    realtime,
    logout: async () => {
      await api.delete('/session')
      api.setCsrf(null)
      setSessionOverride(null)
      await queryClient.invalidateQueries({ queryKey: ['session'] })
      queryClient.clear()
    },
    refreshSession: () => { setSessionOverride(null); void queryClient.invalidateQueries({ queryKey: ['session'] }) },
  }), [session, theme, timezone, realtime, queryClient])

  if (sessionQuery.isPending && !sessionOverride) return <div className="boot-screen"><div className="brand-mark">CW</div><span>连接工作台…</span></div>
  const error = sessionQuery.error as { status?: number } | null
  if (!session && !sessionOverride && error?.status === 401) {
    return <WorkspaceContext.Provider value={value}><LoginScreen onLogin={(next) => { api.setCsrf(next.csrf_token); setSessionOverride(next); queryClient.setQueryData(['session'], next) }} /></WorkspaceContext.Provider>
  }
  if (!session && !sessionOverride && sessionQuery.isError) {
    return <WorkspaceContext.Provider value={value}><ConnectionScreen onRetry={() => void sessionQuery.refetch()} /></WorkspaceContext.Provider>
  }
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
}

function LoginScreen({ onLogin }: { onLogin: (session: Session) => void }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true); setError('')
    try { onLogin(await login(username.trim(), password)) }
    catch (cause) { setError(cause instanceof Error ? cause.message : '登录失败，请重试。') }
    finally { setBusy(false) }
  }
  return <main className="auth-screen"><div className="auth-frame">
    <div className="auth-brand"><div className="brand-mark">CW</div><span>CAMERA AI WORKBENCH</span></div>
    <div className="auth-copy"><div className="eyebrow">本地观测 · 私有数据</div><h1>让每一段记录，<br />都有来处。</h1><p>通过采集状态、活动时间轴与原始证据，了解系统记录到了什么。</p></div>
    <form className="auth-form" onSubmit={(event) => void submit(event)}>
      <label className="field"><span>用户名</span><input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required /></label>
      <label className="field"><span>密码</span><input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
      {error && <div className="inline-error" role="alert">{error}</div>}
      <button className="button button-primary button-full" disabled={busy}>{busy ? '正在验证…' : '登录工作台'}<span aria-hidden="true">↗</span></button>
      <p className="auth-footnote">凭据仅发送至当前工作台服务，不会保存在浏览器中。</p>
    </form>
  </div><div className="auth-side-note">LOCAL-FIRST · EVIDENCE-LED</div></main>
}

function ConnectionScreen({ onRetry }: { onRetry: () => void }) {
  return <main className="connection-screen"><div className="brand-mark">CW</div><h1>工作台服务暂不可用</h1><p>无法取得会话信息。请确认 API 服务已经启动，再重新连接。</p><button className="button button-primary" onClick={onRetry}>重新连接</button></main>
}

export function useAppQueryClient() { return useQueryClient() }
