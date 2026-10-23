import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getActivities, getCameras, getSegment, getTimeline, mergeSegments, splitSegment, toggleFavorite, updateSegment } from './api'
import type { ActivityCatalogItem, Camera, EvidenceFrame, Job, Observation, TimelineSegment } from './types'
import { categoryTone, clamp, formatDateTime, formatDuration, formatPercent, formatTime, fromLocalInput, shortId, statusLabel, toLocalInput } from './utils'
import { Button, DataState, Drawer, EmptyState, ErrorNotice, Field, Icon, Modal, Notice, PageHeading, SectionHeading, SelectField, StatusPill } from './ui'
import { EvidenceLegend } from './dashboard'
import { useWorkspace } from './workspace'
import { mapTimelineSegment } from './timeline-view-model'

type Preset = 'now' | '1h' | 'today' | 'yesterday' | 'custom'

function dayRange(timezone: string, dayOffset = 0) {
  const now = new Date()
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now)
  const read = (key: string) => Number(parts.find((part) => part.type === key)?.value ?? 0)
  const shifted = new Date(Date.UTC(read('year'), read('month') - 1, read('day') + dayOffset))
  const date = `${shifted.getUTCFullYear()}-${String(shifted.getUTCMonth() + 1).padStart(2, '0')}-${String(shifted.getUTCDate()).padStart(2, '0')}`
  const next = new Date(Date.UTC(shifted.getUTCFullYear(), shifted.getUTCMonth(), shifted.getUTCDate() + 1))
  const nextDate = `${next.getUTCFullYear()}-${String(next.getUTCMonth() + 1).padStart(2, '0')}-${String(next.getUTCDate()).padStart(2, '0')}`
  const atZone = (day: string, hour: string) => {
    const [year, month, dayNumber] = day.split('-').map(Number)
    const desired = Date.UTC(year, month - 1, dayNumber, Number(hour))
    let guess = desired
    for (let i = 0; i < 3; i += 1) {
      const current = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(guess))
      const value = (key: string) => Number(current.find((part) => part.type === key)?.value ?? 0)
      guess += desired - Date.UTC(value('year'), value('month') - 1, value('day'), value('hour'))
    }
    return new Date(guess).toISOString()
  }
  return { from: atZone(date, '0'), to: atZone(nextDate, '0') }
}
function fromPreset(preset: Preset, timezone: string) {
  const to = Date.now()
  if (preset === 'today') return dayRange(timezone)
  if (preset === 'yesterday') return dayRange(timezone, -1)
  const minutes = preset === 'now' ? 5 : 60
  return { from: new Date(to - minutes * 60_000).toISOString(), to: new Date(to).toISOString() }
}

export function TimelinePage() {
  const params = useParams()
  const [search, setSearch] = useSearchParams()
  const { timezone } = useWorkspace()
  const cameraParam = params.cameraId ?? search.get('cameraId') ?? ''
  const defaultPreset = search.get('range') === 'today' || search.get('range') === 'yesterday' ? search.get('range') as Preset : '1h'
  const initialRange = useMemo(() => fromPreset(defaultPreset, timezone), [defaultPreset, timezone])
  const from = search.get('from') || initialRange.from
  const to = search.get('to') || initialRange.to
  const includeObservations = search.get('observations') === '1'
  const selectedSegmentId = search.get('segment')
  const cameras = useQuery({ queryKey: ['cameras'], queryFn: getCameras })
  const activities = useQuery({ queryKey: ['activities'], queryFn: getActivities })
  const rangeMs = new Date(to).getTime() - new Date(from).getTime()
  const timeline = useInfiniteQuery({
    queryKey: ['timeline', { cameraParam, from, to, includeObservations }],
    queryFn: ({ pageParam }) => getTimeline({ camera_id: cameraParam || undefined, from, to, include_observations: includeObservations && rangeMs <= 86_400_000, limit: 200, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.meta.next_cursor ?? undefined,
  })
  const pages = timeline.data?.pages ?? []
  const segments = useMemo(() => pages.flatMap((page) => page.data.segments).map(mapTimelineSegment).filter((segment): segment is TimelineSegment => Boolean(segment)), [pages])
  const gaps = pages.flatMap((page) => page.data.camera_gaps)
  const windows = pages.flatMap((page) => page.data.window_states)
  const observations = pages.flatMap((page) => page.data.observations)
  const cameraList = (cameras.data ?? []).filter((camera) => !cameraParam || camera.id === cameraParam)
  const [stateFilter, setStateFilter] = useState('all')
  const [evidenceFilter, setEvidenceFilter] = useState('all')
  const [activityFilter, setActivityFilter] = useState('all')
  const [listMode, setListMode] = useState(false)
  const [notice, setNotice] = useState('')
  const [rangeError, setRangeError] = useState('')
  const queryClient = useQueryClient()
  const localFrom = toLocalInput(from, timezone)
  const localTo = toLocalInput(to, timezone)
  const latestRange = Math.abs(Date.now() - new Date(to).getTime()) < 30_000
  const setRange = (next: { from: string; to: string }, preset: Preset) => {
    setRangeError('')
    const value = new URLSearchParams(search)
    value.set('from', next.from); value.set('to', next.to); value.set('range', preset); value.delete('segment'); setSearch(value)
  }
  const setCamera = (id: string) => {
    const value = new URLSearchParams(search)
    if (id) value.set('cameraId', id); else value.delete('cameraId')
    value.delete('segment'); setSearch(value)
  }
  const openSegment = (segment: TimelineSegment) => { const value = new URLSearchParams(search); value.set('segment', segment.id); setSearch(value) }
  const closeSegment = () => { const value = new URLSearchParams(search); value.delete('segment'); setSearch(value) }
  const changeRangeInput = (key: 'from' | 'to', value: string) => {
    const next = fromLocalInput(value, timezone)
    if (!next) { setRangeError('此本地时间不存在（可能处于夏令时跳时区间），请改选有效时间。'); return }
    const candidate = { from: key === 'from' ? next : from, to: key === 'to' ? next : to }
    if (new Date(candidate.to).getTime() <= new Date(candidate.from).getTime()) { setRangeError('结束时间必须晚于开始时间。'); return }
    setRangeError(''); setRange(candidate, 'custom')
  }
  const invalidateTimeline = () => {
    void queryClient.invalidateQueries({ queryKey: ['timeline'] }); void queryClient.invalidateQueries({ queryKey: ['summaries'] }); void queryClient.invalidateQueries({ queryKey: ['segment'] })
  }
  const visibleSegments = segments.filter((segment) => {
    if (stateFilter !== 'all' && segment.state !== stateFilter) return false
    if (activityFilter !== 'all' && segment.category !== activityFilter && segment.canonical_activity_id !== activityFilter) return false
    if (evidenceFilter !== 'all' && !segment.intervals.some((interval) => interval.evidence_kind === evidenceFilter)) return false
    return true
  })
  const categories = Array.from(new Set(segments.map((segment) => segment.category).filter(Boolean))) as string[]
  const activityOptions = activities.data ?? []

  return <div className="page-stack timeline-page">
    <PageHeading eyebrow="活动记录 / 第二层时间轴" title="时间轴" description="以第二层整理结果为主；直接观察、推断补全、未知与采集缺口分别呈现。" aside={<div className="view-switch"><button className={!listMode ? 'selected' : ''} onClick={() => setListMode(false)}>时间线</button><button className={listMode ? 'selected' : ''} onClick={() => setListMode(true)}>可读列表</button></div>} />
    <div className="timeline-toolbar" aria-label="时间轴查询条件">
      <div className="toolbar-controls">
        <SelectField label="摄像头" value={cameraParam} onChange={setCamera} options={[{ value: '', label: '全部摄像头' }, ...(cameras.data ?? []).map((camera) => ({ value: camera.id, label: camera.name }))]} />
        <div className="range-inputs"><Field label="开始时间"><input type="datetime-local" value={localFrom} onChange={(event) => changeRangeInput('from', event.target.value)} /></Field><span className="range-arrow">→</span><Field label="结束时间"><input type="datetime-local" value={localTo} onChange={(event) => changeRangeInput('to', event.target.value)} /></Field></div>{rangeError && <div className="inline-error" role="alert">{rangeError}</div>}
        <div className="toolbar-presets" role="group" aria-label="快捷时间范围">{([['now', '现在'], ['1h', '最近 1 小时'], ['today', '今天'], ['yesterday', '昨天']] as Array<[Preset, string]>).map(([preset, label]) => <button key={preset} className={search.get('range') === preset ? 'selected' : ''} onClick={() => setRange(fromPreset(preset, timezone), preset)}>{label}</button>)}</div>
      </div>
      <div className="toolbar-filters">
        <SelectField label="状态" value={stateFilter} onChange={setStateFilter} options={[{ value: 'all', label: '全部状态' }, { value: 'final', label: '第二层已整理' }, { value: 'provisional', label: '第一层临时' }, { value: 'unknown', label: '未知 / 证据不足' }, { value: 'failed', label: '失败' }]} />
        <SelectField label="活动类别" value={activityFilter} onChange={setActivityFilter} options={[{ value: 'all', label: '全部活动' }, ...categories.map((category) => ({ value: category, label: category }))]} />
        <SelectField label="证据等级" value={evidenceFilter} onChange={setEvidenceFilter} options={[{ value: 'all', label: '全部证据' }, { value: 'direct', label: '直接观察' }, { value: 'inferred', label: '推断补全' }, { value: 'unknown', label: '未知' }]} />
        <label className="toggle-control"><input type="checkbox" checked={includeObservations} onChange={(event) => { const value = new URLSearchParams(search); if (event.target.checked) value.set('observations', '1'); else value.delete('observations'); setSearch(value) }} /><span className="toggle-ui" /><span>第一层分钟观察</span></label>
        {latestRange ? <span className="latest-state"><span className="health-mark is-good" />接近当前</span> : <Button size="sm" onClick={() => setRange(fromPreset('1h', timezone), '1h')}><Icon name="refresh" size={14} />回到最新</Button>}
      </div>
    </div>
    {notice && <div className="operation-note" role="status"><Icon name="check" size={15} />{notice}<button aria-label="关闭消息" onClick={() => setNotice('')}><Icon name="close" size={14} /></button></div>}
    <div className="timeline-workspace"><div className="timeline-main">
      <div className="timeline-panel-heading"><div><strong>{cameraParam ? cameraList[0]?.name ?? '所选摄像头' : `${cameraList.length} 路摄像头`}</strong><span>{formatDateTime(from, { year: 'numeric', month: 'short', day: '2-digit' }, timezone)} · {formatTime(from, false, timezone)} — {formatTime(to, false, timezone)}</span></div><span className="event-count">{visibleSegments.length} 个活动段</span></div>
      <DataState isLoading={timeline.isPending} error={timeline.error} onRetry={() => void timeline.refetch()}>
        {cameraList.length === 0 ? <EmptyState icon="camera" title="尚未配置摄像头" detail="配置摄像头并完成采集后，已生成的时间轴记录会出现在此处。" /> : <>
          {listMode ? <TimelineAccessibleList cameras={cameraList} segments={visibleSegments} gaps={gaps} observations={includeObservations ? observations : []} timezone={timezone} onSelect={openSegment} /> : <TimelineCanvas cameras={cameraList} segments={visibleSegments} gaps={gaps} windows={windows} observations={includeObservations ? observations : []} from={from} to={to} timezone={timezone} onSelect={openSegment} onShowJob={(camera) => { const value = new URLSearchParams(search); value.set('tab', 'jobs'); value.set('cameraId', camera.id); setSearch(value) }} />}
          {timeline.hasNextPage && <div className="load-more-row"><Button onClick={() => void timeline.fetchNextPage()} disabled={timeline.isFetchingNextPage}>{timeline.isFetchingNextPage ? '正在读取…' : '加载更多记录'}</Button></div>}
          {!visibleSegments.length && <EmptyState icon="timeline" title={segments.length ? '没有匹配当前筛选的活动段' : '所选区间尚无已生成的活动段'} detail={segments.length ? '调整活动类别、证据等级或状态筛选。' : '检查采集缺口和处理中任务；未知或未生成记录不等于休息。'} />}
        </>}
      </DataState>
      <EvidenceLegend />
    </div><aside className="timeline-aside">
      <div className="aside-block"><div className="eyebrow">时间范围</div><strong>{rangeMs > 86_400_000 ? '结构化记录模式' : '活动与证据模式'}</strong><p>{rangeMs > 86_400_000 ? '超过 24 小时时仅加载活动、缺口与窗口状态；缩小范围后可展开第一层分钟观察。' : '活动段来自第二层。分钟观察仅在展开后作为证据，不替代主时间轴。'}</p></div>
      <div className="aside-block"><div className="eyebrow">证据组成</div><EvidenceSummary segments={visibleSegments} /></div>
      <div className="aside-block"><div className="eyebrow">区间说明</div><p>所有区间按左闭右开处理。活动边界和时长来自服务端记录；浏览器不补算缺失时长。</p><p className="small-muted">显示时区：{timezone}</p></div>
    </aside></div>
    <SegmentDrawer key={selectedSegmentId ?? 'closed'} segment={segments.find((segment) => segment.id === selectedSegmentId)} segmentId={selectedSegmentId ?? undefined} siblingSegments={segments} activities={activityOptions} observations={observations} timezone={timezone} onClose={closeSegment} onChanged={(message) => { setNotice(message); invalidateTimeline() }} />
  </div>
}

function EvidenceSummary({ segments }: { segments: TimelineSegment[] }) {
  const counts = segments.flatMap((segment) => segment.intervals).reduce((result, interval) => { result[interval.evidence_kind] = (result[interval.evidence_kind] ?? 0) + 1; return result }, {} as Record<string, number>)
  return <div className="evidence-summary"><div><span>直接观察</span><strong>{counts.direct ?? 0}</strong></div><div><span>推断区间</span><strong>{counts.inferred ?? 0}</strong></div><div><span>证据不足</span><strong>{counts.unknown ?? 0}</strong></div></div>
}

function TimelineCanvas({ cameras, segments, gaps, windows, observations, from, to, timezone, onSelect, onShowJob }: { cameras: Camera[]; segments: TimelineSegment[]; gaps: any[]; windows: any[]; observations: Observation[]; from: string; to: string; timezone: string; onSelect: (segment: TimelineSegment) => void; onShowJob: (camera: Camera) => void }) {
  const rangeStart = new Date(from).getTime(); const rangeEnd = new Date(to).getTime(); const range = Math.max(1, rangeEnd - rangeStart)
  const tickCount = range < 12 * 60_000 ? 4 : range < 2 * 60 * 60_000 ? 6 : 8
  const ticks = Array.from({ length: tickCount + 1 }, (_, index) => rangeStart + (range * index) / tickCount)
  const place = (start: string, end?: string | null) => {
    const startTime = new Date(start).getTime(); const left = clamp((startTime - rangeStart) / range * 100, 0, 100)
    const endTime = end ? new Date(end).getTime() : rangeEnd
    const width = clamp((endTime - Math.max(rangeStart, startTime)) / range * 100, 0.25, Math.max(0.25, 100 - left))
    return { left: `${left}%`, width: `${width}%` }
  }
  return <div className="timeline-canvas-wrap" tabIndex={0} aria-label="活动时间轴。时间范围可使用快捷按钮调整；活动列表提供键盘等价操作。">
    <div className="timeline-axis-label">{ticks.map((tick, index) => <span key={index} style={{ left: `${index / tickCount * 100}%` }}>{formatTime(new Date(tick), range < 60 * 60_000, timezone)}</span>)}</div>
    <div className="camera-tracks">{cameras.map((camera) => {
      const rowSegments = segments.filter((segment) => segment.camera_id === camera.id)
      const rowGaps = gaps.filter((gap) => gap.camera_id === camera.id)
      const rowWindows = windows.filter((item) => item.camera_id === camera.id)
      const rowObservations = observations.filter((item) => item.camera_id === camera.id)
      return <div className="camera-track-group" key={camera.id}>
        <div className="camera-track-label"><div className="track-camera-icon"><Icon name="camera" size={15} /></div><div><strong>{camera.name}</strong><small>{camera.status === 'online' ? '采集在线' : `采集${statusLabel(camera.status)}`}</small></div><Link to="/settings/cameras" aria-label={`设置 ${camera.name}`}><Icon name="chevron" size={15} /></Link></div>
        <div className="track-content"><div className="track-line" />
          {rowGaps.map((gap) => <div key={gap.id} className="gap-band" style={place(gap.start, gap.end)} title={`采集缺口：${gap.reason || '期间没有可用画面'}`}><span>采集缺口</span></div>)}
          {rowSegments.map((segment) => <TimelineSegmentBlock key={segment.id} segment={segment} position={place(segment.start, segment.end)} timezone={timezone} onClick={() => onSelect(segment)} />)}
          {Date.now() >= rangeStart && Date.now() <= rangeEnd && <div className="track-now-marker" style={{ left: `${clamp((Date.now() - rangeStart) / range * 100, 0, 100)}%` }} aria-label="当前时间" />}
        </div>
        {rowWindows.length > 0 && <div className="processing-track"><span className="processing-track-label">L2 处理窗口</span><div className="processing-track-line">{rowWindows.map((item) => <button key={item.id} className={`processing-band ${item.state === 'failed' ? 'failed' : ''}`} style={place(item.start, item.end)} onClick={() => onShowJob(camera)} title={`${statusLabel(item.state)} · ${formatTime(item.start, false, timezone)}–${formatTime(item.end, false, timezone)}`}><span>{item.state === 'failed' ? '分析失败' : item.state === 'running' ? 'L2 处理中' : 'L2 等待中'}</span></button>)}</div></div>}
        {rowObservations.length > 0 && <div className="observation-track"><span className="observation-track-label">L1 分钟观察</span><div className="observation-track-line">{rowObservations.map((observation) => <button key={observation.id} className={`observation-chip state-${observation.state ?? 'unknown'}`} style={place(observation.start, observation.end)} title={`${formatTime(observation.start, false, timezone)} · ${statusLabel(observation.state)}`} onClick={() => document.getElementById(`observation-${observation.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })}><span /></button>)}</div></div>}
      </div>
    })}</div>
    {observations.length > 0 && <div className="minute-observations-list"><SectionHeading title="第一层分钟观察" detail="独立于第二层活动段的局部记录" level={3} />{observations.map((observation) => <article id={`observation-${observation.id}`} className="observation-list-item" key={observation.id}><StatusPill status={observation.state} /><span className="mono">{formatTime(observation.start, false, timezone)} – {formatTime(observation.end, false, timezone)}</span><p>{observationDescription(observation)}</p><span className="small-muted">来源 {observation.source ?? 'L1'} · 置信度 {formatPercent(observation.confidence)}</span></article>)}</div>}
  </div>
}

function TimelineSegmentBlock({ segment, position, timezone, onClick }: { segment: TimelineSegment; position: React.CSSProperties; timezone: string; onClick: () => void }) {
  const title = segment.canonical_activity || '未知 / 证据不足'
  const intervalText = segment.intervals.map((interval) => `${interval.evidence_kind === 'direct' ? '直接观察' : interval.evidence_kind === 'inferred' ? '推断补全' : '未知'} ${formatTime(interval.start, false, timezone)}–${formatTime(interval.end, false, timezone)}`).join('；')
  return <button className={`activity-segment tone-${categoryTone(segment.category)} state-${segment.state}`} style={position} onClick={onClick} aria-label={`${title}，${formatDateTime(segment.start, { hour: '2-digit', minute: '2-digit' }, timezone)} 至 ${formatTime(segment.end, true, timezone)}，${statusLabel(segment.state)}。按回车查看详情。`} title={`${title} · ${formatTime(segment.start, true, timezone)}–${formatTime(segment.end, true, timezone)} · ${statusLabel(segment.state)}${intervalText ? ` · ${intervalText}` : ''}`}>
    {segment.intervals.map((interval, index) => {
      const start = new Date(interval.start).getTime(); const end = new Date(interval.end).getTime(); const segStart = new Date(segment.start).getTime(); const segEnd = new Date(segment.end).getTime()
      const left = clamp((start - segStart) / Math.max(1, segEnd - segStart) * 100, 0, 100); const width = clamp((end - start) / Math.max(1, segEnd - segStart) * 100, 0.5, 100 - left)
      return <span key={interval.id ?? index} className={`segment-evidence segment-${interval.evidence_kind}`} style={{ left: `${left}%`, width: `${width}%` }} aria-hidden="true" />
    })}
    <span className="segment-label"><strong>{title}</strong><small>{segment.state === 'provisional' ? '第一层临时' : segment.state === 'final' ? '第二层已整理' : statusLabel(segment.state)}</small></span>
    {segment.locked && <span className="segment-lock" title="用户锁定"><Icon name="lock" size={12} /></span>}
  </button>
}
function TimelineAccessibleList({ cameras, segments, gaps, observations, timezone, onSelect }: { cameras: Camera[]; segments: TimelineSegment[]; gaps: any[]; observations: Observation[]; timezone: string; onSelect: (segment: TimelineSegment) => void }) {
  return <div className="accessible-timeline-list">{cameras.map((camera) => {
    const own = segments.filter((segment) => segment.camera_id === camera.id).sort((a, b) => a.start.localeCompare(b.start))
    const ownGaps = gaps.filter((gap) => gap.camera_id === camera.id)
    const ownObs = observations.filter((observation) => observation.camera_id === camera.id)
    if (!own.length && !ownGaps.length && !ownObs.length) return null
    return <section key={camera.id} className="accessible-camera-group"><h3>{camera.name}</h3>
      {own.map((segment) => <button key={segment.id} className="accessible-event" onClick={() => onSelect(segment)}><span className={`event-marker tone-${categoryTone(segment.category)} state-${segment.state}`} /><span className="event-time mono">{formatDateTime(segment.start, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }, timezone)} – {formatTime(segment.end, false, timezone)}</span><span className="event-title"><strong>{segment.canonical_activity ?? '未知 / 证据不足'}</strong><small>{segment.display_description || statusLabel(segment.state)}</small></span><StatusPill status={segment.state} /><span className="event-evidence">{segment.intervals.map((interval) => interval.evidence_kind).filter((kind, index, all) => all.indexOf(kind) === index).map((kind) => kind === 'direct' ? '直接观察' : kind === 'inferred' ? '含推断' : '未知').join(' · ') || '证据未返回'}</span></button>)}
      {ownGaps.map((gap: any) => <div key={gap.id} className="accessible-gap"><Icon name="warning" size={15} /><span>采集缺口 · {formatTime(gap.start, false, timezone)} – {gap.end ? formatTime(gap.end, false, timezone) : '持续中'}</span><small>{gap.reason || '期间没有可用画面'}</small></div>)}
      {ownObs.map((observation) => <div key={observation.id} className="accessible-observation"><Icon name="activity" size={15} /><span>第一层观察 · {formatTime(observation.start, false, timezone)} · {observationDescription(observation)}</span></div>)}
    </section>
  })}</div>
}

function observationDescription(observation: Observation) {
  const data = observation.observation as Record<string, unknown> | undefined
  if (data) for (const field of ['description', 'summary', 'text', 'activity', 'observation']) {
    const value = data[field]
    if (typeof value === 'string' && value) return value
  }
  return observation.state ? statusLabel(observation.state) : '观察内容未返回'
}

function SegmentDrawer({ segment, segmentId, siblingSegments, activities, observations, timezone, onClose, onChanged }: { segment?: TimelineSegment; segmentId?: string; siblingSegments: TimelineSegment[]; activities: ActivityCatalogItem[]; observations: Observation[]; timezone: string; onClose: () => void; onChanged: (message: string) => void }) {
  const queryClient = useQueryClient()
  const detailQuery = useQuery({ queryKey: ['segment', segmentId], queryFn: () => getSegment(segmentId!), enabled: Boolean(segmentId) })
  const detail = detailQuery.data
  const current = detail ?? segment
  const evidence = detail?.evidence_frames ?? []
  const linkedObservations = observations.filter((item) => current?.source_observation_ids?.includes(item.id))
  const [editing, setEditing] = useState(false)
  const [conflict, setConflict] = useState(false)
  const [serverVersion, setServerVersion] = useState<any>(null)
  const [viewerIndex, setViewerIndex] = useState<number | null>(null)
  const [imageMode, setImageMode] = useState<'thumbnail' | 'original'>('thumbnail')
  const [unavailableImages, setUnavailableImages] = useState<string[]>([])
  const [splitOpen, setSplitOpen] = useState(false)
  const [splitAt, setSplitAt] = useState('')
  const [mergeOpen, setMergeOpen] = useState(false)
  const [mergeIds, setMergeIds] = useState<string[]>([])
  const [error, setError] = useState('')
  const [form, setForm] = useState({ start: '', end: '', activityId: '', category: '', description: '', locked: false, reason: '' })
  const saveMutation = useMutation({
    mutationFn: () => updateSegment(current!.id, { expected_version: current!.version, start: fromLocalInput(form.start, timezone), end: fromLocalInput(form.end, timezone), canonical_activity_id: form.activityId || null, category: form.category || null, display_description: form.description || null, locked: form.locked, edit_reason: form.reason.trim() }),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ['segment', current!.id] }); setEditing(false); setConflict(false); onChanged('活动修订已保存；相关总结可能需要更新。') },
    onError: async (cause: any) => { setError(cause.message); if (cause.status === 409 || cause.status === 412) { setConflict(true); setServerVersion(await queryClient.fetchQuery({ queryKey: ['segment', current!.id], queryFn: () => getSegment(current!.id), staleTime: 0 }).catch(() => null)) } },
  })
  const splitMutation = useMutation({ mutationFn: () => splitSegment(current!.id, { split_at: fromLocalInput(splitAt, timezone), expected_version: current!.version, reason: '用户在活动详情中拆分' }), onSuccess: () => { setSplitOpen(false); onChanged('活动段已拆分为两个独立区间。') }, onError: (cause: any) => setError(cause.message) })
  const mergeMutation = useMutation({ mutationFn: () => mergeSegments({ segment_ids: mergeIds, expected_versions: Object.fromEntries(mergeIds.map((id) => [id, siblingSegments.find((item) => item.id === id)?.version ?? current!.version])), reason: '用户确认合并相邻活动段' }), onSuccess: () => { setMergeOpen(false); setMergeIds([]); onChanged('符合条件的活动段已合并。') }, onError: (cause: any) => setError(cause.message) })

  useEffect(() => { setEditing(false); setConflict(false); setServerVersion(null); setError(''); setUnavailableImages([]); setViewerIndex(null); setImageMode('thumbnail') }, [segmentId])
  useEffect(() => {
    if (!current) return
    setForm({ start: toLocalInput(current.start, timezone), end: toLocalInput(current.end, timezone), activityId: current.canonical_activity_id ?? '', category: current.category ?? '', description: current.display_description ?? '', locked: current.locked, reason: '' })
  }, [current?.id, current?.version, timezone])
  useEffect(() => {
    if (viewerIndex === null) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'ArrowRight') setViewerIndex((index) => index === null ? null : Math.min(evidence.length - 1, index + 1))
      if (event.key === 'ArrowLeft') setViewerIndex((index) => index === null ? null : Math.max(0, index - 1))
      if (event.key === 'Escape') setViewerIndex(null)
    }
    document.addEventListener('keydown', onKey); return () => document.removeEventListener('keydown', onKey)
  }, [viewerIndex, evidence.length])
  const frame = viewerIndex === null ? undefined : evidence[viewerIndex]
  const candidates = siblingSegments.filter((item) => item.id !== current?.id && item.camera_id === current?.camera_id && item.canonical_activity_id === current?.canonical_activity_id && !item.locked && current?.canonical_activity_id && (new Date(item.start).getTime() === new Date(current.end).getTime() || new Date(item.end).getTime() === new Date(current.start).getTime()))
  const beginEdit = () => { setEditing(true); setError(''); setConflict(false) }
  const startSplit = () => { if (!current) return; const middle = new Date((new Date(current.start).getTime() + new Date(current.end).getTime()) / 2).toISOString(); setSplitAt(toLocalInput(middle, timezone)); setSplitOpen(true); setError('') }
  const submitEdit = (event: React.FormEvent) => {
    event.preventDefault(); setError('')
    if (!form.reason.trim()) { setError('请填写编辑原因，便于后续追溯。'); return }
    const start = fromLocalInput(form.start, timezone); const end = fromLocalInput(form.end, timezone)
    if (!start || !end || new Date(end).getTime() <= new Date(start).getTime()) { setError('结束时间必须晚于开始时间。'); return }
    saveMutation.mutate()
  }
  const markUnavailable = (id: string) => setUnavailableImages((items) => items.includes(id) ? items : [...items, id])
  return <>
    <Drawer open={Boolean(segmentId)} title={current?.canonical_activity ?? (detailQuery.isPending ? '读取活动详情…' : '活动详情')} onClose={onClose}>
      <DataState isLoading={Boolean(segmentId && detailQuery.isPending)} error={detailQuery.error} onRetry={() => void detailQuery.refetch()}>
        {current && <>
          <div className="detail-summary"><div className="detail-badges"><StatusPill status={current.state} />{current.locked && <span className="subtle-tag"><Icon name="lock" size={13} />用户锁定</span>}{current.userEdited && <span className="subtle-tag">用户修订</span>}{current.evidenceExpired && <span className="subtle-tag">证据已过期</span>}</div><h3>{current.canonical_activity ?? '未知 / 证据不足'}</h3><p>{current.display_description || '服务端未提供自由描述。'}</p><div className="detail-meta-grid"><div><small>开始</small><strong className="mono">{formatDateTime(current.start, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }, timezone)}</strong></div><div><small>结束</small><strong className="mono">{formatDateTime(current.end, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }, timezone)}</strong></div><div><small>类别</small><strong>{current.category || '未分类'}</strong></div><div><small>活动时长</small><strong>{current.duration_ms == null ? '时长未返回' : formatDuration(current.duration_ms)}</strong></div></div><div className="confidence-row"><span>置信度 <strong>{formatPercent(current.confidence)}</strong></span><span>证据覆盖 <strong>{formatPercent(current.evidence_coverage)}</strong></span><span>版本 <strong>v{current.version}</strong></span></div></div>
          {editing ? <form className="edit-form" onSubmit={submitEdit}><SectionHeading title="修订活动记录" detail="保存使用 expected_version；版本冲突时保留当前草稿。" level={3} /><div className="form-grid-two"><Field label="开始时间"><input type="datetime-local" value={form.start} onChange={(event) => setForm({ ...form, start: event.target.value })} required /></Field><Field label="结束时间"><input type="datetime-local" value={form.end} onChange={(event) => setForm({ ...form, end: event.target.value })} required /></Field></div><Field label="标准活动"><select value={form.activityId} onChange={(event) => { const item = activities.find((activity) => activity.id === event.target.value); setForm({ ...form, activityId: event.target.value, category: item?.category ?? form.category }) }}><option value="">未知 / 证据不足</option>{activities.map((item) => <option key={item.id} value={item.id} disabled={!item.enabled}>{item.name}{item.category ? ` · ${item.category}` : ''}</option>)}</select></Field><Field label="类别"><input value={form.category} onChange={(event) => setForm({ ...form, category: event.target.value })} maxLength={128} /></Field><Field label="具体描述"><textarea rows={3} value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} maxLength={2000} /></Field><label className="check-field"><input type="checkbox" checked={form.locked} onChange={(event) => setForm({ ...form, locked: event.target.checked })} /><span>锁定此活动段，避免自动整理覆盖</span></label><Field label="编辑原因"><input value={form.reason} onChange={(event) => setForm({ ...form, reason: event.target.value })} required maxLength={500} placeholder="简要说明本次修订" /></Field><Notice tone="warning" icon="warning" title="保存影响">此活动段将保存为用户修正版，相关总结会标记为待更新；已锁定区间不会被自动分析覆盖。</Notice>{conflict && <Notice tone="danger" icon="warning" title="记录版本已变化">服务器已存在更新版本。当前草稿仍保留。可重新加载服务端版本，或继续保留草稿并检查后再提交。</Notice>}{error && <div className="inline-error" role="alert">{error}</div>}<div className="form-actions"><Button type="button" onClick={() => { setEditing(false); setConflict(false); setError('') }}>取消</Button>{conflict && <Button type="button" onClick={() => { if (serverVersion) { setForm({ start: toLocalInput(serverVersion.start, timezone), end: toLocalInput(serverVersion.end, timezone), activityId: serverVersion.canonical_activity_id ?? '', category: serverVersion.category ?? '', description: serverVersion.display_description ?? '', locked: serverVersion.locked, reason: '' }); setConflict(false); setError('') } }}>重新加载服务器版本</Button>}<Button type="submit" variant="primary" disabled={saveMutation.isPending}>{saveMutation.isPending ? '保存中…' : '保存修订'}</Button></div></form> : <div className="detail-actions"><Button onClick={beginEdit}><Icon name="activity" size={15} />修订活动</Button><Button onClick={startSplit}>拆分</Button><Button onClick={() => { setMergeIds([current.id]); setMergeOpen(true) }} disabled={current.locked || !current.canonical_activity_id || candidates.length === 0}>合并相邻段</Button></div>}
          <section className="detail-section"><SectionHeading title="证据与分钟观察" detail="图片按服务端留存策略读取，不在浏览器离线保存。" level={3} />{evidence.length ? <div className="evidence-gallery">{evidence.map((item, index) => <EvidenceThumb key={item.id} item={item} timezone={timezone} index={index} unavailable={unavailableImages.includes(item.id)} onClick={() => { setViewerIndex(index); setImageMode('thumbnail') }} onError={() => markUnavailable(item.id)} />)}</div> : <div className="evidence-empty"><Icon name="image" /><div><strong>{detailQuery.isPending ? '读取证据…' : '当前活动没有可展示的关联照片'}</strong><p>{detailQuery.isPending ? '证据元信息正在加载。' : '活动文字和时间记录仍可追溯；没有照片不代表活动记录已丢失。'}</p></div></div>}{linkedObservations.length ? <div className="minute-observation-list">{linkedObservations.map((observation) => <article className="minute-observation" key={observation.id}><div className="minute-observation-head"><StatusPill status={observation.state} /><span className="mono">{formatTime(observation.start, true, timezone)} – {formatTime(observation.end, true, timezone)}</span><span>置信度 {formatPercent(observation.confidence)}</span></div><p>{observationDescription(observation)}</p><small>第一层分钟观察 · 来源 {observation.source ?? 'L1'} · {observation.evidence_frame_ids?.length ?? 0} 张关联帧</small></article>)}</div> : <p className="small-muted">当前查询未返回关联的第一层分钟观察。</p>}</section>
          <section className="detail-section"><SectionHeading title="活动区间与可信等级" detail="颜色之外同时使用标签和纹理区分证据。" level={3} /><div className="interval-list">{current.intervals.length ? current.intervals.map((interval, index) => <article className={`interval-row interval-${interval.evidence_kind}`} key={interval.id ?? index}><span className="interval-glyph" /><div><strong>{interval.evidence_kind === 'direct' ? '直接观察' : interval.evidence_kind === 'inferred' ? '推断补全' : '未知 / 证据不足'}</strong><span className="mono">{formatTime(interval.start, true, timezone)} – {formatTime(interval.end, true, timezone)}</span>{interval.reason && <p>{interval.reason}</p>}<small>置信度 {formatPercent(interval.confidence)} · 锚点 {interval.anchor_frame_ids?.length ?? 0}</small></div></article>) : <EmptyState title="未返回区间证据" detail="后端尚未为此活动提供区间级证据明细。" />}</div></section>
          <section className="detail-section"><SectionHeading title="修订历史" detail="仅显示服务端记录的用户变更。" level={3} />{detail?.revision_history?.length ? <div className="revision-list">{detail.revision_history.map((entry, index) => <article key={String(entry.id ?? index)}><span className="revision-mark" /><div><strong>{String(entry.operation ?? '修订')}</strong><p>{String(entry.reason ?? '未提供修订原因')}</p><small>{formatDateTime(String(entry.created_at ?? ''), { dateStyle: 'medium', timeStyle: 'short' }, timezone)}</small></div></article>)}</div> : <p className="small-muted">此活动没有用户修订记录。</p>}</section>{error && !editing && <div className="inline-error" role="alert">{error}</div>}
        </>}
      </DataState>
    </Drawer>
    <Modal open={splitOpen} title="拆分活动段" onClose={() => setSplitOpen(false)}><form className="form-stack" onSubmit={(event) => { event.preventDefault(); const point = fromLocalInput(splitAt, timezone); if (!point) { setError('所选本地时间在该时区不存在，可能处于夏令时跳时区间。'); return } if (!current || new Date(point).getTime() <= new Date(current.start).getTime() || new Date(point).getTime() >= new Date(current.end).getTime()) { setError('拆分点必须位于活动区间内部。'); return } splitMutation.mutate() }}><p>拆分后会形成两个独立活动段；两段保留原活动名称，后续分别修订。</p><Field label="拆分时间"><input type="datetime-local" value={splitAt} onChange={(event) => setSplitAt(event.target.value)} required /></Field>{error && <div className="inline-error">{error}</div>}<Notice tone="info">相关小时总结和日报将因活动区间变化标记为待更新。</Notice><div className="form-actions"><Button type="button" onClick={() => setSplitOpen(false)}>取消</Button><Button type="submit" variant="primary" disabled={splitMutation.isPending}>{splitMutation.isPending ? '拆分中…' : '确认拆分'}</Button></div></form></Modal>
    <Modal open={mergeOpen} title="合并相邻活动段" onClose={() => setMergeOpen(false)}><div className="form-stack"><p>仅允许同一摄像头、同一标准活动且未锁定的活动段。合并后保留服务端的修订历史。</p>{current && <div className="merge-current"><StatusPill status={current.state} /><strong>{current.canonical_activity}</strong><span className="mono">{formatTime(current.start, false, timezone)} – {formatTime(current.end, false, timezone)}</span></div>}{candidates.length ? candidates.map((item) => <label key={item.id} className="merge-option"><input type="checkbox" checked={mergeIds.includes(item.id)} onChange={(event) => setMergeIds(event.target.checked ? [...mergeIds, item.id] : mergeIds.filter((id) => id !== item.id))} /><span><strong>{item.canonical_activity}</strong><small>{formatTime(item.start, false, timezone)} – {formatTime(item.end, false, timezone)}</small></span></label>) : <EmptyState title="没有符合条件的相邻活动段" detail="需要同一摄像头、相同标准活动且未锁定。" />}{error && <div className="inline-error">{error}</div>}<div className="form-actions"><Button onClick={() => setMergeIds([current?.id ?? ''])}>仅当前活动</Button><Button variant="primary" disabled={mergeMutation.isPending || mergeIds.length < 2} onClick={() => mergeMutation.mutate()}>{mergeMutation.isPending ? '合并中…' : `合并 ${mergeIds.length} 个活动段`}</Button></div></div></Modal>
    <Modal open={viewerIndex !== null} title={frame ? `证据 · ${formatTime(frame.captured_at, true, timezone)}` : '证据查看器'} onClose={() => setViewerIndex(null)} wide>{frame && <div className="evidence-viewer"><div className="viewer-toolbar"><div><strong>{formatDateTime(frame.captured_at, { dateStyle: 'medium', timeStyle: 'medium' }, timezone)}</strong><small>机位 {shortId(frame.camera_id)} · {frame.width && frame.height ? `${frame.width} × ${frame.height}` : '尺寸未知'}</small></div><div className="viewer-actions"><Button size="sm" onClick={() => setImageMode(imageMode === 'thumbnail' ? 'original' : 'thumbnail')}>{imageMode === 'thumbnail' ? '查看原图' : '查看缩略图'}</Button><Button size="sm" onClick={async () => { try { await toggleFavorite(frame.id, !frame.favorite); await queryClient.invalidateQueries({ queryKey: ['segment', segmentId] }) } catch (cause) { setError(cause instanceof Error ? cause.message : '收藏状态更新失败') } }}>{frame.favorite ? '取消收藏' : '收藏证据'}</Button></div></div><div className="viewer-stage"><button className="viewer-nav" disabled={viewerIndex === 0} onClick={() => setViewerIndex((index) => index === null ? null : index - 1)} aria-label="上一张"><Icon name="left" /></button>{unavailableImages.includes(frame.id) || frame.expired ? <div className="expired-frame"><Icon name="image" size={34} /><strong>原图已按留存策略清理</strong><p>活动文字记录仍可查看。此图片不会被重复请求。</p></div> : <img src={imageMode === 'original' ? frame.content_url ?? '' : frame.thumbnail_url ?? frame.content_url ?? ''} alt={`活动证据，${formatDateTime(frame.captured_at, { dateStyle: 'medium', timeStyle: 'medium' }, timezone)}`} onError={() => markUnavailable(frame.id)} />}<button className="viewer-nav" disabled={viewerIndex === evidence.length - 1} onClick={() => setViewerIndex((index) => index === null ? null : index + 1)} aria-label="下一张"><Icon name="right" /></button></div><p className="viewer-help">使用 ← / → 浏览，Esc 关闭。图片无法访问时将保留活动文字，不会无限重试。</p></div>}</Modal>
  </>
}
function EvidenceThumb({ item, timezone, index, unavailable, onClick, onError }: { item: EvidenceFrame; timezone: string; index: number; unavailable: boolean; onClick: () => void; onError: () => void }) { return <button className={`evidence-thumb ${unavailable || item.expired ? 'expired' : ''}`} onClick={onClick} aria-label={`查看证据 ${index + 1}，${formatTime(item.captured_at, true, timezone)}`}>{unavailable || item.expired ? <span className="thumb-unavailable"><Icon name="image" size={20} />照片已过期</span> : <img loading="lazy" decoding="async" src={item.thumbnail_url ?? ''} alt={`活动证据 ${formatTime(item.captured_at, true, timezone)}`} onError={(event) => { event.currentTarget.removeAttribute('src'); onError() }} />}<span className="thumb-time">{formatTime(item.captured_at, true, timezone)}</span>{item.favorite && <span className="favorite-mark">已收藏</span>}</button> }
