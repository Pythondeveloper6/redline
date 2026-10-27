export type SpanKind = 'sql' | 'cache' | 'http_out' | 'template' | 'middleware' | 'view' | 'custom'
  | 'serializer' | 'resolver' | 'transaction' | 'blocking' | 'pool_wait'
export interface Frame { file: string; line: number; function: string }
export interface Callsite { file: string; line: number; function: string; code: string | null; stack: Frame[] }
export interface Span {
  id: string; parent_id: string | null; kind: SpanKind; name: string
  start_ms: number; duration_ms: number; callsite: Callsite | null
  data: Record<string, any>
}
export interface Suggestion {
  summary: string; code: string | null; diff: string | null
  confidence: 'high' | 'medium' | 'low'; docs_url: string | null
}
export type IssueKind = 'n_plus_one' | 'duplicate_query' | 'slow_query' | 'too_many_queries'
  | 'blocking_in_async' | 'large_result' | 'unbounded_query' | 'inefficient_count'
  | 'slow_serializer' | 'long_transaction' | 'external_in_transaction'
  | 'slow_external' | 'duplicate_external' | 'retry_storm'
export type Severity = 'info' | 'warning' | 'critical'
export interface Issue {
  id: string; kind: IssueKind; severity: Severity; title: string; detail: string
  span_ids: string[]; fingerprint: string | null; callsite: Callsite | null
  count: number; total_ms: number; suggestion: Suggestion | null
  extra?: Record<string, any>
}
export interface ErrorFrame { file: string; line: number; function: string; code?: string | null; locals?: Record<string, string> }
export interface TraceError { type: string; message: string; traceback: string; frames?: ErrorFrame[] }
export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'
export interface LogRecord { t_ms: number; level: LogLevel | string; logger: string; message: string; callsite: Callsite | null }
export interface Trace {
  id: string; framework: string; method: string; path: string; route: string | null
  view: string | null; status: number; started_at: number; duration_ms: number
  query_count: number; query_ms: number
  request: { headers: Record<string, string>; query: Record<string, any>; body: string | null; body_truncated: boolean; client: string | null }
  response: { headers: Record<string, string>; body: string | null; body_truncated: boolean; size: number | null }
  spans: Span[]; issues: Issue[]; tags: Record<string, string>
  error: TraceError | null
  logs?: LogRecord[]; logs_dropped?: number; release?: string | null
  truncated?: { dropped_spans: number; max_spans: number } | null
  has_profile: boolean
}
export interface TraceSummary {
  id: string; method: string; path: string; route: string | null; view: string | null
  status: number; started_at: number; duration_ms: number; query_count: number; query_ms: number
  issue_count: number; issue_kinds: IssueKind[]; has_profile: boolean; tags: Record<string, string>
  /** Insertion order; present on live-tail events only. */
  seq?: number
}
export interface Page { items: TraceSummary[]; next_cursor: string | null; total: number }
export interface EndpointStats {
  route: string; method: string; count: number; error_rate: number
  p50_ms: number; p95_ms: number; p99_ms: number
  avg_queries: number; max_queries: number; avg_query_ms: number; issue_count: number
  series: { t: number; p95_ms: number; count: number }[]
}
export interface IssueGroup {
  id: string; kind: IssueKind; severity: Severity; title: string; detail: string
  occurrences: number; first_seen: number; last_seen: number; routes: string[]
  sample_trace_id: string; sample_span_ids: string[]; suggestion: Suggestion | null; callsite: Callsite | null
}
export interface PlanNode {
  label: string; detail: string; cost: number | null; rows: number | null
  actual_ms: number | null; children: PlanNode[]; flags: string[]
}
export interface ExplainResult {
  vendor: string; analyzed: boolean; plan: unknown; text: string; tree: PlanNode
  findings: { severity: Severity; title: string; detail: string }[]
  suggested_indexes: string[]
}
export interface Meta {
  version: string; framework: string
  settings: Record<string, any>
  storage: { rows: number; path: string; dropped?: number }
  // v0.2 (CONTRACT §12); absent on older backends
  project_root?: string | null; editor?: string | null; editor_url_template?: string | null; release?: string | null
  features?: Partial<Record<FeatureName, boolean>>
  access?: Access
}
export type FeatureName = 'load_from_ui' | 'sse' | 'snapshot' | 'explain' | 'profile' | 'replay' | 'load' | 'logs' | 'locals'
export interface Access {
  policy: 'local' | 'token' | 'basic' | 'framework' | 'custom' | 'open'
  login: 'token' | 'basic' | null
  read_only: boolean
  preset: 'dev' | 'staging' | 'production'
}
export type ConfigSource = 'default' | 'adapter' | 'preset' | 'file' | 'env' | 'framework'
export interface ConfigResponse {
  preset: string
  file: string | null
  settings: Record<string, unknown>
  sources: Record<string, ConfigSource | string>
  groups: Record<string, string[]>
  warnings: string[]
}
export type LoginBody = { token: string } | { username: string; password: string }
export interface Speedscope {
  shared: { frames: { name: string; file?: string; line?: number }[] }
  profiles: {
    type: 'evented' | 'sampled'; name: string; unit: string; startValue: number; endValue: number
    events?: { type: 'O' | 'C'; at: number; frame: number }[]
    samples?: number[][]; weights?: number[]
  }[]
  name?: string
}
export interface TraceFilters {
  q?: string; method?: string; status?: string; min_ms?: string; min_queries?: string; has_issues?: string
  replay_of?: string; route?: string; sql?: string; since?: string
}

export interface CompareResult {
  a: TraceSummary; b: TraceSummary
  delta: { duration_ms: number; query_count: number; query_ms: number }
  by_kind: Record<string, { a_ms: number; b_ms: number; a_count: number; b_count: number }>
  sql: { fingerprint: string; sql: string; a_count: number; b_count: number; a_ms: number; b_ms: number }[]
  issues: { only_a: unknown[]; only_b: unknown[]; both: unknown[] }
}

// ---- History / releases / load runs (CONTRACT §12) ----
export interface HistoryPoint { t: number; count: number; p50_ms: number; p95_ms: number; p99_ms: number; error_rate: number; avg_queries: number }
export interface ReleaseMark { release: string; first_seen: number }
export interface HistoryResp { series: HistoryPoint[]; releases: ReleaseMark[] }
export interface ReleaseInfo { release: string; first_seen: number; last_seen: number; requests: number }
export interface PeriodStats { count: number; error_rate: number; p50_ms: number; p95_ms: number; p99_ms: number; avg_ms?: number; avg_queries: number }
export type Verdict = 'regressed' | 'improved' | 'same' | 'new' | 'gone'
export interface CompareEndpoint {
  route: string; method: string; a: PeriodStats | null; b: PeriodStats | null
  delta_p95_pct: number | null; delta_queries: number | null; verdict: Verdict
}
export interface CompareIssue {
  id?: string; issue_id?: string; kind: IssueKind; severity?: Severity; title: string; route?: string | null; occurrences?: number
  first_seen?: number; last_seen?: number; first_release?: string | null; last_release?: string | null
}
/** `a` / `b` of api/history/compare echo the period. */
export type ComparePeriod = { kind: 'release'; release: string; requests: number } | { kind: 'range'; start: number; end: number; requests: number }
export interface CompareResp {
  a: ComparePeriod | unknown; b: ComparePeriod | unknown; endpoints: CompareEndpoint[]
  issues: { new: CompareIssue[]; resolved: CompareIssue[]; persisting: CompareIssue[] }
}
export interface RunStep {
  step: number; target_rps: number; started_at?: number; ended_at?: number | null; achieved_rps: number; sent: number; ok: number; errors: number; dropped: number
  p50_ms: number; p95_ms: number; p99_ms: number; max_ms: number; error_rate: number; error_breakdown: Record<string, number>
}
export type RunStatus = 'running' | 'done' | 'stopped' | 'failed'
export interface RunSummary {
  knee_step: number | null; max_ok_rps: number | null; verdict: string | null; verdict_detail?: string | null; knee_reason?: string | null
  sent?: number; ok?: number; errors?: number; dropped?: number; error_rate?: number; p50_ms?: number; p95_ms?: number; p99_ms?: number; max_ms?: number
}
export interface RunConfig { requests?: { method: string; path: string; weight?: number; name?: string }[]; profile?: Record<string, any>; [k: string]: any }
export interface RunItem {
  id: string; name: string; status: RunStatus | string; config?: RunConfig; created_at: number; finished_at: number | null
  summary?: RunSummary | null; steps?: RunStep[]
}
export interface Run extends RunItem { steps: RunStep[] }
export interface BottleneckStep { step: number; mean_ms: number; p95_ms: number; count: number }
export interface Bottleneck { key: string; kind: string; label: string; callsite: Callsite | null | string; per_step: BottleneckStep[]; growth: number }
export interface Bottlenecks { steps: { step: number; target_rps: number | null }[]; items: Bottleneck[] }
export interface NewRunBody {
  name: string
  requests: { method: string; path: string; weight: number }[]
  profile: { mode: 'constant' | 'ramp' | 'steps'; rps?: number; start_rps?: number; end_rps?: number; step_rps?: number; duration_s: number; step_duration_s?: number }
}

// ---- v0.2 analysis endpoints (CONTRACT §12; code wins) ----
export interface SqlStat {
  fingerprint: string; sql: string; count: number; traces: number; total_ms: number; max_ms: number; mean_ms?: number
  mean_rows?: number | null; routes: string[]; sample_trace_id: string; vendor?: string
}
export interface IndexCandidate {
  table: string; columns: string[]; create_sql: string; covers: string[]; queries: number; total_ms: number
  est_saving_ms: number; vendor: string; kind?: string; confidence?: string; verified?: boolean
}
export interface UnusedIndex { table: string; index: string; scans: number | null; size_bytes: number | null; drop_sql?: string }
export interface IndexAdvice { candidates: IndexCandidate[]; unused: UnusedIndex[]; unused_supported: boolean }
export interface CacheStat {
  backend: string; key_pattern: string; gets: number; hits: number; misses: number; hit_rate: number | null
  sets: number; deletes?: number; unread_sets: number; stampedes: number
}
export interface ExternalStat {
  method: string; host: string; url_pattern: string; count: number; total_ms?: number; p95_ms: number; max_ms: number
  errors: number; error_rate: number; duplicates: number; retries: number
}
export interface SnapshotData { trace: Trace; profile: Speedscope | null }
