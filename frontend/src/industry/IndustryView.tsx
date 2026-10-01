import { useCallback, useEffect, useMemo, useState } from "react";
import "./industry.css";
import {
  AlertTriangle, ArrowDown, ArrowUp, ArrowUpRight, BadgeCheck, BarChart3, ChevronRight, CircleHelp, Gauge, Layers, LoaderCircle,
  PieChart, RefreshCw, ScanSearch, ShieldCheck, Sparkles, TrendingUp, Wallet, X,
} from "lucide-react";
import { industryApi, type CompanyMetrics, type IndustrySnapshot } from "../services/api";
import { ago, inr, pct, plain, times } from "../lib/format";
import { useCountUp } from "../lib/motion";
import { Tabs } from "../components/Tabs";
import { Carousel } from "../components/Carousel";
import { DotStrip, QuarterBars, ShareBars } from "./charts";
import { DETAIL_METRICS, METRICS, POSITIONING_METRICS, TABLE_COLUMNS, median, rank, value, type MetricKey } from "./metrics";

type View = "peers" | "positioning" | "share";

const VIEWS: { id: View; label: string; icon: typeof BarChart3 }[] = [
  { id: "peers", label: "Peer table", icon: BarChart3 },
  { id: "positioning", label: "Positioning", icon: Gauge },
  { id: "share", label: "Revenue share", icon: PieChart },
];

const INSIGHT_ICON: Record<string, typeof BarChart3> = {
  concentration: PieChart, growth: TrendingUp, profitability: Wallet, valuation: Gauge,
  earnings: Sparkles, data: ShieldCheck,
};

const fmtDate = (iso: string) => new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });

const REFRESH_POLL_MS = 3000;
const MAX_REFRESH_POLLS = 8;

export function IndustryView({ industryId, onDeepDive }: { industryId: string; onDeepDive: (query: string) => void }) {
  const [snap, setSnap] = useState<IndustrySnapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<View>("peers");
  const [selected, setSelected] = useState<string | null>(null);
  const [, setTick] = useState(0);

  const load = useCallback(async (refresh = false) => {
    setBusy(true); setError("");
    try {
      let next = await industryApi.snapshot(industryId, refresh);
      setSnap(next);
      // Stale-while-revalidate: the server answered from cache and is
      // refreshing in the background - pick up the fresh copy when ready.
      for (let i = 0; next.refreshing && i < MAX_REFRESH_POLLS; i += 1) {
        await new Promise((r) => setTimeout(r, REFRESH_POLL_MS));
        next = await industryApi.snapshot(industryId);
        setSnap(next);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally { setBusy(false); }
  }, [industryId]);

  useEffect(() => { load(); }, [load]);
  // Keep the "updated N min ago" label honest without refetching.
  useEffect(() => { const id = setInterval(() => setTick((t) => t + 1), 30000); return () => clearInterval(id); }, []);

  const companies = useMemo(() => (snap?.companies ?? []).filter((c) => c.available), [snap]);
  const selectedCompany = companies.find((c) => c.symbol === selected) ?? null;

  if (!snap) {
    return error
      ? <div className="error-panel"><AlertTriangle size={18} /><div><strong>Industry data could not be loaded</strong><span>{error}</span></div><button type="button" onClick={() => load()} aria-label="Retry"><RefreshCw size={16} /></button></div>
      : <IndustrySkeleton />;
  }

  const agg = snap.aggregates;
  const verified = companies.filter((c) => c.verification_status === "verified").length;

  return (
    <div className="industry">
      <section className="ind-hero">
        <div>
          <div className="hero-kicker"><Layers size={14} /> INDUSTRY · {snap.industry.universe.toUpperCase()}</div>
          <h1>{snap.industry.name}</h1>
          <p>{snap.industry.description}</p>
        </div>
        <div className="ind-status">
          <span className={`data-pill ${snap.mode}`}>
            <span className={`status-dot ${snap.mode === "demo" ? "status-amber" : ""}`} />
            {snap.mode === "demo"
              ? "Offline · stored filings"
              : snap.refreshing ? "Checking for new filings…" : `From the database · checked ${snap.synced_at ? ago(snap.synced_at + (snap.synced_at.endsWith("Z") ? "" : "Z")) : "—"}`}
          </span>
          {snap.mode === "live" && (
            <button type="button" className="button button-secondary" onClick={() => load(true)} disabled={busy}>
              {busy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />} Check for new filings
            </button>
          )}
        </div>
      </section>

      {error && <div className="error-panel"><AlertTriangle size={18} /><div><strong>Refresh failed - showing the last loaded data</strong><span>{error}</span></div><button type="button" onClick={() => setError("")} aria-label="Dismiss"><X size={16} /></button></div>}
      {snap.warnings.length > 0 && <div className="warn-strip"><AlertTriangle size={14} /><span>{snap.warnings.join(" · ")}</span></div>}

      <section className="kpi-row" aria-label="Industry summary">
        <Kpi label="Industry revenue (TTM)" num={snap.concentration.total_revenue} fmt={inr} detail={`${companies.length} companies · ${snap.concentration.largest} ${pct(snap.concentration.largest_share, 0)}`} />
        <Kpi label="Median revenue growth" num={agg.revenue_growth_yoy?.median} fmt={(v) => pct(v, 1, true)} detail={`range ${pct(agg.revenue_growth_yoy?.min, 0)} to ${pct(agg.revenue_growth_yoy?.max, 0)}`} />
        <Kpi label="Median operating margin" num={agg.operating_margin?.median} fmt={(v) => pct(v)} detail={`${pct(agg.operating_margin?.weighted_mean)} industry-wide`} />
        <Kpi label="Median net margin" num={agg.profit_margin?.median} fmt={(v) => pct(v)} detail={`${pct(agg.profit_margin?.weighted_mean)} industry-wide`} />
        <Kpi label="Median profit growth" num={agg.earnings_growth_yoy?.median} fmt={(v) => pct(v, 1, true)} detail={`range ${pct(agg.earnings_growth_yoy?.min, 0)} to ${pct(agg.earnings_growth_yoy?.max, 0)}`} />
      </section>

      <section className="insights" aria-label="Key takeaways">
        <div className="section-title insights-title"><span className="eyebrow">Key takeaways · computed from the data, not generated</span></div>
        <Carousel label="takeaways" count={snap.insights.length}>
          {snap.insights.map((item, i) => {
            const Icon = INSIGHT_ICON[item.kind] ?? Sparkles;
            return (
              <article className={`insight ${item.kind}`} key={item.title} style={{ ["--i" as string]: i }}>
                <span className="insight-icon"><Icon size={16} /></span>
                <div><span className="insight-kind">{item.kind}</span><strong>{item.title}</strong><p>{item.detail}</p></div>
              </article>
            );
          })}
        </Carousel>
      </section>

      <div className="ind-tabs">
        <Tabs label="Industry views" active={view} onChange={setView} tabs={VIEWS.map(({ id, label, icon: Icon }) => ({ id, label, icon: <Icon size={15} /> }))} />
      </div>

      <div className="result-panel panel-anim" key={view}>
        {view === "peers" && <PeerTable companies={companies} snap={snap} onSelect={setSelected} selected={selected} />}
        {view === "positioning" && <Positioning companies={companies} selected={selected} onSelect={setSelected} />}
        {view === "share" && <ConcentrationPanel snap={snap} companies={companies} selected={selected} onSelect={setSelected} />}
      </div>

      <p className="ind-footnote">
        Every figure comes from the companies' own reported quarterly results (consolidated), stored in the VeriFi database - each company's panel links to its source filings.
        {" "}{verified}/{companies.length} companies pass every consistency check. Answered from the database in {snap.fetch_seconds != null ? Math.round(snap.fetch_seconds * 1000) : "—"} ms. Not investment advice.
      </p>

      {selectedCompany && <CompanyPanel company={selectedCompany} companies={companies} snap={snap} onClose={() => setSelected(null)} onDeepDive={onDeepDive} />}
    </div>
  );
}

function Kpi({ label, num, fmt, detail }: { label: string; num?: number | null; fmt: (v: number | null | undefined) => string; detail: string }) {
  // Counts up to the real value; the final frame is exactly fmt(num).
  const shown = useCountUp(num ?? null);
  return <div className="metric kpi"><span className="eyebrow">{label}</span><strong aria-label={fmt(num)}>{fmt(shown)}</strong><span className="metric-detail">{detail}</span></div>;
}

function IndustrySkeleton() {
  return (
    <div className="industry" aria-busy="true" aria-label="Loading industry data">
      <div className="skel skel-title" /><div className="skel skel-line" />
      <div className="kpi-row">{Array.from({ length: 5 }, (_, i) => <div className="metric kpi skel-box" key={i} />)}</div>
      <div className="skel skel-panel" />
    </div>
  );
}

function VerifyBadge({ status }: { status: CompanyMetrics["verification_status"] }) {
  if (status === "verified") return <span className="vbadge ok" title="Every cross-check passed"><BadgeCheck size={13} /><span className="sr-only">verified</span></span>;
  if (status === "mismatch") return <span className="vbadge bad" title="At least one figure disagrees with reported statements"><AlertTriangle size={13} /><span className="sr-only">mismatch</span></span>;
  return <span className="vbadge na" title="Not enough reported data to cross-check"><CircleHelp size={13} /><span className="sr-only">unverified</span></span>;
}

// ---- Peer table -------------------------------------------------------------------

function PeerTable({ companies, snap, onSelect, selected }: { companies: CompanyMetrics[]; snap: IndustrySnapshot; onSelect: (s: string) => void; selected: string | null }) {
  const [sortKey, setSortKey] = useState<MetricKey>("revenue_ttm");
  const [desc, setDesc] = useState(true);
  const rows = useMemo(() => [...companies].sort((a, b) => {
    const av = value(a, sortKey), bv = value(b, sortKey);
    if (av === null) return 1;
    if (bv === null) return -1;
    return desc ? bv - av : av - bv;
  }), [companies, sortKey, desc]);
  const sortBy = (key: MetricKey) => { if (key === sortKey) setDesc(!desc); else { setSortKey(key); setDesc(METRICS[key].better !== false); } };
  const best = (key: MetricKey) => {
    const def = METRICS[key];
    if (def.better === null) return null;
    const vals = companies.map((c) => value(c, key)).filter((v): v is number => v !== null);
    return vals.length ? (def.better ? Math.max(...vals) : Math.min(...vals)) : null;
  };

  return (
    <div>
      <div className="panel-heading">
        <div><span className="eyebrow">Side by side</span><h3>Peer comparison</h3></div>
        <span className="source-count">Click a column to sort · a row for detail · <b className="best-key">bold</b> = best in group</span>
      </div>
      <div className="table-wrap peer-wrap">
        <table className="peer-table">
          <thead>
            <tr>
              <th className="sticky-col">Company</th>
              {TABLE_COLUMNS.map((key) => (
                <th key={key} className="num" aria-sort={sortKey === key ? (desc ? "descending" : "ascending") : "none"}>
                  <button type="button" onClick={() => sortBy(key)} title={METRICS[key].help}>
                    {METRICS[key].short}{sortKey === key && (desc ? <ArrowDown size={11} /> : <ArrowUp size={11} />)}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.symbol} className={selected === c.symbol ? "selected" : ""} onClick={() => onSelect(c.symbol)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onSelect(c.symbol)}>
                <td className="sticky-col">
                  <div className="co-cell"><span className="co-mono">{c.short_name.slice(0, 2).toUpperCase()}</span><div><strong>{c.short_name}</strong><small>{c.nse} · {c.tier}</small></div><VerifyBadge status={c.verification_status} /></div>
                </td>
                {TABLE_COLUMNS.map((key) => {
                  const v = value(c, key);
                  return <td key={key} className={`num ${v !== null && v === best(key) ? "is-best" : ""}`}>{METRICS[key].format(v)}</td>;
                })}
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td className="sticky-col">Industry median</td>
              {TABLE_COLUMNS.map((key) => {
                const agg = snap.aggregates[key];
                const med = agg?.median ?? median(companies.map((c) => value(c, key)).filter((v): v is number => v !== null));
                return <td key={key} className="num">{key === "revenue_share" ? pct(1 / companies.length) : METRICS[key].format(med)}</td>;
              })}
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}

// ---- Positioning (dot strips) -------------------------------------------------------

function Positioning({ companies, selected, onSelect }: { companies: CompanyMetrics[]; selected: string | null; onSelect: (s: string) => void }) {
  const [focus, setFocus] = useState<string | null>(selected);
  const active = focus ?? selected;
  return (
    <div>
      <div className="panel-heading">
        <div><span className="eyebrow">Where each company sits</span><h3>Peer positioning</h3></div>
        <div className="claim-filters focus-chips" role="group" aria-label="Highlight a company">
          {companies.map((c) => (
            <button type="button" key={c.symbol} className={active === c.symbol ? "active" : ""} aria-pressed={active === c.symbol}
              onClick={() => setFocus(active === c.symbol ? null : c.symbol)}>{c.short_name}</button>
          ))}
        </div>
      </div>
      <p className="panel-lede">Each row is one metric; each dot a company. The tick marks the peer median. Pick a company above to follow it across every metric.</p>
      <div className="strips">
        {POSITIONING_METRICS.map((m) => <DotStrip key={m} metric={m} companies={companies} selected={active} onSelect={onSelect} />)}
      </div>
    </div>
  );
}

// ---- Concentration -----------------------------------------------------------------------

function ConcentrationPanel({ snap, companies, selected, onSelect }: { snap: IndustrySnapshot; companies: CompanyMetrics[]; selected: string | null; onSelect: (s: string) => void }) {
  const c = snap.concentration;
  const hhiPts = c.hhi != null ? Math.round(c.hhi * 10000) : null;
  const band = hhiPts == null ? "" : hhiPts >= 2500 ? "highly concentrated" : hhiPts >= 1500 ? "moderately concentrated" : "unconcentrated";
  return (
    <div className="split">
      <div>
        <div className="panel-heading"><div><span className="eyebrow">Who earns the revenue</span><h3>Share of industry revenue</h3></div></div>
        <ShareBars companies={companies} selected={selected} onSelect={onSelect} />
      </div>
      <aside className="stat-stack">
        <Stat label="Top-3 share" value={pct(c.top3_share, 0)} note="of the ten companies' combined revenue is earned by the three largest." />
        <Stat label="Effective number of companies" value={plain(c.effective_companies, 1)} note={`Revenue is spread as if across ${plain(c.effective_companies, 1)} equal-sized companies, not ${companies.length}.`} />
        <Stat label="Herfindahl-Hirschman index" value={hhiPts != null ? hhiPts.toLocaleString("en-IN") : "—"} note={`On the 0–10,000 scale competition regulators use: ${band}.`} />
      </aside>
    </div>
  );
}

function Stat({ label, value: v, note }: { label: string; value: string; note: string }) {
  return <div className="stat"><span className="eyebrow">{label}</span><strong>{v}</strong><p>{note}</p></div>;
}

// ---- Company detail panel ---------------------------------------------------------------

function CompanyPanel({ company: c, companies, snap, onClose, onDeepDive }: { company: CompanyMetrics; companies: CompanyMetrics[]; snap: IndustrySnapshot; onClose: () => void; onDeepDive: (q: string) => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      <div className="drawer-overlay" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={`${c.name} detail`}>
        <header className="drawer-head">
          <div className="co-cell"><span className="co-mono lg">{c.short_name.slice(0, 2).toUpperCase()}</span><div><strong>{c.name}</strong><small>{c.nse} · {c.tier} · latest quarter {c.latest_quarter ? fmtDate(c.latest_quarter) : "—"}</small></div></div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close"><X size={18} /></button>
        </header>

        <div className="drawer-body">
          <div className="drawer-top">
            <div><span className="eyebrow">Revenue (TTM)</span><strong>{inr(c.revenue_ttm)}</strong><small>{pct(c.revenue_share)} of industry revenue</small></div>
            <div><span className="eyebrow">Net profit (TTM)</span><strong>{inr(c.net_income_ttm)}</strong><small>{pct(c.profit_margin)} net margin</small></div>
            <div><span className="eyebrow">Shares in issue</span><strong>{c.shares_outstanding ? (c.shares_outstanding / 1e7).toLocaleString("en-IN", { maximumFractionDigits: 2 }) + " Cr" : "—"}</strong><small>from latest filing</small></div>
          </div>

          <section>
            <div className="section-title"><span className="eyebrow">Versus {companies.length - 1} peers</span></div>
            <div className="vs-grid">
              {DETAIL_METRICS.map((key) => {
                const def = METRICS[key];
                const v = value(c, key);
                const med = snap.aggregates[key]?.median ?? median(companies.map((x) => value(x, key)).filter((n): n is number => n !== null));
                const r = rank(companies, c, key);
                const above = v !== null && med != null ? v > med : null;
                const good = above === null || def.better === null ? null : above === def.better;
                return (
                  <div className="vs-cell" key={key} title={def.help}>
                    <span>{def.label}</span>
                    <strong>{def.format(v)}</strong>
                    <small>
                      median {def.format(med)}
                      {r && <em className={good === null ? "" : good ? "up" : "down"}>{good === null ? "" : good ? "▲" : "▼"} #{r.rank} of {r.of}</em>}
                    </small>
                  </div>
                );
              })}
            </div>
          </section>

          <section>
            <div className="section-title"><span className="eyebrow">Quarterly revenue · consolidated, as filed</span></div>
            <QuarterBars quarters={c.quarters} />
          </section>

          <section>
            <div className="section-title"><span className="eyebrow">Source filings · the story behind these numbers</span></div>
            <ul className="filing-list">
              {[...c.quarters].reverse().map((q) => (
                <li key={q.period_end}>
                  <div><strong>Quarter ended {fmtDate(q.period_end)}</strong><small>{q.audited ?? "—"} · filed {q.filed_at ? q.filed_at.slice(0, 11) : "—"}</small></div>
                  {q.filing_url && <a href={q.filing_url} target="_blank" rel="noreferrer">XBRL <ArrowUpRight size={12} /></a>}
                </li>
              ))}
            </ul>
          </section>

          <section>
            <div className="section-title"><span className="eyebrow">Cross-checks · deterministic, no LLM</span></div>
            <ul className="check-list">
              {c.checks.map((k) => (
                <li key={k.metric + k.label} className={k.informational ? "info" : k.status}>
                  {k.informational ? <CircleHelp size={15} /> : k.status === "verified" ? <BadgeCheck size={15} /> : k.status === "mismatch" ? <AlertTriangle size={15} /> : <CircleHelp size={15} />}
                  <div><strong>{k.label}</strong><p>{k.detail}</p></div>
                </li>
              ))}
            </ul>
          </section>
        </div>

        <footer className="drawer-foot">
          <p>Want source-level evidence - filings, news and risks, each claim checked against its source?</p>
          <button type="button" className="button button-primary" onClick={() => onDeepDive(`Analyze ${c.name} revenue, profitability and risks`)}>
            <ScanSearch size={15} /> Run verified deep dive <ChevronRight size={14} />
          </button>
        </footer>
      </aside>
    </>
  );
}
