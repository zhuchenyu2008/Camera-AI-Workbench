import { describe, expect, it } from 'vitest'
import { formatDateTime, formatDuration, formatPercent, formatTime, fromLocalInput, toLocalInput } from './utils'
import { mapTimelineSegment, toTimelineSegmentVM } from './timeline-view-model'
import type { TimelineSegment } from './types'

const base: TimelineSegment = {
  id: 'segment-1', camera_id: 'camera-1', start: '2026-10-23T01:00:00Z', end: '2026-10-23T01:05:00Z',
  canonical_activity: '阅读', state: 'final', locked: false, version: 2,
  intervals: [
    { start: '2026-10-23T01:00:00Z', end: '2026-10-23T01:02:00Z', evidence_kind: 'direct' },
    { start: '2026-10-23T01:02:00Z', end: '2026-10-23T01:05:00Z', evidence_kind: 'inferred', reason: '遮挡' },
  ],
}

describe('timeline view model', () => {
  it('converts direct and inferred interval boundaries without inventing duration', () => {
    const vm = toTimelineSegmentVM(base)
    expect(vm.valid).toBe(true)
    expect(vm.endMs - vm.startMs).toBe(300_000)
    expect(vm.intervals.map((interval) => interval.kind)).toEqual(['direct', 'inferred'])
    expect(vm.status).toBe('final')
  })

  it('downgrades unsupported state and malformed evidence to unknown', () => {
    const mapped = mapTimelineSegment({ ...base, state: 'future-backend-state', intervals: [{ start: base.start, end: base.end, evidence_kind: 'unexpected' as any }] })
    expect(mapped?.state).toBe('unknown')
    expect(mapped?.intervals[0].evidence_kind).toBe('unknown')
    expect(mapped?.source_state_code).toBe('future-backend-state')
  })

  it('marks overlapping evidence intervals unknown instead of displaying them as direct', () => {
    const vm = toTimelineSegmentVM({ ...base, intervals: [base.intervals[0], { start: '2026-10-23T01:01:00Z', end: base.end, evidence_kind: 'direct' }] })
    expect(vm.overlapDetected).toBe(true)
    expect(vm.status).toBe('unknown')
    expect(vm.intervals.every((interval) => interval.kind === 'unknown')).toBe(true)
  })

  it('rejects invalid server time ranges for rendering', () => {
    expect(toTimelineSegmentVM({ ...base, end: base.start }).valid).toBe(false)
    expect(mapTimelineSegment({ ...base, start: 'not-a-time' })).toBeNull()
  })
})

describe('time and evidence display helpers', () => {
  it('formats API UTC instants in the selected IANA timezone', () => {
    expect(formatTime('2026-10-23T01:00:00Z', false, 'Asia/Shanghai')).toBe('09:00')
    expect(formatTime('2026-10-23T01:00:00Z', false, 'UTC')).toBe('01:00')
  })
  it('does not turn missing duration or evidence coverage into a numeric claim', () => {
    expect(formatDuration(null)).toBe('—')
    expect(formatPercent(null)).toBe('未知')
    expect(formatPercent(Number.NaN)).toBe('未知')
  })
})


describe('IANA timezone datetime-local conversion', () => {
  it('round-trips local values in a DST-observing timezone', () => {
    const utc = fromLocalInput('2026-03-08T03:30', 'America/Los_Angeles')
    expect(utc).toBe('2026-03-08T10:30:00.000Z')
    expect(toLocalInput(utc, 'America/Los_Angeles')).toBe('2026-03-08T03:30')
  })

  it('rejects nonexistent wall-clock time during the spring-forward gap', () => {
    expect(fromLocalInput('2026-03-08T02:30', 'America/Los_Angeles')).toBeUndefined()
  })

  it('resolves an ambiguous fall-back time deterministically to the earlier instant', () => {
    expect(fromLocalInput('2026-11-01T01:30', 'America/Los_Angeles')).toBe('2026-11-01T08:30:00.000Z')
  })

  it('keeps API instants independent from the host timezone', () => {
    expect(toLocalInput('2026-10-23T01:00:00Z', 'Asia/Shanghai')).toBe('2026-10-23T09:00')
    expect(toLocalInput('2026-10-23T01:00:00Z', 'UTC')).toBe('2026-10-23T01:00')
  })
})
