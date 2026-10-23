export type ThemeMode = 'light' | 'dark'

export type CameraStatus = 'online' | 'offline' | 'reconnecting' | 'starting' | 'error' | string
export type SegmentState = 'final' | 'provisional' | 'unknown' | 'failed' | 'processing' | string
export type EvidenceKind = 'direct' | 'inferred' | 'unknown'

export type Camera = {
  id: string
  name: string
  enabled: boolean
  status: CameraStatus
  source_configured: boolean
  source_host?: string | null
  sampling_interval_ms?: number | null
  frame_width?: number | null
  frame_height?: number | null
  last_frame_at?: string | null
  created_at?: string | null
  updated_at?: string | null
  version?: number
}

export type ActivityInterval = {
  id?: string
  start: string
  end: string
  evidence_kind: EvidenceKind
  confidence?: number | null
  reason?: string | null
  anchor_frame_ids?: string[]
  evidence_frame_ids?: string[]
}

export type TimelineSegment = {
  id: string
  camera_id: string
  l2_window_id?: string | null
  start: string
  end: string
  duration_ms?: number | null
  canonical_activity_id?: string | null
  canonical_activity?: string | null
  display_description?: string | null
  category?: string | null
  confidence?: number | null
  evidence_coverage?: number | null
  state: SegmentState
  source_state_code?: string
  locked: boolean
  version: number
  intervals: ActivityInterval[]
  source_observation_ids?: string[]
  evidence_frames?: EvidenceFrame[]
  evidenceExpired?: boolean
  userEdited?: boolean
}

export type EvidenceFrame = {
  id: string
  camera_id: string
  captured_at?: string | null
  width?: number | null
  height?: number | null
  byte_size?: number | null
  state?: string
  favorite?: boolean
  expired?: boolean
  content_url?: string | null
  thumbnail_url?: string | null
}

export type Observation = {
  id: string
  camera_id: string
  start: string
  end: string
  source?: string
  state?: string
  observation?: Record<string, unknown>
  candidates?: unknown[]
  evidence_frame_ids?: string[]
  gaps?: unknown[]
  coverage_ratio?: number | null
  confidence?: number | null
  version?: number
}

export type CameraGap = {
  id: string
  camera_id: string
  start: string
  end?: string | null
  reason?: string | null
}

export type WindowState = {
  id: string
  camera_id: string
  start: string
  end: string
  state: string
  version?: number
}

export type TimelineData = {
  segments: TimelineSegment[]
  observations: Observation[]
  camera_gaps: CameraGap[]
  window_states: WindowState[]
}

export type Summary = {
  id: string
  camera_id?: string | null
  period_kind: string
  period_start: string
  period_end: string
  timezone: string
  body?: string | null
  statistics: Record<string, unknown>
  source_segment_ids: string[]
  state: string
  version: number
  stale: boolean
  created_at?: string | null
  updated_at?: string | null
}

export type Job = {
  id: string
  layer?: string | null
  kind?: string | null
  camera_id?: string | null
  state: string
  progress?: Record<string, unknown>
  priority?: number
  period_start?: string | null
  period_end?: string | null
  attempts?: number
  max_attempts?: number
  attempt_count?: number
  available_at?: string | null
  started_at?: string | null
  finished_at?: string | null
  error_code?: string | null
  error_message?: string | null
  input_frame_count?: number
  created_at?: string | null
  updated_at?: string | null
  retryable?: boolean | null
}

export type Usage = {
  totals: Record<string, number | null>
  buckets: Array<Record<string, string | number | null>>
  by_provider: Array<Record<string, string | number | null>>
  by_layer: Array<Record<string, string | number | null>>
  items?: Array<Record<string, string | number | null>>
}

export type Provider = {
  id: string
  label: string
  base_url: string
  base_host?: string | null
  timeout_ms: number
  enabled: boolean
  api_key_configured: boolean
  capabilities: Record<string, unknown>
  last_test_at?: string | null
  last_test_error?: string | null
  version?: number
}

export type Binding = {
  layer: string
  provider_id: string
  model_name: string
  fallback_provider_id?: string | null
  temperature?: number | null
  max_output_tokens?: number | null
  concurrency?: number
  timeout_ms?: number | null
}

export type ActivityCatalogItem = {
  id: string
  name: string
  category?: string | null
  aliases: string[]
  enabled: boolean
  description?: string | null
  usage_count?: number | null
  version?: number
}

export type Person = {
  id: string
  label: string
  enabled: boolean
  occurrence_count?: number | null
  first_seen_at?: string | null
  last_seen_at?: string | null
  version?: number
}

export type RetentionSettings = {
  ordinary_frame_minutes: number
  event_frame_minutes: number
  favorite_keeps_forever: boolean
  keep_timeline_after_expiry: boolean
  low_disk_watermark_bytes?: number | null
  revision: number
}

export type AnalysisSettings = {
  sampling_interval_ms: number
  stable_sample_seconds: number
  minute_window_seconds: number
  l2_window_minutes: number
  change_threshold: number
  max_inference_gap_minutes: number
  strict_mode: boolean
  revision: number
}

export type Overview = {
  cameras: Camera[]
  current_window?: { now?: string; timezone?: string } | null
  jobs: { by_state?: Record<string, number>; queued?: number }
  usage: Record<string, number>
  storage: Record<string, number>
  capabilities: Record<string, unknown>
}

export type Session = {
  username: string
  display_name: string
  csrf_token: string
  expires_at: string
}

export type AppData = {
  overview: Overview
  cameras: Camera[]
  timeline: TimelineData
  summaries: Summary[]
  jobs: Job[]
  usage: Usage
  providers: Provider[]
  bindings: Binding[]
  activities: ActivityCatalogItem[]
  persons: Person[]
  retention: RetentionSettings
  analysis: AnalysisSettings
  isPreview: boolean
}
