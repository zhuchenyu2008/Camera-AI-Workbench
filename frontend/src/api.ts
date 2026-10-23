import type { ActivityCatalogItem, AnalysisSettings, Binding, Camera, EvidenceFrame, Job, Overview, Person, Provider, RetentionSettings, Session, Summary, TimelineData, TimelineSegment, Usage } from './types'

const API_BASE = '/api/v1'
let csrfToken: string | null = null

export class ApiError extends Error {
  status: number
  code?: string
  requestId?: string
  details?: Record<string, unknown>
  retryable = false

  constructor(message: string, status: number, body?: any) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = body?.error?.code
    this.requestId = body?.error?.request_id
    this.details = body?.error?.details
    this.retryable = Boolean(body?.error?.retryable)
  }
}

type RequestOptions = {
  method?: string
  body?: unknown
  headers?: Record<string, string>
  signal?: AbortSignal
  raw?: boolean
  envelope?: boolean
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (csrfToken && options.method && !['GET', 'HEAD', 'OPTIONS'].includes(options.method.toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken
  }

  let response: Response
  try {
    response = await fetch(path.startsWith('/health/') ? path : `${API_BASE}${path}`, {
      method: options.method ?? 'GET',
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError('无法连接到工作台服务，请检查后端是否正在运行。', 0)
  }

  if (options.raw) {
    if (!response.ok) {
      const body = await safeJson(response)
      throw new ApiError(errorMessage(response.status, body), response.status, body)
    }
    return response as T
  }

  const body = response.status === 204 ? null : await safeJson(response)
  if (!response.ok) throw new ApiError(errorMessage(response.status, body), response.status, body)
  return (options.envelope ? body : body?.data ?? body) as T
}

async function safeJson(response: Response): Promise<any> {
  try { return await response.json() } catch { return null }
}

function errorMessage(status: number, body: any) {
  if (status === 401) return '登录状态已失效，请重新登录。'
  if (status === 403) return '当前会话无权执行此操作。'
  if (status === 409 || status === 412) return body?.error?.message || '记录已被其他操作更新，请检查最新版本后再保存。'
  if (status === 410) return '证据已按留存策略过期。'
  if (status === 429) return '请求过于频繁，请稍后再试。'
  if (status >= 500) return '服务暂时无法完成请求，请稍后重试。'
  return body?.error?.message || '请求未能完成，请检查输入后重试。'
}

export const api = {
  setCsrf(token?: string | null) { csrfToken = token ?? null },
  get: <T,>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
  getEnvelope: <T,>(path: string, signal?: AbortSignal) => request<{ data: T; meta?: Record<string, any> }>(path, { signal, envelope: true }),
  post: <T,>(path: string, body?: unknown, headers?: Record<string, string>) => request<T>(path, { method: 'POST', body, headers }),
  put: <T,>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  patch: <T,>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  delete: <T,>(path: string) => request<T>(path, { method: 'DELETE' }),
  raw: (path: string) => request<Response>(path, { raw: true }),
}

export const idempotencyHeaders = () => ({ 'Idempotency-Key': crypto.randomUUID() })
export const query = (values: Record<string, string | number | boolean | null | undefined>) => {
  const params = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  })
  return params.toString()
}

export const getSession = () => api.get<Session>('/session')
export const loadSession = getSession
export const login = (username: string, password: string) => api.post<Session>('/session', { username, password })
export const logout = () => api.delete<{ logged_out: boolean }>('/session')
export const getOverview = () => api.get<Overview>('/overview')
export const getCameras = () => api.get<Camera[]>('/cameras')
export const getCamera = (id: string) => api.get<Camera>(`/cameras/${encodeURIComponent(id)}`)
export const getCameraHealth = (id: string) => api.get<any>(`/cameras/${encodeURIComponent(id)}/health`)

export const timelinePath = (values: { camera_id?: string; from: string; to: string; include_observations?: boolean; limit?: number; cursor?: string }) =>
  `/timeline?${query(values)}`
export const getTimeline = (values: { camera_id?: string; from: string; to: string; include_observations?: boolean; limit?: number; cursor?: string }) =>
  api.getEnvelope<TimelineData>(timelinePath(values)).then((result) => ({ data: result.data, meta: result.meta ?? {} }))
export const getSegment = (id: string) => api.get<TimelineSegment & { evidence_frames: EvidenceFrame[]; revision_history: Array<Record<string, unknown>> }>(`/segments/${encodeURIComponent(id)}`)
export const getSummaries = (values: { camera_id?: string; period_kind?: string; from?: string; to?: string }) => api.get<Summary[]>(`/summaries?${query(values)}`)
export const getJobs = (values: { state?: string; layer?: string; camera_id?: string; limit?: number; cursor?: string } = {}) => api.get<Job[]>(`/jobs?${query(values)}`)
export const getUsage = (values: { from: string; to: string; camera_id?: string; provider_id?: string; layer?: string; group_by?: string; limit?: number }) => api.get<Usage>(`/ai/usage?${query(values)}`)
export const getProviders = () => api.get<Provider[]>('/ai/providers')
export const getBindings = () => api.get<Binding[]>('/ai/bindings')
export const getActivities = () => api.get<ActivityCatalogItem[]>('/activity-catalog')
export const getPersons = () => api.get<Person[]>('/persons')
export const getRetention = () => api.get<RetentionSettings>('/settings/retention')
export const getAnalysis = () => api.get<AnalysisSettings>('/settings/analysis')
export const getReadiness = () => api.get<any>('/health/ready')
export const getDependencies = () => api.get<any>('/health/dependencies')

export const updateSegment = (id: string, body: Record<string, unknown>) => api.patch<TimelineSegment>(`/segments/${encodeURIComponent(id)}`, body)
export const splitSegment = (id: string, body: Record<string, unknown>) => api.post<{ segments: TimelineSegment[] }>(`/segments/${encodeURIComponent(id)}/split`, body)
export const mergeSegments = (body: Record<string, unknown>) => api.post<TimelineSegment>('/segments/merge', body)
export const toggleFavorite = (id: string, favorite: boolean) => api.post<EvidenceFrame>(`/frames/${encodeURIComponent(id)}/favorite`, { favorite })
export const retryJob = (id: string) => api.post<Job>(`/jobs/${encodeURIComponent(id)}/retry`)
export const regenerateSummary = (id: string) => api.post<Job>(`/summaries/${encodeURIComponent(id)}/regenerate`, { reason: '用户请求重新汇总现有活动记录', force: false })
