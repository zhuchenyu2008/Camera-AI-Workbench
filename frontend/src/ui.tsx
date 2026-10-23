import { useEffect, type ButtonHTMLAttributes, type ReactNode } from 'react'
import { cn, statusLabel } from './utils'

const paths: Record<string, ReactNode> = {
  home: <><path d="m3 10 9-7 9 7" /><path d="M5 9v11h14V9M9 20v-7h6v7" /></>,
  timeline: <><path d="M3 7h18M3 12h18M3 17h18" /><path d="M8 5v4M15 10v4M11 15v4" /></>,
  summary: <><path d="M4 19V5M4 19h17" /><path d="m7 15 4-4 3 2 6-7" /></>,
  usage: <><rect x="4" y="4" width="16" height="16" rx="2" /><path d="M8 9h8M8 13h3M8 16h8" /></>,
  settings: <><path d="M12 8.2a3.8 3.8 0 1 0 0 7.6 3.8 3.8 0 0 0 0-7.6Z" /><path d="m19.4 15 .1.1a1.8 1.8 0 0 1-2.5 2.5l-.1-.1a1.8 1.8 0 0 0-3 .9v.2a1.8 1.8 0 0 1-3.6 0v-.2a1.8 1.8 0 0 0-3-.9l-.1.1a1.8 1.8 0 0 1-2.5-2.5l.1-.1a1.8 1.8 0 0 0-.9-3H3.7a1.8 1.8 0 0 1 0-3.6h.2a1.8 1.8 0 0 0 .9-3l-.1-.1a1.8 1.8 0 0 1 2.5-2.5l.1.1a1.8 1.8 0 0 0 3-.9v-.2a1.8 1.8 0 0 1 3.6 0v.2a1.8 1.8 0 0 0 3 .9l.1-.1a1.8 1.8 0 0 1 2.5 2.5l-.1.1a1.8 1.8 0 0 0 .9 3h.2a1.8 1.8 0 0 1 0 3.6h-.2a1.8 1.8 0 0 0-.9 3Z" /></>,
  camera: <><rect x="3" y="6" width="18" height="14" rx="2" /><circle cx="12" cy="13" r="3" /><path d="M7 6l1.5-2h7L17 6" /></>,
  activity: <><path d="M3 12h4l3-8 4 16 3-8h4" /></>,
  ai: <><path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z" /><path d="m19 15 .9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15Z" /></>,
  database: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" /></>,
  users: <><circle cx="9" cy="8" r="3" /><path d="M3 20c0-3.1 2.7-5 6-5s6 1.9 6 5M16 5.5a3 3 0 0 1 0 5.8M18 15c1.8.6 3 2.1 3 4" /></>,
  system: <><rect x="4" y="4" width="16" height="13" rx="2" /><path d="M8 21h8m-4-4v4M8 9h8M8 12h5" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  chevron: <path d="m9 18 6-6-6-6" />,
  left: <path d="m15 18-6-6 6-6" />,
  right: <path d="m9 18 6-6-6-6" />,
  plus: <path d="M12 5v14M5 12h14" />,
  search: <><circle cx="10.8" cy="10.8" r="6.8" /><path d="m16 16 4 4" /></>,
  refresh: <><path d="M20 7v5h-5" /><path d="M4.8 9A7.5 7.5 0 0 1 18 6l2 6M4 17v-5h5" /><path d="M19.2 15A7.5 7.5 0 0 1 6 18l-2-6" /></>,
  close: <path d="m6 6 12 12M18 6 6 18" />,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  lock: <><rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 1 1 8 0v3" /></>,
  unlock: <><rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0" /></>,
  image: <><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="8.5" cy="9" r="1.5" /><path d="m21 15-5-5L5 20" /></>,
  warning: <><path d="M10.3 4.4 2.8 18a2 2 0 0 0 1.7 3h15a2 2 0 0 0 1.7-3L13.7 4.4a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4m0 3h.01" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  external: <><path d="M14 4h6v6M20 4l-9 9" /><path d="M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6" /></>,
  filter: <><path d="M4 6h16M7 12h10m-7 6h4" /><circle cx="8" cy="6" r="1" fill="currentColor" /><circle cx="15" cy="12" r="1" fill="currentColor" /></>,
  download: <><path d="M12 3v12m-5-5 5 5 5-5" /><path d="M5 19h14" /></>,
  more: <><circle cx="5" cy="12" r="1" fill="currentColor" /><circle cx="12" cy="12" r="1" fill="currentColor" /><circle cx="19" cy="12" r="1" fill="currentColor" /></>,
}

export function Icon({ name, size = 18, className }: { name: string; size?: number; className?: string }) {
  return <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.activity}</svg>
}

export function BrandMark({ compact = false }: { compact?: boolean }) {
  return <span className={cn('brand-lockup', compact && 'brand-lockup-compact')}><span className="brand-mark">CW</span>{!compact && <span className="brand-wordmark"><strong>Camera AI</strong><small>Workbench</small></span>}</span>
}

export function StatusPill({ status, children, className }: { status?: string | null; children?: ReactNode; className?: string }) {
  const value = status ?? 'unknown'
  return <span className={cn('status-pill', `status-${value.replace(/[^a-z0-9_-]/gi, '-')}`, className)}><span className="status-dot" />{children ?? statusLabel(value)}</span>
}

export function PageHeading({ eyebrow, title, description, actions, aside }: { eyebrow?: string; title: string; description?: string; actions?: ReactNode; aside?: ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1>{description && <p>{description}</p>}</div><div className="page-heading-actions">{aside}{actions}</div></div>
}

export function SectionHeading({ title, detail, action, level = 2 }: { title: string; detail?: string; action?: ReactNode; level?: 2 | 3 }) {
  const Tag = level === 2 ? 'h2' : 'h3'
  return <div className="section-heading"><div><Tag>{title}</Tag>{detail && <p>{detail}</p>}</div>{action}</div>
}

export function Button({ variant = 'secondary', size = 'md', className, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'quiet' | 'danger'; size?: 'sm' | 'md' }) {
  return <button className={cn('button', `button-${variant}`, `button-${size}`, className)} {...props}>{children}</button>
}

export function EmptyState({ icon = 'activity', title, detail, action }: { icon?: string; title: string; detail?: string; action?: ReactNode }) {
  return <div className="empty-state"><span className="empty-icon"><Icon name={icon} size={22} /></span><h3>{title}</h3>{detail && <p>{detail}</p>}{action}</div>
}

export function ErrorNotice({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : '请求未完成。'
  const requestId = typeof error === 'object' && error && 'requestId' in error ? String(error.requestId ?? '') : ''
  return <div className="error-notice" role="alert"><Icon name="warning" /><div><strong>数据暂不可用</strong><p>{message}</p>{requestId && <small>请求编号 {requestId}</small>}</div>{onRetry && <Button size="sm" onClick={onRetry}>重试</Button>}</div>
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return <div className="skeleton-stack" aria-label="正在加载">{Array.from({ length: rows }, (_, index) => <span className="skeleton-line" key={index} />)}</div>
}

export function Modal({ open, title, children, onClose, wide = false }: { open: boolean; title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  useEffect(() => {
    if (!open) return
    const before = document.activeElement as HTMLElement | null
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    document.body.classList.add('modal-open')
    return () => { document.removeEventListener('keydown', onKey); document.body.classList.remove('modal-open'); before?.focus() }
  }, [open, onClose])
  if (!open) return null
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><section className={cn('modal', wide && 'modal-wide')} role="dialog" aria-modal="true" aria-labelledby="modal-title"><header><h2 id="modal-title">{title}</h2><button className="icon-button" onClick={onClose} aria-label="关闭"><Icon name="close" /></button></header><div className="modal-body">{children}</div></section></div>
}

export function Drawer({ open, title, children, onClose }: { open: boolean; title: string; children: ReactNode; onClose: () => void }) {
  useEffect(() => {
    if (!open) return
    const before = document.activeElement as HTMLElement | null
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    document.body.classList.add('modal-open')
    return () => { document.removeEventListener('keydown', onKey); document.body.classList.remove('modal-open'); before?.focus() }
  }, [open, onClose])
  if (!open) return null
  return <div className="drawer-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><aside className="detail-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title"><header className="drawer-header"><div><div className="eyebrow">活动记录</div><h2 id="drawer-title">{title}</h2></div><button className="icon-button" onClick={onClose} aria-label="关闭详情"><Icon name="close" /></button></header><div className="drawer-content">{children}</div></aside></div>
}

export function Field({ label, hint, error, children, className }: { label: string; hint?: string; error?: string; children: ReactNode; className?: string }) {
  return <label className={cn('field', className)}><span>{label}</span>{children}{hint && <small>{hint}</small>}{error && <small className="field-error">{error}</small>}</label>
}

export function SelectField({ label, value, options, onChange, hint, className }: { label: string; value: string; options: Array<{ value: string; label: string }>; onChange: (value: string) => void; hint?: string; className?: string }) {
  return <Field label={label} hint={hint} className={className}><select value={value} onChange={(event) => onChange(event.target.value)}>{options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></Field>
}

export function DataState({ isLoading, error, onRetry, children }: { isLoading: boolean; error?: unknown; onRetry?: () => void; children: ReactNode }) {
  if (isLoading) return <Skeleton />
  if (error) return <ErrorNotice error={error} onRetry={onRetry} />
  return <>{children}</>
}

export function Notice({ tone = 'info', icon = 'activity', title, children }: { tone?: 'info' | 'warning' | 'danger' | 'success'; icon?: string; title?: string; children: ReactNode }) {
  return <div className={cn('notice', `notice-${tone}`)}><Icon name={icon} /><div>{title && <strong>{title}</strong>}<div>{children}</div></div></div>
}

export function TableEmptyRow({ columns, children }: { columns: number; children: ReactNode }) {
  return <tr><td colSpan={columns}><div className="table-empty">{children}</div></td></tr>
}
