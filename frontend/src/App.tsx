import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Clock3,
  FileText,
  Filter,
  Globe2,
  Layers,
  LayoutDashboard,
  ScanSearch,
  LoaderCircle,
  Menu,
  PanelLeftClose,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import { IndustryView } from "./industry/IndustryView";
import { api, industryApi, type IndustrySummary, type Claim, type Conflict, type HealthResponse, type Report, type ResearchRun, type Source } from "./services/api";
import { Sidebar, type NavSection } from "./components/Sidebar";
import { UserMenu } from "./components/UserMenu";
import { Tabs } from "./components/Tabs";
import { Pipeline } from "./research/Pipeline";
import type { SessionUser } from "./services/api";

type Tab = "overview" | "financials" | "risks" | "findings" | "claims" | "conflicts" | "sources";
type ClaimFilter = "all" | "supported" | "contradicted" | "insufficient";

const examples = ["Analyze Infosys revenue and risks", "Compare TCS and Wipro", "HCLTech profitability and margins", "Analyze Persistent Systems growth"];
type AppView = { kind: "industry"; id: string } | { kind: "research" };
const POLL_INTERVAL_MS = 1500;
const MAX_CONSECUTIVE_POLL_ERRORS = 5;
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const tabs: { id: Tab; label: string; icon: typeof LayoutDashboard }[] = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "financials", label: "Financials", icon: BarChart3 },
  { id: "risks", label: "Risks", icon: AlertTriangle },
  { id: "findings", label: "Key findings", icon: Sparkles },
  { id: "claims", label: "Claims", icon: ShieldCheck },
  { id: "conflicts", label: "Conflicts", icon: Filter },
  { id: "sources", label: "Sources", icon: BookOpen },
];

function StatusDot({ tone = "teal" }: { tone?: "teal" | "amber" | "red" }) {
  return <span className={`status-dot status-${tone}`} aria-hidden="true" />;
}

function Button({ children, onClick, variant = "secondary", disabled = false, className = "" }: { children: React.ReactNode; onClick?: () => void; variant?: "primary" | "secondary" | "ghost"; disabled?: boolean; className?: string }) {
  return <button type="button" className={`button button-${variant} ${className}`} onClick={onClick} disabled={disabled}>{children}</button>;
}

function Metric({ label, value, detail, tone = "neutral" }: { label: string; value: string; detail: string; tone?: "positive" | "negative" | "neutral" }) {
  return <div className="metric"><span className="eyebrow">{label}</span><strong>{value}</strong><span className={`metric-detail ${tone}`}>{tone === "positive" ? <ArrowUpRight size={12} /> : tone === "negative" ? <ArrowDownRight size={12} /> : null}{detail}</span></div>;
}

function App({ user, onSignOut }: { user: SessionUser; onSignOut: (next?: "signin" | "signup") => void }) {
  const [query, setQuery] = useState("");
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const [claimFilter, setClaimFilter] = useState<ClaimFilter>("all");
  const [run, setRun] = useState<ResearchRun | null>(null);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem("verifi.sidebar") === "collapsed"; } catch { return false; } });
  const toggleSidebar = useCallback(() => setCollapsed((c) => {
    try { localStorage.setItem("verifi.sidebar", c ? "expanded" : "collapsed"); } catch { /* storage unavailable */ }
    return !c;
  }), []);
  const [industries, setIndustries] = useState<IndustrySummary[]>([]);
  const [appView, setAppView] = useState<AppView>({ kind: "industry", id: "information-technology" });

  useEffect(() => { api.health().then(setHealth).catch(() => undefined); }, []);
  useEffect(() => { industryApi.list().then(setIndustries).catch(() => undefined); }, []);

  const go = (next: AppView) => { setAppView(next); setSidebarOpen(false); window.scrollTo({ top: 0 }); };
  const openDeepDive = (nextQuery: string) => { go({ kind: "research" }); setQuery(nextQuery); runResearch(nextQuery); };

  const runResearch = async (nextQuery = query) => {
    const trimmed = nextQuery.trim();
    if (!trimmed || loading) return;
    setQuery(trimmed); setLoading(true); setError(""); setRun(null); setReport(null); setClaims([]); setSources([]); setConflicts([]);
    try {
      // The pipeline runs in the background on the server - this call
      // returns immediately with status="pending" and a run id. We poll
      // for live status instead of blocking on one long request, so the
      // UI can show real progress (and never silently hangs on a
      // multi-minute real-data run).
      const startedRun = await api.startResearch(trimmed);
      setRun(startedRun);

      // A real run can poll for several minutes, so a single transient
      // network blip must not discard an otherwise-successful run. Only
      // give up after several consecutive failures.
      let current = startedRun;
      let consecutivePollErrors = 0;
      while (current.status !== "complete" && current.status !== "failed") {
        await sleep(POLL_INTERVAL_MS);
        try {
          current = await api.research(startedRun.research_run_id);
          consecutivePollErrors = 0;
          setRun(current);
        } catch (pollError) {
          consecutivePollErrors += 1;
          if (consecutivePollErrors >= MAX_CONSECUTIVE_POLL_ERRORS) throw pollError;
        }
      }

      if (current.status === "failed") throw new Error(current.error || "Research run failed.");
      const [nextClaims, nextSources, nextConflicts, nextReport] = await Promise.all([
        api.claims(current.research_run_id), api.sources(current.research_run_id), api.conflicts(current.research_run_id), api.report(current.research_run_id),
      ]);
      setClaims(nextClaims); setSources(nextSources); setConflicts(nextConflicts); setReport(nextReport); setActiveTab("overview");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally { setLoading(false); }
  };

  const company = run?.plan?.companies?.map((item) => item.ticker ? `${item.name} (${item.ticker})` : item.name).join(", ") || run?.plan?.raw_query || "Awaiting research";
  const filteredClaims = useMemo(() => claimFilter === "all" ? claims : claims.filter((claim) => claim.verification_status === claimFilter), [claims, claimFilter]);
  const closeSidebar = () => setSidebarOpen(false);
  const navSections: NavSection[] = [
    {
      label: "Industries",
      items: industries.map((ind) => ({
        id: ind.id, label: ind.name, icon: <Layers size={18} />, badge: ind.company_count,
        active: appView.kind === "industry" && appView.id === ind.id, onSelect: () => go({ kind: "industry", id: ind.id }),
      })),
    },
    {
      label: "Research",
      items: [
        { id: "research", label: "Verified deep dive", icon: <ScanSearch size={18} />, active: appView.kind === "research", badge: loading ? <LoaderCircle size={11} className="spin" /> : undefined, onSelect: () => go({ kind: "research" }) },
        ...(appView.kind === "research" && report ? [
          { id: "claims", label: "Verified claims", icon: <ShieldCheck size={16} />, sub: true, badge: claims.length || undefined, onSelect: () => setActiveTab("claims") },
          { id: "sources", label: "Sources", icon: <BookOpen size={16} />, sub: true, onSelect: () => setActiveTab("sources") },
        ] : []),
      ],
    },
  ];

  return <div className="research-app">
    <Sidebar sections={navSections} collapsed={collapsed} onToggle={toggleSidebar} mobileOpen={sidebarOpen} onCloseMobile={closeSidebar}
      user={user} status={health?.demo_mode ? { tone: "amber", label: "Offline snapshot" } : { tone: "teal", label: "Verified data" }} />
    <main className="main">
      <header className="topbar"><button type="button" className="mobile-menu" aria-label="Open navigation" onClick={() => setSidebarOpen(true)}><Menu size={19} /></button><div className="breadcrumb"><span>{appView.kind === "industry" ? "Industries" : "Company research"}</span><ChevronRight size={14} /><strong>{appView.kind === "industry" ? (industries.find((i) => i.id === appView.id)?.name ?? "Industry") : "Verified deep dive"}</strong></div><div className="topbar-meta"><span><StatusDot tone={health?.demo_mode ? "amber" : "teal"} /> {health?.demo_mode ? "Offline snapshot" : "Verified data"}</span><UserMenu user={user} onSignOut={onSignOut} /></div></header>
      <div className="content-shell">
        {appView.kind === "industry" && <IndustryView key={appView.id} industryId={appView.id} onDeepDive={openDeepDive} />}
        {appView.kind === "research" && <div className="view-anim">
          <section className="hero"><div><div className="hero-kicker"><Sparkles size={14} /> VERIFIED REPORTS</div><h1>Verified deep dive</h1><p>One company, every claim traced to its source. Slower by design - each figure is checked by the LLM verifier.</p></div><div className="hero-note"><ShieldCheck size={17} /><span>Every factual claim is independently verified against its source.</span></div></section>
          <section className="query-panel"><div className="query-label"><Search size={15} /><label htmlFor="research-query">Company or research query</label><kbd>ENTER</kbd></div><div className="query-row"><input id="research-query" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => event.key === "Enter" && runResearch()} placeholder="e.g. Analyze Infosys revenue and risks" disabled={loading} /><Button variant="primary" onClick={() => runResearch()} disabled={loading}>{loading ? <><LoaderCircle size={15} className="spin" /> Running</> : <><Send size={15} /> Run research</>}</Button></div><div className="example-chips">{examples.map((example) => <button type="button" key={example} onClick={() => runResearch(example)} disabled={loading}>{example}</button>)}</div>{health && <div className={`mode-banner ${health.demo_mode ? "demo" : "live"}`}><StatusDot tone={health.demo_mode ? "amber" : "teal"} /><span>{health.demo_mode ? "Demo mode: sources are synthetic and explicitly labelled." : "Live mode: connected to real filings, financial data, and web sources."}</span></div>}</section>
          {loading && <Pipeline run={run} running />}
          {error && <div className="error-panel"><AlertTriangle size={18} /><div><strong>Research could not be completed</strong><span>{error}</span></div><button type="button" onClick={() => setError("")} aria-label="Dismiss error"><X size={16} /></button></div>}
          {run && report && <ResearchResults company={company} run={run} report={report} claims={claims} sources={sources} conflicts={conflicts} activeTab={activeTab} setActiveTab={setActiveTab} claimFilter={claimFilter} setClaimFilter={setClaimFilter} filteredClaims={filteredClaims} />}
          {!run && !loading && !error && <Pipeline />}
        </div>}
      </div>
    </main>
  </div>;
}

function ResearchResults({ company, run, report, claims, sources, conflicts, activeTab, setActiveTab, claimFilter, setClaimFilter, filteredClaims }: { company: string; run: ResearchRun; report: Report; claims: Claim[]; sources: Source[]; conflicts: Conflict[]; activeTab: Tab; setActiveTab: (tab: Tab) => void; claimFilter: ClaimFilter; setClaimFilter: (filter: ClaimFilter) => void; filteredClaims: Claim[] }) {
  return <section className="results"><div className="results-heading"><div><div className="company-line"><span className="company-monogram">{company.slice(0, 1).toUpperCase()}</span><span>{company}</span><StatusDot /></div><h2>Verified research brief</h2><p>{run.plan?.period || "Period not specified"} · completed {new Date(run.updated_at).toLocaleString()}</p></div><div className="result-actions"><span className="complete-status"><Check size={14} /> {run.status}</span><span className="run-id"><Clock3 size={13} /> {run.research_run_id}</span></div></div><div className="metrics-strip"><Metric label="Sources" value={String(sources.length)} detail="documents retrieved" /><Metric label="Claims" value={String(report.total_claims)} detail="extracted and checked" /><Metric label="Avg. confidence" value={report.average_confidence != null ? `${Math.round(report.average_confidence * 100)}%` : "—"} detail="weighted verification" tone="positive" /><Metric label="Conflicts" value={String(conflicts.length)} detail={conflicts.length ? "review recommended" : "none detected"} tone={conflicts.length ? "negative" : "neutral"} /></div><Tabs label="Research result sections" active={activeTab} onChange={setActiveTab} tabs={tabs.map(({ id, label, icon: Icon }) => ({ id, label, icon: <Icon size={15} />, count: id === "claims" ? claims.length : id === "conflicts" ? conflicts.length : undefined }))} /><div className="result-panel panel-anim" key={activeTab}>{activeTab === "overview" && <OverviewTab report={report} />}{activeTab === "financials" && <FinancialsTab claims={claims} section={report.financial_performance} />}{activeTab === "risks" && <ReportText title="Risks" section={report.risks} />}{activeTab === "findings" && <ReportText title="Important findings" section={report.important_findings} />}{activeTab === "claims" && <ClaimsTab claims={filteredClaims} filter={claimFilter} setFilter={setClaimFilter} sources={sources} />}{activeTab === "conflicts" && <ConflictsTab conflicts={conflicts} />}{activeTab === "sources" && <SourcesTab sources={sources} />}</div></section>;
}

function OverviewTab({ report }: { report: Report }) { return <div className="overview-content"><ReportText title="Executive overview" section={report.executive_overview} /><div className="verification-callout"><ShieldCheck size={19} /><div><strong>Claim verification summary</strong><p>{report.claim_verification_summary.content}</p><div className="verdicts"><span className="supported">{report.supported_claims} supported</span><span className="contradicted">{report.contradicted_claims} contradicted</span><span className="insufficient">{report.insufficient_claims} insufficient</span></div></div></div>{report.comparison_tables?.map((table) => <div className="comparison-block" key={table.title}><div className="section-title"><span className="eyebrow">Comparison</span><h3>{table.title}</h3></div><DataTable columns={table.columns} rows={table.rows} /></div>)}</div>; }
const METRIC_LABELS: Record<string, string> = {
  revenue: "Revenue", annual_revenue: "Annual revenue", revenue_ttm: "Revenue (TTM)",
  net_income: "Net income", net_income_ttm: "Net income (TTM)", operating_income: "Operating income",
  operating_margin: "Operating margin", operating_margin_ttm: "Operating margin (TTM)",
  profit_margin: "Profit margin", average_net_profit_margin: "Net profit margin",
  ebitda: "EBITDA", eps_diluted: "Diluted EPS", revenue_growth_yoy: "Revenue growth (YoY)",
  cash_and_equivalents: "Cash & equivalents", total_debt: "Total debt", long_term_debt: "Long-term debt",
  market_cap: "Market cap", pe_ratio: "P/E ratio",
};

/** Mirrors the backend's humanize_metric: raw XBRL tags are unreadable in a report. */
function metricLabel(metric: string): string {
  const key = metric.trim().toLowerCase();
  if (METRIC_LABELS[key]) return METRIC_LABELS[key];
  const fuzzy: [string, string][] = [
    ["revenuefromcontract", "Revenue"], ["netincome", "Net income"], ["operatingincome", "Operating income"],
    ["earningspershare", "Diluted EPS"], ["cashandcash", "Cash & equivalents"], ["longtermdebt", "Long-term debt"],
  ];
  for (const [needle, label] of fuzzy) if (key.includes(needle)) return label;
  const spaced = metric.replace(/_/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Renders 466822988000 as $466.82B and 0.326 as 32.6%. */
function metricValue(claim: Claim): string {
  const isPercent = claim.normalized?.base_unit === "%" || claim.unit === "%"
    || /margin|growth/i.test(claim.metric);
  const raw = String(claim.value).trim();
  if (isPercent) {
    const n = parseFloat(raw.replace("%", ""));
    if (Number.isNaN(n)) return raw;
    return `${(Math.abs(n) <= 1 ? n * 100 : n).toFixed(1)}%`;
  }
  const mag = claim.normalized?.magnitude;
  if (mag == null) return `${raw}${claim.unit ? ` ${claim.unit}` : ""}`;
  const base = claim.normalized?.base_unit ?? "";
  const sym = base === "INR" ? "₹" : base === "USD" || base === "" ? "$" : "";
  const abs = Math.abs(mag);
  for (const [cut, sfx] of [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]] as [number, string][]) {
    if (abs >= cut) return `${sym}${(mag / cut).toFixed(2)}${sfx}`;
  }
  return `${sym}${mag.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

const FINANCIAL_KEYS = ["revenue", "netincome", "operatingincome", "operatingmargin", "profitmargin", "ebitda", "revenuegrowth", "eps", "cash", "debt", "marketcap", "peratio"];

function FinancialsTab({ claims, section }: { claims: Claim[]; section: { content: string; claim_ids: string[] } }) {
  // Keep the strongest verdict per metric so one figure isn't listed many times.
  const rank = (s?: string | null) => (s === "supported" ? 3 : s === "contradicted" ? 2 : s === "insufficient" ? 1 : 0);
  const financial = claims.filter((c) => {
    if (c.claim_type !== "numeric") return false;
    const k = c.metric.toLowerCase().replace(/_/g, "");
    return FINANCIAL_KEYS.some((core) => k.includes(core));
  });
  const best = new Map<string, Claim>();
  for (const c of financial) {
    const key = metricLabel(c.metric);
    const prev = best.get(key);
    if (!prev || rank(c.verification_status) > rank(prev.verification_status) || (rank(c.verification_status) === rank(prev.verification_status) && (c.confidence ?? 0) > (prev.confidence ?? 0))) {
      best.set(key, c);
    }
  }
  const rows = [...best.values()].sort((a, b) => rank(b.verification_status) - rank(a.verification_status) || (b.confidence ?? 0) - (a.confidence ?? 0));

  if (rows.length === 0) {
    return <article className="report-text">
      <div className="section-title"><span className="eyebrow">Verified report section</span><h3>Financial performance</h3></div>
      <div className="empty-inline"><BarChart3 size={17} />{section.content || "No financial figures were extracted from the retrieved sources."}</div>
    </article>;
  }

  return <article className="report-text">
    <div className="section-title"><span className="eyebrow">Verified report section</span><h3>Financial performance</h3></div>
    <div className="fin-grid">
      {rows.map((c) => (
        <div className={`fin-card ${c.verification_status || "insufficient"}`} key={c.claim_id}>
          <span className="fin-metric">{metricLabel(c.metric)}</span>
          <strong className="fin-value">{metricValue(c)}</strong>
          <div className="fin-meta">
            {c.period && <span className="fin-period">{c.period}</span>}
            {c.basis && c.basis !== "unknown" && <span className="fin-basis">{c.basis.replace(/_/g, "-")}</span>}
          </div>
          <div className="fin-foot">
            <span className={`verdict ${c.verification_status || "insufficient"}`}>{c.verification_status || "unverified"}</span>
            {c.confidence != null && <span className="fin-conf">{Math.round(c.confidence * 100)}% confidence</span>}
          </div>
        </div>
      ))}
    </div>
    <p className="fin-note">
      Every figure above is shown with the verdict its own source evidence produced. Figures marked
      <strong> insufficient</strong> were retrieved but could not be tied to the requested period — they are not verified facts.
    </p>
  </article>;
}

function ReportText({ title, section }: { title: string; section: { title: string; content: string; claim_ids: string[] } }) { return <article className="report-text"><div className="section-title"><span className="eyebrow">Verified report section</span><h3>{title}</h3></div><p>{section.content || "Nothing to show."}</p>{section.claim_ids?.length > 0 && <span className="linked-claims"><ShieldCheck size={13} /> {section.claim_ids.length} linked verified claims</span>}</article>; }
function ClaimsTab({ claims, filter, setFilter, sources }: { claims: Claim[]; filter: ClaimFilter; setFilter: (filter: ClaimFilter) => void; sources: Source[] }) { return <div><div className="panel-heading"><div><span className="eyebrow">Evidence ledger</span><h3>Extracted and verified claims</h3></div><div className="claim-filters">{(["all", "supported", "contradicted", "insufficient"] as ClaimFilter[]).map((item) => <button type="button" key={item} className={filter === item ? "active" : ""} onClick={() => setFilter(item)}>{item}</button>)}</div></div>{claims.length ? claims.map((claim, index) => <ClaimCard claim={claim} key={claim.claim_id} index={index} sources={sources} />) : <div className="empty-inline"><Filter size={17} />No claims match this filter.</div>}</div>; }
function ClaimCard({ claim, index, sources }: { claim: Claim; index: number; sources: Source[] }) { const [open, setOpen] = useState(false); const source = sources.find((item) => item.source_id === claim.evidence_span.source_id); const confidence = claim.confidence == null ? null : Math.round(claim.confidence * 100); const breakdown = claim.confidence_breakdown ?? {}; return <article className="claim-card"><div className="claim-head"><div><strong>{claim.statement}</strong><span>{claim.entity} · {claim.metric}{claim.period ? ` · ${claim.period}` : ""}</span></div><span className={`verdict ${claim.verification_status || "insufficient"}`}>{claim.verification_status || "unverified"}</span></div>{confidence != null && <div className="confidence-row"><div><span style={{ width: `${confidence}%` }} /></div><b>{confidence}%</b></div>}<blockquote>“{claim.evidence_span.evidence_text}”</blockquote><div className="claim-source"><span>{source?.source_tier?.replaceAll("_", " ") || "unknown source"}</span><strong>{source?.title || "Unknown source"}</strong>{source?.publisher && <small>{source.publisher}</small>}</div><div className="claim-footer"><span>{claim.verification_reason || "Verification complete."}</span>{claim.confidence_breakdown && <button type="button" onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"} confidence breakdown <ChevronDown size={13} /></button>}</div>{open && <div className="breakdown-grid">{Object.entries(breakdown).map(([key, value]) => <div key={key}><span>{key.replaceAll("_", " ")}</span><strong>{Math.round(value * 100)}%</strong></div>)}</div>}<span className="claim-index">#{index + 1}</span></article>; }
function ConflictsTab({ conflicts }: { conflicts: Conflict[] }) { return conflicts.length ? <div><div className="panel-heading"><div><span className="eyebrow">Basis-aware comparison</span><h3>Conflicting information</h3></div><AlertTriangle size={18} className="warning" /></div><DataTable columns={["Metric", "Source A", "Source B", "Type", "Reason"]} rows={conflicts.map((item) => ({ Metric: item.metric, "Source A": item.value_a, "Source B": item.value_b, Type: item.is_genuine_conflict ? "Genuine conflict" : `Explained · ${item.reason_type}`, Reason: item.explanation }))} /></div> : <div className="empty-inline"><ShieldCheck size={17} />No conflicts were detected between sources.</div>; }
function SourcesTab({ sources }: { sources: Source[] }) { return <div><div className="panel-heading"><div><span className="eyebrow">Provenance library</span><h3>Retrieved source documents</h3></div><span className="source-count">{sources.length} sources</span></div><div className="source-list">{sources.length ? sources.map((source) => <article className="source-card" key={source.source_id}><div className="source-icon"><FileText size={17} /></div><div className="source-main"><strong>{source.title}</strong><span>{source.publisher} · Retrieved {new Date(source.retrieved_at).toLocaleString()}</span>{source.url && <a href={source.url} target="_blank" rel="noreferrer">Open original source <ArrowUpRight size={13} /></a>}</div><span className="source-tier">{source.source_tier.replaceAll("_", " ")}</span></article>) : <div className="empty-inline">No sources were retrieved.</div>}</div></div>; }
function DataTable({ columns, rows }: { columns: string[]; rows: Record<string, unknown>[] }) { return <div className="table-wrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{columns.map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}</tr>)}</tbody></table></div>; }

export default App;
