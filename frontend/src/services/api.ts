export type ResearchStatus = string;

export interface CompanyEntity {
  name: string;
  ticker?: string | null;
  cik?: string | null;
  resolved?: boolean;
}

export interface ResearchPlan {
  raw_query: string;
  companies: CompanyEntity[];
  period?: string | null;
  requested_metrics: string[];
  financial_questions: string[];
  qualitative_questions: string[];
  risk_questions: string[];
  is_comparison: boolean;
}

export interface ResearchRun {
  research_run_id: string;
  query: string;
  status: ResearchStatus;
  plan?: ResearchPlan | null;
  created_at: string;
  updated_at: string;
  followup_iterations_used: number;
  research_queries_used: number;
  error?: string | null;
}

export interface EvidenceSpan {
  source_id: string;
  start_char: number;
  end_char: number;
  evidence_text: string;
}

export interface Claim {
  claim_id: string;
  research_run_id: string;
  claim_type: string;
  entity: string;
  metric: string;
  value: string;
  unit?: string | null;
  period?: string | null;
  basis?: string | null;
  source_id: string;
  evidence_span: EvidenceSpan;
  normalized?: {
    magnitude?: number | null;
    base_unit?: string | null;
    period_type?: string | null;
    basis?: string | null;
  } | null;
  verification_status?: "supported" | "contradicted" | "insufficient" | string | null;
  verification_reason?: string | null;
  confidence?: number | null;
  confidence_breakdown?: Record<string, number> | null;
  statement: string;
}

export interface Source {
  source_id: string;
  title: string;
  url?: string | null;
  source_type: string;
  source_tier: string;
  publisher: string;
  retrieved_at: string;
  document_text: string;
  metadata: Record<string, unknown>;
}

export interface Conflict {
  conflict_id: string;
  entity: string;
  metric: string;
  claim_id_a: string;
  claim_id_b: string;
  source_id_a: string;
  source_id_b: string;
  value_a: string;
  value_b: string;
  reason_type: string;
  explanation: string;
  is_genuine_conflict: boolean;
}

export interface ReportSection {
  title: string;
  content: string;
  claim_ids: string[];
}

export interface ComparisonTable {
  title: string;
  columns: string[];
  rows: Record<string, unknown>[];
}

export interface Report {
  report_id: string;
  research_run_id: string;
  company_summary: string;
  executive_overview: ReportSection;
  financial_performance: ReportSection;
  key_metrics: ReportSection;
  risks: ReportSection;
  important_findings: ReportSection;
  conflicting_information: ReportSection;
  claim_verification_summary: ReportSection;
  sources_section: ReportSection;
  comparison_tables: ComparisonTable[];
  total_claims: number;
  supported_claims: number;
  contradicted_claims: number;
  insufficient_claims: number;
  average_confidence?: number | null;
}

export interface HealthResponse {
  status: string;
  demo_mode: boolean;
  llm_provider: string;
  llm_available: boolean;
  search_provider: string;
  search_available: boolean;
}

export const UNAUTHORIZED_EVENT = "verifi:unauthorized";

const BACKEND_DOWN = "Can't reach the backend server. Start it with .\\scripts\\dev.ps1 (port 8000), then retry.";

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error(BACKEND_DOWN);
  }
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    // An expired or missing session anywhere in the app sends the user back
    // to the login page (see main.tsx) - except for the auth calls
    // themselves, whose 401 is a normal "wrong password" answer.
    if (response.status === 401 && !url.startsWith("/api/auth/")) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    if (typeof data?.detail === "string") throw new Error(data.detail);
    // FastAPI always answers with a JSON body. A bodiless 5xx means the Vite
    // dev proxy couldn't reach the backend at all (e.g. it isn't running).
    if (data === null && response.status >= 500) throw new Error(BACKEND_DOWN);
    throw new Error(`Request failed (${response.status})`);
  }
  return data as T;
}

export const api = {
  health: () => fetchJson<HealthResponse>("/api/health"),
  // Database first: answered instantly from stored, verified filings, or
  // answered=false when the database can't answer (then startResearch).
  answer: (query: string) => fetchJson<DatabaseAnswer | NotAnswered>("/api/answer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  }),
  startResearch: (query: string, fresh = false) => fetchJson<ResearchRun>("/api/research", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, fresh }),
  }),
  research: (id: string) => fetchJson<ResearchRun>(`/api/research/${id}`),
  claims: (id: string) => fetchJson<Claim[]>(`/api/research/${id}/claims`),
  sources: (id: string) => fetchJson<Source[]>(`/api/research/${id}/sources`),
  conflicts: (id: string) => fetchJson<Conflict[]>(`/api/research/${id}/conflicts`),
  report: (id: string) => fetchJson<Report>(`/api/research/${id}/report`),
};

// ---- Industry dashboard ----------------------------------------------------

export interface IndustrySummary {
  id: string;
  name: string;
  short_name: string;
  universe: string;
  description: string;
  company_count: number;
}

export type CheckStatus = "verified" | "mismatch" | "unavailable";

export interface CrossCheck {
  metric: string;
  label: string;
  reported?: number | null;
  recomputed?: number | null;
  difference_pct?: number | null;
  tolerance_pct: number;
  status: CheckStatus;
  detail: string;
  informational: boolean;
}

export interface QuarterPoint {
  period_end: string;
  revenue?: number | null;
  net_income?: number | null;
  operating_profit?: number | null;
  eps_diluted?: number | null;
  filed_at?: string | null;
  audited?: string | null;
  filing_url?: string | null;
  notes: string[];
}

export interface CompanyMetrics {
  name: string;
  short_name: string;
  symbol: string;
  nse: string;
  tier: string;
  available: boolean;
  error?: string | null;
  shares_outstanding?: number | null;
  revenue_ttm?: number | null;
  net_income_ttm?: number | null;
  eps_ttm?: number | null;
  revenue_growth_yoy?: number | null;
  earnings_growth_yoy?: number | null;
  operating_margin?: number | null;
  profit_margin?: number | null;
  revenue_share?: number | null;
  latest_quarter?: string | null;
  quarters: QuarterPoint[];
  checks: CrossCheck[];
  verification_status: CheckStatus;
}

export interface MetricAggregate {
  metric: string;
  median?: number | null;
  mean?: number | null;
  weighted_mean?: number | null;
  min?: number | null;
  max?: number | null;
  leader?: string | null;
  laggard?: string | null;
  count: number;
}

export interface IndustrySnapshot {
  industry: IndustrySummary;
  mode: "live" | "demo";
  currency: string;
  fetched_at: string;
  fetch_seconds?: number | null;
  stale: boolean;
  refreshing: boolean;
  synced_at?: string | null;
  source: string;
  companies: CompanyMetrics[];
  aggregates: Record<string, MetricAggregate>;
  concentration: {
    total_revenue?: number | null;
    hhi?: number | null;
    effective_companies?: number | null;
    top3_share?: number | null;
    largest?: string | null;
    largest_share?: number | null;
  };
  insights: { kind: string; title: string; detail: string }[];
  warnings: string[];
}

export const industryApi = {
  list: () => fetchJson<IndustrySummary[]>("/api/industries"),
  snapshot: (id: string, refresh = false) =>
    fetchJson<IndustrySnapshot>(`/api/industries/${id}${refresh ? "?refresh=true" : ""}`),
};

// ---- Auth & public ------------------------------------------------------------

export interface SessionUser {
  kind: "user" | "demo";
  name: string;
  email?: string | null;
  expires_at: string;
}

const postJson = <T,>(url: string, body?: unknown) =>
  fetchJson<T>(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });

export const authApi = {
  me: () => fetchJson<SessionUser>("/api/auth/me"),
  login: (email: string, password: string) => postJson<SessionUser>("/api/auth/login", { email, password }),
  signup: (name: string, email: string, password: string) => postJson<SessionUser>("/api/auth/signup", { name, email, password }),
  demo: () => postJson<SessionUser>("/api/auth/demo"),
  logout: () => postJson<null>("/api/auth/logout"),
};

export interface OverviewCompany {
  name: string;
  short_name: string;
  nse: string;
  revenue_ttm?: number | null;
  latest_quarter?: string | null;
  quarters_filed: number;
  verification_status: CheckStatus;
  checks: { label: string; status: CheckStatus; difference_pct?: number | null }[];
}

export interface PublicOverview {
  industry?: string;
  universe?: string;
  mode?: "live" | "demo";
  verified?: number;
  companies: OverviewCompany[];
}

export const publicApi = {
  overview: () => fetchJson<PublicOverview>("/api/public/overview"),
};

// ---- Answers from the database ---------------------------------------------------

export interface ComparisonRow {
  metric: string;
  label: string;
  kind: "inr" | "pct" | "pct_signed" | "rupees";
  values: Record<string, number | null>;
  leader?: string | null;
  note?: string | null;
}

export interface PeriodFigures {
  found: boolean;
  label: string;
  period_end: string;
  revenue?: number | null;
  net_income?: number | null;
  operating_profit?: number | null;
  eps_diluted?: number | null;
  filing_url?: string | null;
  filed_at?: string | null;
  message?: string | null;
}

export interface DatabaseAnswer {
  answered: true;
  answered_from: "database";
  query: string;
  intent: "company" | "comparison";
  companies: CompanyMetrics[];
  focus_metrics: string[];
  period_label?: string | null;
  period_figures: Record<string, PeriodFigures>;
  comparison: ComparisonRow[];
  summary: string[];
  coverage: string;
  wants_qualitative: boolean;
  qualitative_terms: string[];
  elapsed_ms: number;
  synced_at?: string | null;
  warnings: string[];
}

export interface NotAnswered { answered: false; reason: string }
