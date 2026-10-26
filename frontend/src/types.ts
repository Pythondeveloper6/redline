export type SpanKind = 'sql' | 'cache' | 'http_out' | 'template' | 'middleware' | 'view' | 'custom'
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
export type Severity = 'info' | 'warning' | 'critical'
export interface Issue {
  id: string; kind: IssueKind; severity: Severity; title: string; detail: string
  span_ids: string[]; fingerprint: string | null; callsite: Callsite | null
  count: number; total_ms: number; suggestion: Suggestion | null
}
export interface Trace {
  id: string; framework: string; method: string; path: string; route: string | null
  view: string | null; status: number; started_at: number; duration_ms: number
  query_count: number; query_ms: number
  request: { headers: Record<string, string>; query: Record<string, any>; body: string | null; body_truncated: boolean; client: string | null }
  response: { headers: Record<string, string>; body: string | null; body_truncated: boolean; size: number | null }
  spans: Span[]; issues: Issue[]; tags: Record<string, string>
  error: { type: string; message: string; traceback: string } | null
  has_profile: boolean
}
export interface TraceSummary {
  id: string; method: string; path: string; route: string | null; view: string | null
  status: number; started_at: number; duration_ms: number; query_count: number; query_ms: number
  issue_count: number; issue_kinds: IssueKind[]; has_profile: boolean; tags: Record<string, string>
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
  storage: { rows: number; path: string }
}
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
}
