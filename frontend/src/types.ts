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
  | 'repeated_writes' | 'deep_offset' | 'database_round_trips' | 'connection_pool_wait' | 'large_response' | 'repeated_scan' | 'large_cache_entry' | 'many_orm_instances' | 'json_decode_hotspot' | 'slow_cache_decode'
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
  runtime?: Record<string, string>
  diagnostics?: { duration_ms: number; time_ms: Record<string, number>; observed_span_kinds: Record<string, number>; profile_available: boolean; complete_spans: boolean; warnings: string[]; note: string }
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
  observed_total_ms?: number; fingerprint?: string | null; suppressed?: boolean; suppressed_routes?: string[]
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
export type FeatureName = 'load_from_ui' | 'sse' | 'snapshot' | 'explain' | 'profile' | 'replay' | 'load' | 'logs' | 'locals' | 'checks' | 'checks_from_ui'
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
  $schema?: string
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

// ---- v0.3 refactor check (CONTRACT §19) ----
export type CheckMethod = 'GET' | 'HEAD' | 'OPTIONS'
export interface CheckRequestDef {
  name: string; method: CheckMethod; path: string
  headers?: Record<string, string>
  perf?: boolean; response?: boolean
  ignore?: string[]; ignore_headers?: string[]
  expect_status?: number | null
  array_keys?: Record<string, string>
  tolerances?: Record<string, { absolute?: number; relative?: number }>
  budgets?: { max_queries?: number; max_median_ms?: number; max_p95_ms?: number; max_external_calls?: number; forbidden_issues?: string[] }
}
export interface CheckThresholds { slower_pct: number; slower_ms: number; max_query_increase: number }
/** The suite object of §19.1 (JSON form of the TOML). */
export interface CheckSuite {
  name: string; base_url?: string | null
  headers?: Record<string, string>
  environment?: Record<string, string>
  timeout_s?: number; repeat?: number; warmup?: number
  thresholds?: Partial<CheckThresholds>
  requests: CheckRequestDef[]
}
export interface CheckGit { sha?: string | null; branch?: string | null; dirty?: boolean | null }
export type CheckRunKind = 'baseline' | 'compare'
export type CheckRunStatus = 'running' | 'done' | 'failed' | 'stopped'
/** Per-verdict counts of a finished compare (flat or under `counts`; the UI accepts both). */
export interface CheckSummary {
  budget_failed?: number
  counts?: Partial<Record<PerfVerdict | ResponseVerdict | 'passed', number>>
  faster?: number; slower?: number; same?: number; noisy?: number; changed?: number; broken?: number; missing?: number; passed?: number
  /** Report-level warnings (e.g. requests missing from the baseline). */
  warnings?: string[]
  speed_change_pct?: number | null; queries_change?: number | null
}
/** Baseline summary: the fields of a baseline run needed to pick or describe it. */
export interface CheckBaselineSummary { id: string; label: string | null; git?: CheckGit | null; created_at: number }
export interface CheckRunItem {
  id: string; suite_id: string | null; kind: CheckRunKind; baseline_id: string | null; status: CheckRunStatus
  label: string | null; git?: CheckGit | null; created_at: number; finished_at: number | null
  summary?: CheckSummary | null; error?: string | null
  /** Optional live progress while running (calls done of total). */
  progress?: { done: number; total: number; current?: string | null } | null
}
export interface CheckSuiteItem {
  id: string; name: string; requests: number; updated_at: number
  last_baseline: CheckBaselineSummary | null
  last_compare: (Pick<CheckRunItem, 'id' | 'status' | 'created_at'> & { summary?: CheckSummary | null }) | null
}
export interface CheckSuiteDoc { id: string; name: string; suite: CheckSuite; created_at?: number; updated_at?: number }

export type PerfVerdict = 'faster' | 'slower' | 'same' | 'noisy' | 'n/a'
export type ResponseVerdict = 'same' | 'changed' | 'broken' | 'missing' | 'n/a'
export interface PerfStats { samples_ms: number[]; median: number; p95: number; mean: number; stdev: number; failed: number }
export interface ServerStats {
  runtime?: Record<string, string>
  duration_ms_median?: number | null; queries_median: number; queries_max?: number | null; query_ms_median?: number | null
  sql?: Record<string, { count: number; max?: number; sql?: string }>
  sample_trace_id?: string | null
  issues?: { kind: IssueKind; title?: string; fingerprint?: string }[]
}
export interface JsonDiffEntry { path: string; kind: 'added' | 'removed' | 'changed' | 'type' | 'invalid_identity'; before: unknown; after: unknown }
export interface ReportIssue { kind: IssueKind; title?: string; fingerprint?: string; sample_trace_id?: string | null }
export interface ReportSql { fingerprint: string; sql: string; count?: number; sample_trace_id?: string | null }
export interface ReportRequest {
  budgets?: { rule: string; limit: number | string[]; observed: number | string[] | null; passed: boolean; reason?: string | null }[]
  name: string; method: string; path: string
  perf: { verdict: PerfVerdict; before: PerfStats | null; after: PerfStats | null; change_ms: number | null; change_pct: number | null; p_value: number | null; reason: string | null; note?: string | null }
  server: { before: ServerStats | null; after: ServerStats | null; queries_change: number | null; new_issues: ReportIssue[]; fixed_issues: ReportIssue[]; new_sql: ReportSql[]; gone_sql: ReportSql[]
    /** Wins that are not counted because the response is broken (fixed issues, gone SQL, query decrease). */
    not_judged?: { fixed_issues: ReportIssue[]; gone_sql: ReportSql[]; queries_change: number | null }; note?: string | null } | null
  response: { verdict: ResponseVerdict; status: [number | null, number | null]; diff: JsonDiffEntry[]; diff_total?: number; text_diff: string | null; ignored: string[]; rules?: { array_keys: Record<string, string>; tolerances: Record<string, { absolute?: number; relative?: number }> } }
}
export interface CheckReport {
  format: 'redline-check-report'; version: number
  suite: CheckSuite
  baseline: { id?: string | null; file?: string | null; label: string | null; git?: CheckGit | null; created_at: number }
  run: { id: string; git?: CheckGit | null; created_at: number; base_url: string }
  summary?: CheckSummary
  requests: ReportRequest[]
}
/** One recorded request of a baseline (§19.3). */
export interface BaselineRequest {
  name: string; method: string; path: string
  perf: PerfStats | null
  server: ServerStats | null
  response: { status: number | null; content_type: string | null; headers?: Record<string, string>; body_kind: 'json' | 'text' | 'hash'; sha256?: string; size: number } | null
  volatile: string[]
}
/** `GET api/checks/runs/{id}`: the run plus its results (`report` for compares, `requests` for baselines). */
export interface CheckRunDetail extends CheckRunItem {
  suite?: CheckSuite | null
  report?: CheckReport | null
  requests?: BaselineRequest[] | null
}
export interface CheckDraftBody { routes?: string[]; limit?: number }
export type CheckStartBody = { suite_id: string; kind: CheckRunKind; baseline_id?: string; label?: string }
