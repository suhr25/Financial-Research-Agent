import { useState } from "react";
import { AlertTriangle, ArrowUpRight, BadgeCheck, CircleHelp, Database, FileText, Layers, ScanSearch, Trophy, Zap } from "lucide-react";
import type { CompanyMetrics, ComparisonRow, DatabaseAnswer as Answer } from "../services/api";
import { ago, inr, pct } from "../lib/format";
import { QuarterBars } from "../industry/charts";
import "./answer.css";

const fmtDate = (iso: string) => new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });

function fmt(kind: ComparisonRow["kind"], v: number | null | undefined): string {
  if (v == null) return "—";
  if (kind === "inr") return inr(v);
  if (kind === "pct") return pct(v);
  if (kind === "pct_signed") return pct(v, 1, true);
  return "₹" + v.toFixed(2);
}

/** A question answered from stored, verified filings - every figure is
 * labelled with its company and links back to the filing it came from. */
export function DatabaseAnswer({ answer, onRunResearch, onOpenIndustry }: {
  answer: Answer;
  onRunResearch: () => void;
  onOpenIndustry: () => void;
}) {
  const cos = answer.companies.filter((c) => c.available);
  const title = cos.map((c) => c.short_name).join(" vs ");
  const focus = new Set(answer.focus_metrics);
  const rows = [...answer.comparison].sort((a, b) => Number(focus.has(b.metric)) - Number(focus.has(a.metric)));
  // One shared scale for every company's quarterly chart, so a 3x larger
  // business has 3x taller bars.
  const sharedMax = Math.max(...cos.flatMap((c) => c.quarters.map((q) => q.revenue ?? 0)), 1);
  const synced = answer.synced_at ? ago(answer.synced_at.endsWith("Z") ? answer.synced_at : answer.synced_at + "Z") : null;

  return (
    <section className="dba" aria-label="Answer">
      <header className="dba-head">
        <div>
          <div className="dba-source"><Database size={13} /> Answered from the VeriFi database <span className="dba-speed"><Zap size={12} /> {answer.elapsed_ms} ms</span></div>
          <h2>{answer.intent === "comparison" ? title : `${cos[0]?.name ?? title}`}</h2>
          <p>
            {answer.intent === "comparison" ? "Side-by-side, from each company's own reported results" : "From the company's own reported results"}
            {" · "}{answer.coverage}{synced ? ` · checked against the source ${synced}` : ""}
          </p>
        </div>
        <span className="dba-verified"><BadgeCheck size={15} /> Verified figures</span>
      </header>

      {answer.warnings.length > 0 && <div className="warn-strip"><AlertTriangle size={14} /><span>{answer.warnings.join(" · ")}</span></div>}

      <ol className="dba-summary">
        {answer.summary.map((line, i) => <li key={i} style={{ ["--i" as string]: i }}>{line}</li>)}
      </ol>

      {answer.period_label && (
        <PeriodBlock answer={answer} companies={cos} />
      )}

      {answer.intent === "comparison" ? (
        <ComparisonTable rows={rows} companies={cos} focus={focus} />
      ) : (
        <MetricGrid rows={rows} company={cos[0]} focus={focus} />
      )}

      <section className="dba-block">
        <div className="section-title"><span className="eyebrow">Quarterly revenue · as filed{cos.length > 1 ? " · same scale" : ""}</span></div>
        <div className={`dba-trends n${cos.length}`}>
          {cos.map((c) => (
            <div className="dba-trend" key={c.nse}>
              <strong>{c.short_name}</strong>
              <QuarterBars quarters={c.quarters} scaleMax={cos.length > 1 ? sharedMax : undefined} />
            </div>
          ))}
        </div>
      </section>

      <section className="dba-block">
        <div className="section-title"><span className="eyebrow">Where every number comes from</span></div>
        <div className={`dba-proof n${cos.length}`}>
          {cos.map((c) => <Proof key={c.nse} company={c} />)}
        </div>
      </section>

      <div className={`dba-next ${answer.wants_qualitative ? "strong" : ""}`}>
        <div>
          <strong>{answer.wants_qualitative ? `The database holds reported financials - not ${answer.qualitative_terms.join(", ")}.` : "Need news, risks or management commentary?"}</strong>
          <p>Full research reads live sources (news, filings, coverage) and checks every claim against its source. It takes a few minutes the first time; the same question asked again is answered instantly from the database.</p>
        </div>
        <div className="dba-next-actions">
          <button type="button" className="button button-primary" onClick={onRunResearch}><ScanSearch size={15} /> Run full research</button>
          <button type="button" className="button button-secondary" onClick={onOpenIndustry}><Layers size={15} /> Industry view</button>
        </div>
      </div>
    </section>
  );
}

function PeriodBlock({ answer, companies }: { answer: Answer; companies: CompanyMetrics[] }) {
  return (
    <section className="dba-block dba-period">
      <div className="section-title"><span className="eyebrow">The period you asked about</span><h3>{answer.period_label}</h3></div>
      <div className="table-wrap">
        <table className="dba-table">
          <thead><tr><th>Company</th><th className="num">Revenue</th><th className="num">Net profit</th><th className="num">Operating profit</th><th className="num">EPS (diluted)</th><th>Source</th></tr></thead>
          <tbody>
            {companies.map((c) => {
              const pf = answer.period_figures[c.nse];
              if (!pf) return null;
              return pf.found ? (
                <tr key={c.nse}>
                  <td><strong>{c.short_name}</strong></td>
                  <td className="num">{inr(pf.revenue)}</td>
                  <td className="num">{inr(pf.net_income)}</td>
                  <td className="num">{inr(pf.operating_profit)}</td>
                  <td className="num">{pf.eps_diluted != null ? "₹" + pf.eps_diluted.toFixed(2) : "—"}</td>
                  <td>{pf.filing_url ? <a href={pf.filing_url} target="_blank" rel="noreferrer">Filing <ArrowUpRight size={12} /></a> : "—"}</td>
                </tr>
              ) : (
                <tr key={c.nse}><td><strong>{c.short_name}</strong></td><td colSpan={5} className="dba-missing"><CircleHelp size={14} /> {pf.message}</td></tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ComparisonTable({ rows, companies, focus }: { rows: ComparisonRow[]; companies: CompanyMetrics[]; focus: Set<string> }) {
  return (
    <section className="dba-block">
      <div className="section-title"><span className="eyebrow">Side by side</span></div>
      <div className="table-wrap">
        <table className="dba-table compare">
          <thead>
            <tr>
              <th>Figure</th>
              {companies.map((c) => <th key={c.nse} className="num">{c.short_name}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const vals = companies.map((c) => row.values[c.nse]).filter((v): v is number => v != null);
              const max = Math.max(...vals.map(Math.abs), 1e-9);
              return (
                <tr key={row.metric} className={focus.has(row.metric) ? "focus" : ""}>
                  <td>{row.label}</td>
                  {companies.map((c) => {
                    const v = row.values[c.nse];
                    const lead = row.leader === c.nse;
                    return (
                      <td key={c.nse} className={`num ${lead ? "lead" : ""}`}>
                        <span className="dba-val">{lead && <Trophy size={12} aria-label="leads" />}{fmt(row.kind, v)}</span>
                        {(row.kind === "inr" || row.kind === "pct") && v != null && (
                          <span className="dba-bar" aria-hidden="true"><i style={{ width: `${(Math.abs(v) / max) * 100}%` }} /></span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function MetricGrid({ rows, company, focus }: { rows: ComparisonRow[]; company: CompanyMetrics; focus: Set<string> }) {
  return (
    <section className="dba-block">
      <div className="section-title"><span className="eyebrow">Key figures</span></div>
      <div className="dba-grid">
        {rows.map((row) => (
          <div key={row.metric} className={`dba-metric ${focus.has(row.metric) ? "focus" : ""}`}>
            <span>{row.label}</span>
            <strong>{fmt(row.kind, row.values[company.nse])}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

function Proof({ company: c }: { company: CompanyMetrics }) {
  const [open, setOpen] = useState(false);
  const checks = c.checks.filter((k) => !k.informational);
  const notes = c.checks.filter((k) => k.informational);
  return (
    <div className="dba-proof-card">
      <div className="dba-proof-head">
        <strong>{c.short_name}</strong>
        <span className={`vstat ${c.verification_status}`}>
          {c.verification_status === "verified" ? <BadgeCheck size={13} /> : <AlertTriangle size={13} />}
          {c.verification_status === "verified" ? "All checks pass" : c.verification_status === "mismatch" ? "A check disagrees" : "Not enough data"}
        </span>
      </div>
      <ul className="dba-checks">
        {checks.map((k) => <li key={k.label} className={k.status}><span>{k.label}</span><em>{k.difference_pct != null ? `Δ ${k.difference_pct.toFixed(2)}%` : k.status}</em></li>)}
      </ul>
      {notes.length > 0 && <p className="dba-note"><AlertTriangle size={12} /> {notes.length} correction{notes.length > 1 ? "s" : ""} to the company's own filing tags - shown in the industry view.</p>}
      <button type="button" className="dba-filings-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
        <FileText size={13} /> {c.quarters.filter((q) => q.filing_url).length} source filings {open ? "▲" : "▼"}
      </button>
      {open && (
        <ul className="dba-filings">
          {[...c.quarters].reverse().map((q) => (
            <li key={q.period_end}>
              <span>Quarter ended {fmtDate(q.period_end)}</span>
              {q.filing_url && <a href={q.filing_url} target="_blank" rel="noreferrer">XBRL <ArrowUpRight size={11} /></a>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
