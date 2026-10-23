import type { EvidenceKind, TimelineSegment } from './types'

export type SegmentStatus = 'final' | 'provisional' | 'unknown' | 'failed' | 'processing'
export type TimelineSegmentVM = {
  id: string
  cameraId: string
  startMs: number
  endMs: number
  title: string
  description?: string
  category?: string
  status: SegmentStatus
  sourceState: string
  confidence?: number
  evidenceCoverage?: number
  intervals: Array<{ startMs: number; endMs: number; kind: EvidenceKind; reason?: string; anchors: string[] }>
  locked: boolean
  version: number
  evidenceExpired: boolean
  valid: boolean
  overlapDetected: boolean
  source: TimelineSegment
}

const mapState = (state: string): SegmentStatus => {
  if (state === 'final' || state === 'provisional' || state === 'failed' || state === 'processing') return state
  return 'unknown'
}
const evidenceKind = (kind: string): EvidenceKind => kind === 'direct' || kind === 'inferred' ? kind : 'unknown'
const parseMs = (value: string) => {
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? timestamp : null
}
const ratio = (value?: number | null) => value != null && Number.isFinite(value) && value >= 0 && value <= 1 ? value : undefined

export function toTimelineSegmentVM(segment: TimelineSegment): TimelineSegmentVM {
  const startMs = parseMs(segment.start)
  const endMs = parseMs(segment.end)
  const valid = startMs !== null && endMs !== null && endMs > startMs
  const lower = startMs ?? 0
  const upper = endMs ?? lower
  const intervals = valid ? (segment.intervals ?? []).flatMap((interval) => {
    const start = parseMs(interval.start)
    const end = parseMs(interval.end)
    if (start === null || end === null || end <= start || end <= lower || start >= upper) return []
    return [{
      startMs: Math.max(lower, start),
      endMs: Math.min(upper, end),
      kind: evidenceKind(interval.evidence_kind),
      reason: interval.reason ?? undefined,
      anchors: interval.anchor_frame_ids ?? [],
    }]
  }).sort((a, b) => a.startMs - b.startMs || a.endMs - b.endMs) : []
  const overlapDetected = intervals.some((interval, index) => index > 0 && interval.startMs < intervals[index - 1].endMs)
  const rawState = String(segment.state ?? 'unknown')
  const status: SegmentStatus = !valid || overlapDetected ? 'unknown' : mapState(rawState)
  const safeIntervals = overlapDetected ? intervals.map((interval) => ({ ...interval, kind: 'unknown' as const, reason: interval.reason ?? '区间重叠，显示降级为未知' })) : intervals
  return {
    id: segment.id,
    cameraId: segment.camera_id,
    startMs: lower,
    endMs: upper,
    title: segment.canonical_activity || '未知 / 证据不足',
    description: segment.display_description ?? undefined,
    category: segment.category ?? undefined,
    status,
    sourceState: rawState,
    confidence: ratio(segment.confidence),
    evidenceCoverage: ratio(segment.evidence_coverage),
    intervals: safeIntervals,
    locked: Boolean(segment.locked),
    version: Number.isFinite(segment.version) && segment.version > 0 ? segment.version : 1,
    evidenceExpired: Boolean(segment.evidenceExpired),
    valid,
    overlapDetected,
    source: segment,
  }
}

export function mapTimelineSegment(segment: TimelineSegment): TimelineSegment | null {
  const vm = toTimelineSegmentVM(segment)
  if (!vm.valid) return null
  return {
    ...segment,
    state: vm.status,
    source_state_code: vm.sourceState,
    confidence: vm.confidence,
    evidence_coverage: vm.evidenceCoverage,
    intervals: vm.intervals.map((interval) => ({
      start: new Date(interval.startMs).toISOString(),
      end: new Date(interval.endMs).toISOString(),
      evidence_kind: interval.kind,
      reason: interval.reason,
      anchor_frame_ids: interval.anchors,
    })),
  }
}
