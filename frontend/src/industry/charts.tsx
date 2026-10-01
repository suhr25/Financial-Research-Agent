import { useRef, useState, type ReactNode } from "react";
import type { CompanyMetrics, QuarterPoint } from "../services/api";
import { inr, pct } from "../lib/format";
import { METRICS, median, value, type MetricKey } from "./metrics";

/* Charts are plain HTML/CSS positioned by percentage rather than SVG with a
   fixed viewBox, so they reflow to any width (down to phone) without
   distorting text. One series each, so a single accent colour carries
   the marks; identity comes from direct labels and tooltips, never hue. */

// ---- Tooltip ---------------------------------------------------------------

interface Tip { x: number; y: number; content: ReactNode }

function useTooltip() {
  const ref = useRef<HTMLDivElement>(null);
  const [tip, setTip] = useState<Tip | null>(null);
  const show = (event: React.MouseEvent | React.FocusEvent, content: ReactNode) => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const target = (event.target as HTMLElement).getBoundingClientRect();
    const clientX = "clientX" in event ? event.clientX : target.left + target.width / 2;
    const clientY = "clientY" in event ? event.clientY : target.top;
    setTip({ x: clientX - box.left, y: clientY - box.top, content });
  };
  const hide = () => setTip(null);
  const node = tip ? (
    <div className={`viz-tip ${tip.x > (ref.current?.clientWidth ?? 0) * 0.6 ? "flip" : ""}`} style={{ left: tip.x, top: tip.y }} role="status">
      {tip.content}
    </div>
  ) : null;
  return { ref, show, hide, node };
}

function TipRows({ title, rows }: { title: string; rows: [string, string][] }) {
  return <><strong>{title}</strong>{rows.map(([k, v]) => <span key={k}><em>{k}</em>{v}</span>)}</>;
}

// ---- Revenue share bars ----------------------------------------------------

export function ShareBars({ companies, selected, onSelect }: { companies: CompanyMetrics[]; selected?: string | null; onSelect: (symbol: string) => void }) {
  const tip = useTooltip();
  const rows = companies.filter((c) => c.revenue_share != null).sort((a, b) => (b.revenue_share ?? 0) - (a.revenue_share ?? 0));
  const max = Math.max(...rows.map((c) => c.revenue_share ?? 0), 0.0001);
  let cumulative = 0;
  return (
    <div className="share-bars" ref={tip.ref}>
      {rows.map((c) => {
        cumulative += c.revenue_share ?? 0;
        const cum = cumulative;
        return (
          <button type="button" key={c.symbol} className={`share-row ${selected === c.symbol ? "selected" : ""}`} onClick={() => onSelect(c.symbol)}
            onMouseMove={(e) => tip.show(e, <TipRows title={c.name} rows={[["Revenue (TTM)", inr(c.revenue_ttm)], ["Share of industry", pct(c.revenue_share)], ["Cumulative", pct(cum, 0)]]} />)}
            onMouseLeave={tip.hide} onFocus={(e) => tip.show(e, <TipRows title={c.name} rows={[["Share of industry", pct(c.revenue_share)]]} />)} onBlur={tip.hide}>
            <span className="share-name">{c.short_name}</span>
            <span className="share-track"><span className="share-fill" style={{ width: `${((c.revenue_share ?? 0) / max) * 100}%` }} /></span>
            <span className="share-value">{pct(c.revenue_share)}</span>
          </button>
        );
      })}
      {tip.node}
    </div>
  );
}

// ---- Dot strip (one metric, all peers) ------------------------------------------

export function DotStrip({ metric, companies, selected, onSelect }: { metric: MetricKey; companies: CompanyMetrics[]; selected?: string | null; onSelect: (symbol: string) => void }) {
  const tip = useTooltip();
  const def = METRICS[metric];
  const pts = companies.map((c) => ({ c, v: value(c, metric) })).filter((p): p is { c: CompanyMetrics; v: number } => p.v !== null);
  if (pts.length < 2) return null;
  const vals = pts.map((p) => p.v);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.06 || Math.abs(hi) * 0.1 || 1;
  const x = (v: number) => ((v - (lo - pad)) / (hi - lo + 2 * pad)) * 100;
  const med = median(vals)!;
  const best = def.better === null ? null : pts.reduce((a, b) => (def.better ? (b.v > a.v ? b : a) : (b.v < a.v ? b : a)));
  const sel = pts.find((p) => p.c.symbol === selected);
  // Label the best performer and the selected company only - selective, not one per dot.
  const labelled = [best, sel].filter((p, i, arr): p is { c: CompanyMetrics; v: number } => !!p && arr.findIndex((q) => q?.c.symbol === p.c.symbol) === i);

  return (
    <div className="strip">
      <div className="strip-head">
        <span className="strip-label" title={def.help}>{def.label}</span>
        <span className="strip-median">median {def.format(med)}</span>
      </div>
      <div className="strip-plot" ref={tip.ref}>
        <span className="strip-axis" />
        {lo < 0 && hi > 0 && <span className="strip-zero" style={{ left: `${x(0)}%` }} />}
        <span className="strip-med" style={{ left: `${x(med)}%` }} aria-hidden="true" />
        {pts.map(({ c, v }) => (
          <button type="button" key={c.symbol} aria-label={`${c.name}: ${def.format(v)}`}
            className={`strip-dot ${c.symbol === selected ? "selected" : ""} ${best?.c.symbol === c.symbol ? "best" : ""}`}
            style={{ left: `${x(v)}%` }} onClick={() => onSelect(c.symbol)}
            onMouseEnter={(e) => tip.show(e, <TipRows title={c.name} rows={[[def.label, def.format(v)], ["Peer median", def.format(med)]]} />)}
            onMouseLeave={tip.hide} onFocus={(e) => tip.show(e, <TipRows title={c.name} rows={[[def.label, def.format(v)]]} />)} onBlur={tip.hide} />
        ))}
        {labelled.map(({ c, v }) => (
          <span key={`l-${c.symbol}`} className={`strip-tag ${x(v) > 82 ? "end" : x(v) < 18 ? "start" : ""} ${c.symbol === selected ? "selected" : ""}`} style={{ left: `${x(v)}%` }}>
            {c.short_name} {def.format(v)}
          </span>
        ))}
        {tip.node}
      </div>
      <div className="strip-scale"><span>{def.format(lo)}</span><span>{def.format(hi)}</span></div>
    </div>
  );
}

// ---- Quarterly revenue bars --------------------------------------------------------

/** scaleMax: pass a shared maximum when several companies are shown side by
 * side, so bar heights are comparable across charts (not each self-scaled). */
export function QuarterBars({ quarters, scaleMax }: { quarters: QuarterPoint[]; scaleMax?: number }) {
  const tip = useTooltip();
  const pts = quarters.filter((q) => q.revenue != null);
  if (!pts.length) return <p className="muted-note">No quarterly statements reported.</p>;
  const max = scaleMax ?? Math.max(...pts.map((q) => q.revenue!));
  const label = (d: string) => new Date(d).toLocaleDateString("en-IN", { month: "short", year: "2-digit" });
  // The provider sometimes omits a quarter; show the hole rather than
  // letting neighbouring bars close up and imply continuity.
  const withGaps: (QuarterPoint & { missing?: boolean })[] = [];
  pts.forEach((q, i) => {
    if (i > 0) {
      const prev = new Date(pts[i - 1].period_end);
      const days = (new Date(q.period_end).getTime() - prev.getTime()) / 86400000;
      if (days > 120) {
        const gap = new Date(prev);
        gap.setMonth(gap.getMonth() + 3);
        withGaps.push({ period_end: gap.toISOString().slice(0, 10), missing: true, notes: [] });
      }
    }
    withGaps.push(q);
  });
  return (
    <div className="qbars" ref={tip.ref}>
      {withGaps.map((q) => q.missing ? (
        <div className="qbar missing" key={q.period_end} title="Not reported by the data provider">
          <span className="qbar-value">n/a</span>
          <span className="qbar-track" />
          <span className="qbar-label">{label(q.period_end)}</span>
        </div>
      ) : (
        <div className="qbar" key={q.period_end}
          onMouseEnter={(e) => tip.show(e, <TipRows title={`Quarter ended ${new Date(q.period_end).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}`} rows={[["Revenue", inr(q.revenue)], ["Net income", inr(q.net_income)]]} />)}
          onMouseLeave={tip.hide}>
          <span className="qbar-value">{inr(q.revenue).replace(" Cr", "")}</span>
          <span className="qbar-track"><span className="qbar-fill" style={{ height: `${(q.revenue! / max) * 100}%` }} /></span>
          <span className="qbar-label">{label(q.period_end)}</span>
        </div>
      ))}
      {tip.node}
    </div>
  );
}
