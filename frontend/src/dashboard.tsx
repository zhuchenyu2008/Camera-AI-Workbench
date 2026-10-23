import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { getCameras, getJobs, getOverview, getSummaries, getTimeline } from './api'
import type { Camera, Job, Summary, TimelineSegment } from './types'
import { categoryTone, formatBytes, formatDateTime, formatDuration, formatPercent, formatRelative, formatTime, statusLabel } from './utils'
import { Button, DataState, EmptyState, ErrorNotice, Icon, PageHeading, SectionHeading, StatusPill } from './ui'
import { useWorkspace } from './workspace'

const recentHour = () => ({ from: new Date(Date.now() - 60 * 60_000).toISOString(), to: new Date().toISOString() })

export function DashboardPage() {
  const hour = useMemo(recentHour, [])
  const overview = useQuery({ queryKey: ['overview'], queryFn: getOverview, refetchInterval: 30_000 })
  const cameras = useQuery({ queryKey: ['cameras'], queryFn: getCameras, refetchInterval: 30_000 })
  const timeline = useQuery({ queryKey: ['timeline', { ...hour, scope: 'dashboard' }], queryFn: () => getTimeline({ ...hour, limit: 200 }) })
  const jobs = useQuery({ queryKey: ['jobs', { limit: 8 }], queryFn: () => getJobs({ limit: 8 }) })
  const summaries = useQuery({ queryKey: ['summaries', { currentHour: true }], queryFn: () => getSummaries({ period_kind: 'hour', from: new Date(Date.now() - 3 * 60 * 60_000).toISOString(), to: new Date().toISOString() }) })
  const { timezone } = useWorkspace()
  const cameraRecords = cameras.data ?? overview.data?.cameras ?? []
  const segments = timeline.data?.data.segments ?? []
  const activeCameras = cameraRecords.filter((camera) => camera.enabled)
  const failedJobs = (jobs.data ?? []).filter((job) => ['failed', 'needs_review', 'evidence_expired'].includes(job.state))
  const queued = overview.data?.jobs.queued
  const storage = overview.data?.storage
  const currentSummary = (summaries.data ?? []).find((summary) => summary.period_end && new Date(summary.period_end).getTime() > Date.now() - 60 * 60_000)
  const safeError = overview.error ?? cameras.error

  return <div className="page-stack dashboard-page">
    <PageHeading eyebrow="工作区 / 实时概览" title="工作台" description="采集状态、分析进度与活动记录。只展示服务端已确认的数据。" aside={<span className="updated-at"><span className="health-mark is-good" />最近同步 {overview.data?.current_window?.now ? formatTime(overview.data.current_window.now, true, timezone) : '等待数据'}</span>} actions={<Link className="button button-primary" to="/timeline"><Icon name="timeline" size={16} />查看时间轴</Link>} />

    {safeError && !overview.data && !cameras.data && <ErrorNotice error={safeError} onRetry={() => { void overview.refetch(); void cameras.refetch() }} />}

    <section className="workspace-section" aria-labelledby="camera-overview-title">
      <SectionHeading title="摄像头状态" detail={`${activeCameras.length} 路已启用 · 状态来自采集服务`} action={<Link className="text-link" to="/settings/cameras">管理摄像头 <Icon name="chevron" size={14} /></Link>} />
      <DataState isLoading={overview.isPending && cameras.isPending} error={!overview.data && !cameras.data ? (overview.error ?? cameras.error) : undefined} onRetry={() => { void overview.refetch(); void cameras.refetch() }}>
        {cameraRecords.length ? <div className="camera-list">{cameraRecords.map((camera) => <CameraRow key={camera.id} camera={camera} segments={segments.filter((segment) => segment.camera_id === camera.id)} timezone={timezone} latestJob={(jobs.data ?? []).find((job) => job.camera_id === camera.id && ['running', 'queued', 'failed'].includes(job.state))} />)}</div> : <EmptyState icon="camera" title="尚未配置摄像头" detail="添加摄像头后，采集连接状态和最近成功帧会显示在这里。" action={<Link className="button button-primary" to="/settings/cameras"><Icon name="plus" size={16} />添加摄像头</Link>} />}
      </DataState>
    </section>

    <section className="workspace-section" aria-labelledby="recent-timeline-title">
      <SectionHeading title="最近一小时" detail="第二层活动为主时间轴；第一层记录仅作为临时观察。" action={<Link className="text-link" to="/timeline?range=1h">打开完整时间轴 <Icon name="chevron" size={14} /></Link>} />
      {timeline.isPending ? <div className="timeline-preview-skeleton"><span /><span /><span /></div> : timeline.error ? <ErrorNotice error={timeline.error} onRetry={() => void timeline.refetch()} /> : segments.length ? <div className="dashboard-timeline">
        {activeCameras.map((camera) => {
          const rows = segments.filter((segment) => segment.camera_id === camera.id).sort((a, b) => a.start.localeCompare(b.start))
          return <div className="mini-timeline-row" key={camera.id}><Link to={`/timeline/${camera.id}`} className="mini-timeline-camera">{camera.name}</Link><div className="mini-track" aria-label={`${camera.name} 最近一小时活动`}>{rows.map((segment) => <TimelineStrip key={segment.id} segment={segment} camera={camera} from={hour.from} to={hour.to} timezone={timezone} />)}</div><div className="mini-track-labels"><span>{formatTime(hour.from, false, timezone)}</span><span>{formatTime(hour.to, false, timezone)}</span></div></div>
        })}
        <EvidenceLegend />
      </div> : <EmptyState icon="timeline" title="此区间尚无活动记录" detail="未生成记录不代表没有活动。请查看摄像头采集状态、处理中任务或其他时间范围。" action={<Link className="button button-secondary" to="/timeline">查看时间轴</Link>} />}
    </section>

    <div className="dashboard-lower-grid">
      <section className="workspace-section summary-section">
        <SectionHeading title="最近总结" detail="总结只归纳已生成的活动记录，不替代时间轴事实。" action={<Link className="text-link" to="/summary">全部总结 <Icon name="chevron" size={14} /></Link>} />
        {summaries.isPending ? <div className="summary-loading" /> : summaries.error ? <ErrorNotice error={summaries.error} onRetry={() => void summaries.refetch()} /> : currentSummary ? <SummaryExcerpt summary={currentSummary} timezone={timezone} /> : <EmptyState icon="summary" title="当前没有已生成的总结" detail="总结会在对应分析周期完成后由服务端生成。" />}
      </section>

      <section className="workspace-section task-section">
        <SectionHeading title="处理队列" detail="第一层、第二层和总结任务分别处理。" action={<Link className="text-link" to="/usage?tab=jobs">全部任务 <Icon name="chevron" size={14} /></Link>} />
        {jobs.isPending ? <div className="summary-loading" /> : jobs.error ? <ErrorNotice error={jobs.error} onRetry={() => void jobs.refetch()} /> : jobs.data?.length ? <div className="recent-job-list">{jobs.data.slice(0, 4).map((job) => <CompactJob key={job.id} job={job} camera={cameraRecords.find((camera) => camera.id === job.camera_id)} timezone={timezone} />)}</div> : <EmptyState icon="activity" title="暂无分析任务" detail="新采集或用户请求的处理任务会出现在这里。" />}
      </section>
    </div>

    <section className="workspace-section system-glance" aria-label="系统概况">
      <div className="glance-item"><span className="glance-icon"><Icon name="activity" /></span><div><small>队列等待</small><strong>{queued == null ? '未知' : `${queued} 项`}</strong></div></div>
      <div className="glance-item"><span className="glance-icon"><Icon name="database" /></span><div><small>数据占用</small><strong>{formatBytes(storage?.stored_bytes)}</strong></div></div>
      <div className="glance-item"><span className="glance-icon"><Icon name="warning" /></span><div><small>需处理任务</small><strong>{jobs.isPending ? '读取中' : `${failedJobs.length} 项`}</strong></div></div>
      <div className="glance-item"><span className="glance-icon"><Icon name="ai" /></span><div><small>分析能力</small><strong>{overview.data?.capabilities?.ai === true ? '已配置' : overview.data?.capabilities?.ai === false ? '未配置' : '未知'}</strong></div></div>
    </section>
  </div>
}

function CameraRow({ camera, segments, timezone, latestJob }: { camera: Camera; segments: TimelineSegment[]; timezone: string; latestJob?: Job }) {
  const latest = [...segments].sort((a, b) => b.start.localeCompare(a.start))[0]
  return <article className="camera-row">
    <div className={`camera-glyph ${camera.status === 'online' ? 'camera-online' : camera.status === 'offline' ? 'camera-offline' : 'camera-warn'}`}><Icon name="camera" size={20} /></div>
    <div className="camera-main"><div className="camera-title"><Link to={`/timeline/${camera.id}`}>{camera.name}</Link><StatusPill status={camera.status} /></div><div className="camera-meta"><span>{camera.last_frame_at ? `最近帧 ${formatRelative(camera.last_frame_at, Date.now(), timezone)}` : '尚无成功采集帧'}</span><span className="meta-separator">·</span><span>{camera.sampling_interval_ms ? `采样间隔 ${(camera.sampling_interval_ms / 1000).toFixed(camera.sampling_interval_ms % 1000 ? 1 : 0)} 秒` : '采样间隔未知'}</span></div></div>
    <div className="camera-pipeline"><small>分析状态</small>{latestJob ? <div className="pipeline-status"><StatusPill status={latestJob.state} /> <span>{latestJob.layer ?? 'AI'} · {formatTime(latestJob.period_start, true, timezone)}</span></div> : <div className="pipeline-status"><span>{latest?.state === 'final' ? '第二层已整理' : latest?.state === 'provisional' ? '第一层临时观察' : latest ? statusLabel(latest.state) : camera.enabled ? '等待分析记录' : '采集已停用'}</span></div>}</div>
    <div className="camera-latest"><small>最近活动</small><strong>{latest?.canonical_activity ?? (latest ? '未知 / 证据不足' : '暂无记录')}</strong><span>{latest?.display_description || (latest ? '以服务端活动段为准' : '没有可展示的已生成活动')}</span></div>
    <div className="camera-actions"><Link className="icon-button" title="打开时间轴" aria-label={`打开${camera.name}时间轴`} to={`/timeline/${camera.id}`}><Icon name="external" size={17} /></Link></div>
  </article>
}

function TimelineStrip({ segment, camera, from, to, timezone }: { segment: TimelineSegment; camera: Camera; from: string; to: string; timezone: string }) {
  const rangeStart = new Date(from).getTime(); const rangeEnd = new Date(to).getTime()
  const start = new Date(segment.start).getTime(); const end = new Date(segment.end).getTime()
  const left = Math.max(0, (start - rangeStart) / (rangeEnd - rangeStart) * 100)
  const width = Math.max(1.4, Math.min(100 - left, (end - start) / (rangeEnd - rangeStart) * 100))
  const label = segment.canonical_activity ?? '未知 / 证据不足'
  return <Link to={`/timeline/${camera.id}?segment=${encodeURIComponent(segment.id)}`} className={`timeline-block tone-${categoryTone(segment.category)} state-${segment.state}`} style={{ left: `${left}%`, width: `${width}%` }} title={`${label} · ${formatTime(segment.start, true, timezone)}–${formatTime(segment.end, true, timezone)} · ${statusLabel(segment.state)}`} aria-label={`${camera.name}，${label}，${formatTime(segment.start, true, timezone)} 到 ${formatTime(segment.end, true, timezone)}，${statusLabel(segment.state)}`}>
    {segment.intervals?.map((interval) => {
      const fromTs = Math.max(start, new Date(interval.start).getTime()); const toTs = Math.min(end, new Date(interval.end).getTime())
      const subLeft = (fromTs - start) / Math.max(1, end - start) * 100
      const subWidth = (toTs - fromTs) / Math.max(1, end - start) * 100
      return interval.evidence_kind === 'inferred' ? <span className="interval-inferred" key={interval.id} style={{ left: `${subLeft}%`, width: `${Math.max(subWidth, 1)}%` }} aria-hidden="true" /> : null
    })}
    {width > 13 && <span className="timeline-block-label">{label}</span>}
  </Link>
}

export function EvidenceLegend() {
  return <div className="evidence-legend" aria-label="时间轴图例"><span><i className="legend-solid" />直接观察</span><span><i className="legend-hatch" />推断补全</span><span><i className="legend-unknown" />未知</span><span><i className="legend-processing" />处理中</span><span><i className="legend-gap" />采集缺口</span></div>
}

function SummaryExcerpt({ summary, timezone }: { summary: Summary; timezone: string }) {
  const stats = summary.statistics ?? {}
  const activities = Array.isArray(stats.activities) ? stats.activities as Array<Record<string, unknown>> : []
  return <div className="summary-excerpt">
    {(summary.stale || summary.state === 'stale') && <div className="stale-note"><Icon name="refresh" size={15} />活动记录已修改，总结待更新</div>}
    <p className="summary-copy">{summary.body || '服务端未返回总结正文。'}</p>
    <div className="summary-stats-inline">{activities.slice(0, 3).map((activity, index) => <div key={`${String(activity.name)}-${index}`}><strong>{String(activity.name ?? '未命名活动')}</strong><span>{activity.duration_ms == null ? '时长未返回' : formatDuration(Number(activity.duration_ms))}</span></div>)}</div>
    <div className="summary-foot"><StatusPill status={summary.stale ? 'stale' : summary.state} /><span>{formatDateTime(summary.period_start, { hour: '2-digit', minute: '2-digit' }, timezone)} – {formatDateTime(summary.period_end, { hour: '2-digit', minute: '2-digit' }, timezone)}</span><Link to={`/timeline?from=${encodeURIComponent(summary.period_start)}&to=${encodeURIComponent(summary.period_end)}&cameraId=${encodeURIComponent(summary.camera_id ?? '')}`}>查看活动依据 <Icon name="chevron" size={14} /></Link></div>
  </div>
}

function CompactJob({ job, camera, timezone }: { job: Job; camera?: Camera; timezone: string }) {
  return <Link className="compact-job" to={`/usage?tab=jobs&job=${encodeURIComponent(job.id)}`}><span className={`job-layer layer-${(job.layer ?? 'ai').toLowerCase()}`}>{job.layer ?? 'AI'}</span><div className="compact-job-main"><strong>{camera?.name ?? '摄像头'}</strong><span>{job.error_message || (job.period_start ? formatDateTime(job.period_start, { hour: '2-digit', minute: '2-digit' }, timezone) : '任务周期未返回')}</span></div><StatusPill status={job.state} /><Icon name="chevron" size={15} /></Link>
}
