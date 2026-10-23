export const cn = (...values: Array<string | false | null | undefined>) => values.filter(Boolean).join(' ')

export const DEFAULT_TIMEZONE = 'Asia/Shanghai'
type ZonedParts = { year: number; month: number; day: number; hour: number; minute: number; second: number }

const zonedParts = (date: Date, timezone: string): ZonedParts => {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(date)
  const value = (type: string) => Number(parts.find((part) => part.type === type)?.value ?? 0)
  return { year: value('year'), month: value('month'), day: value('day'), hour: value('hour'), minute: value('minute'), second: value('second') }
}

const utcMillisFromParts = ({ year, month, day, hour, minute, second }: ZonedParts) => {
  const date = new Date(0)
  date.setUTCFullYear(year, month - 1, day)
  date.setUTCHours(hour, minute, second, 0)
  return date.getTime()
}

const validDate = (value?: string | Date | null) => {
  if (!value) return undefined
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? undefined : date
}

export const formatDateTime = (value?: string | Date | null, options?: Intl.DateTimeFormatOptions, timezone = DEFAULT_TIMEZONE) => {
  const date = validDate(value)
  if (!date) return '—'
  const formatOptions: Intl.DateTimeFormatOptions = options && ('dateStyle' in options || 'timeStyle' in options)
    ? { ...options, timeZone: timezone }
    : { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', ...options, timeZone: timezone }
  return new Intl.DateTimeFormat('zh-CN', formatOptions).format(date)
}

export const formatTime = (value?: string | Date | null, withSeconds = false, timezone = DEFAULT_TIMEZONE) => {
  const date = validDate(value)
  if (!date) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: timezone, hour: '2-digit', minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' } : {}),
  }).format(date)
}

export const formatDateOnly = (value?: string | Date | null, timezone = DEFAULT_TIMEZONE) =>
  formatDateTime(value, { year: 'numeric', month: '2-digit', day: '2-digit' }, timezone)

export const formatRelative = (value?: string | Date | null, now = Date.now(), timezone = DEFAULT_TIMEZONE) => {
  const date = validDate(value)
  if (!date) return '无记录'
  const seconds = Math.round((now - date.getTime()) / 1000)
  if (seconds < 5) return '刚刚'
  if (seconds < 60) return `${seconds} 秒前`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return formatDateTime(date, undefined, timezone)
}

export const formatDuration = (ms?: number | null) => {
  if (ms == null || Number.isNaN(ms)) return '—'
  const seconds = Math.max(0, Math.round(ms / 1000))
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  if (minutes < 60) return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const minuteRemainder = minutes % 60
  return minuteRemainder ? `${hours} 小时 ${minuteRemainder} 分` : `${hours} 小时`
}

export const formatPercent = (value?: number | null) => value == null || !Number.isFinite(value) ? '未知' : `${Math.round(value * 100)}%`
export const formatBytes = (bytes?: number | null) => {
  if (bytes == null || Number.isNaN(bytes)) return '未知'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`
}

export const statusLabel = (status?: string | null) => {
  const labels: Record<string, string> = {
    online: '在线', offline: '离线', reconnecting: '重连中', starting: '启动中', error: '异常',
    final: '已整理', provisional: '第一层临时观察', unknown: '未知 / 证据不足', failed: '分析失败', processing: '处理中',
    queued: '排队中', leased: '已领取', running: '处理中', retry_wait: '等待重试', succeeded: '已完成', needs_review: '待复核', evidence_expired: '证据已过期',
    stale: '待更新', generated: '已生成', complete: '已生成', updating: '正在更新', incomplete: '数据不完整', pending: '等待结果',
  }
  return status ? labels[status] ?? status : '未知状态'
}

export const categoryTone = (category?: string | null) => {
  const tones: Record<string, string> = { 学习: 'learning', 工作: 'work', 生活: 'life', 娱乐: 'play', 休息: 'rest' }
  return tones[category ?? ''] ?? 'neutral'
}

export const toIso = (value: string) => {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toISOString()
}

/** 将 UTC/API 时间转换为所选 IANA 时区下的 datetime-local 字符串。 */
export const toLocalInput = (value?: string | null, timezone = DEFAULT_TIMEZONE) => {
  const date = validDate(value)
  if (!date) return ''
  const parts = zonedParts(date, timezone)
  return `${String(parts.year).padStart(4, '0')}-${String(parts.month).padStart(2, '0')}-${String(parts.day).padStart(2, '0')}T${String(parts.hour).padStart(2, '0')}:${String(parts.minute).padStart(2, '0')}`
}

const timeZoneOffsetMs = (date: Date, timezone: string) => utcMillisFromParts(zonedParts(date, timezone)) - date.getTime()

/** 将 datetime-local 按所选时区解析为 UTC；DST 跳时中不存在的时间返回 undefined。 */
export const fromLocalInput = (value: string, timezone = DEFAULT_TIMEZONE) => {
  const match = /^(\d{4,6})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value)
  if (!match) return undefined
  const [, yearText, monthText, dayText, hourText, minuteText, secondText] = match
  const parts: ZonedParts = { year: Number(yearText), month: Number(monthText), day: Number(dayText), hour: Number(hourText), minute: Number(minuteText), second: Number(secondText ?? 0) }
  if (parts.month < 1 || parts.month > 12 || parts.day < 1 || parts.day > 31 || parts.hour > 23 || parts.minute > 59 || parts.second > 59) return undefined
  const naive = utcMillisFromParts(parts)
  const normalizedDate = new Date(naive)
  const normalized: ZonedParts = { year: normalizedDate.getUTCFullYear(), month: normalizedDate.getUTCMonth() + 1, day: normalizedDate.getUTCDate(), hour: normalizedDate.getUTCHours(), minute: normalizedDate.getUTCMinutes(), second: normalizedDate.getUTCSeconds() }
  if (utcMillisFromParts(normalized) !== naive) return undefined
  let candidate = naive
  for (let index = 0; index < 5; index += 1) {
    const next = naive - timeZoneOffsetMs(new Date(candidate), timezone)
    if (next === candidate) break
    candidate = next
  }
  const represented = zonedParts(new Date(candidate), timezone)
  if (represented.year !== parts.year || represented.month !== parts.month || represented.day !== parts.day || represented.hour !== parts.hour || represented.minute !== parts.minute || represented.second !== parts.second) return undefined
  return new Date(candidate).toISOString()
}

export const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))

export const shortId = (value?: string | null) => value ? `${value.slice(0, 8)}…` : '—'

export const isWithin = (value: string, start: number, end: number) => {
  const time = new Date(value).getTime()
  return time >= start && time <= end
}

export const unique = <T,>(values: T[]) => Array.from(new Set(values))
