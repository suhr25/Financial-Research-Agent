import type { CompanyMetrics } from "../services/api";
import { inr, pct } from "../lib/format";

export type MetricKey =
  | "revenue_ttm" | "revenue_share" | "net_income_ttm" | "revenue_growth_yoy" | "earnings_growth_yoy"
  | "operating_margin" | "profit_margin" | "eps_ttm";

export interface MetricDef {
  key: MetricKey;
  label: string;
  short: string;
  format: (v: number | null | undefined) => string;
  /** true = higher is better, false = lower is better, null = neither (size). */
  better: boolean | null;
  help: string;
}

const rupees = (v: number | null | undefined) => (v == null ? "—" : "₹" + v.toFixed(2));

export const METRICS: Record<MetricKey, MetricDef> = {
  revenue_ttm: { key: "revenue_ttm", label: "Revenue (TTM)", short: "Revenue", format: inr, better: null, help: "Sum of the last four quarterly results (consolidated), as reported by the company." },
  revenue_share: { key: "revenue_share", label: "Share of industry revenue", short: "Rev. share", format: (v) => pct(v, 1), better: null, help: "The company's revenue as a share of the ten companies' combined revenue." },
  net_income_ttm: { key: "net_income_ttm", label: "Net profit (TTM)", short: "Net profit", format: inr, better: null, help: "Profit attributable to shareholders, summed over the last four reported quarters." },
  revenue_growth_yoy: { key: "revenue_growth_yoy", label: "Revenue growth (YoY)", short: "Rev growth", format: (v) => pct(v, 1, true), better: true, help: "Latest reported quarter vs. the same quarter a year earlier." },
  earnings_growth_yoy: { key: "earnings_growth_yoy", label: "Profit growth (YoY)", short: "Profit growth", format: (v) => pct(v, 1, true), better: true, help: "Latest quarter's net profit vs. the same quarter a year earlier." },
  operating_margin: { key: "operating_margin", label: "Operating margin", short: "Op. margin", format: (v) => pct(v), better: true, help: "(Profit before exceptional items and tax + finance costs - other income) / revenue, over the last four quarters." },
  profit_margin: { key: "profit_margin", label: "Net margin", short: "Net margin", format: (v) => pct(v), better: true, help: "Net profit / revenue, over the last four quarters." },
  eps_ttm: { key: "eps_ttm", label: "EPS (TTM, diluted)", short: "EPS", format: rupees, better: null, help: "Diluted earnings per share, summed over the last four reported quarters." },
};

export const TABLE_COLUMNS: MetricKey[] = [
  "revenue_ttm", "revenue_share", "revenue_growth_yoy", "net_income_ttm", "earnings_growth_yoy", "operating_margin", "profit_margin", "eps_ttm",
];

export const POSITIONING_METRICS: MetricKey[] = [
  "revenue_growth_yoy", "earnings_growth_yoy", "operating_margin", "profit_margin",
];

export const DETAIL_METRICS: MetricKey[] = [
  "revenue_ttm", "net_income_ttm", "revenue_growth_yoy", "earnings_growth_yoy", "operating_margin", "profit_margin", "eps_ttm", "revenue_share",
];

export const value = (c: CompanyMetrics, key: MetricKey): number | null => {
  const v = c[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
};

export function median(values: number[]): number | null {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/** 1 = best in peer set, n = worst; null if the metric has no direction or no value. */
export function rank(companies: CompanyMetrics[], c: CompanyMetrics, key: MetricKey): { rank: number; of: number } | null {
  const def = METRICS[key];
  const own = value(c, key);
  if (def.better === null || own === null) return null;
  const vals = companies.map((x) => value(x, key)).filter((v): v is number => v !== null);
  const better = vals.filter((v) => (def.better ? v > own : v < own)).length;
  return { rank: better + 1, of: vals.length };
}
