import type { CompanyMetrics } from "../services/api";
import { inr, pct, times } from "../lib/format";

export type MetricKey =
  | "market_cap" | "market_cap_weight" | "revenue_ttm" | "net_income_ttm" | "revenue_growth_yoy"
  | "earnings_growth_yoy" | "operating_margin" | "profit_margin" | "pe_trailing" | "eps_ttm"
  | "dividend_yield" | "return_1y" | "volatility_1y" | "max_drawdown_1y";

export interface MetricDef {
  key: MetricKey;
  label: string;
  short: string;
  format: (v: number | null | undefined) => string;
  /** true = higher is better, false = lower is better, null = neither (size). */
  better: boolean | null;
  help: string;
}

export const METRICS: Record<MetricKey, MetricDef> = {
  market_cap: { key: "market_cap", label: "Market cap", short: "Mkt cap", format: inr, better: null, help: "Share price × shares outstanding." },
  market_cap_weight: { key: "market_cap_weight", label: "Sector weight", short: "Weight", format: (v) => pct(v, 1), better: null, help: "Share of the ten companies' combined market value." },
  revenue_ttm: { key: "revenue_ttm", label: "Revenue (TTM)", short: "Revenue", format: inr, better: null, help: "Sum of the last four quarterly results filed with NSE (consolidated)." },
  net_income_ttm: { key: "net_income_ttm", label: "Net profit (TTM)", short: "Net profit", format: inr, better: null, help: "Profit attributable to shareholders, summed over the last four filed quarters." },
  revenue_growth_yoy: { key: "revenue_growth_yoy", label: "Revenue growth (YoY)", short: "Rev growth", format: (v) => pct(v, 1, true), better: true, help: "Latest filed quarter vs. the same quarter a year earlier." },
  earnings_growth_yoy: { key: "earnings_growth_yoy", label: "Profit growth (YoY)", short: "Profit growth", format: (v) => pct(v, 1, true), better: true, help: "Latest quarter's net profit vs. the same quarter a year earlier." },
  operating_margin: { key: "operating_margin", label: "Operating margin", short: "Op. margin", format: (v) => pct(v), better: true, help: "(Profit before exceptional items and tax + finance costs - other income) / revenue, over the last four quarters." },
  profit_margin: { key: "profit_margin", label: "Net margin", short: "Net margin", format: (v) => pct(v), better: true, help: "Net profit / revenue, over the last four quarters." },
  pe_trailing: { key: "pe_trailing", label: "P/E (trailing)", short: "P/E", format: times, better: false, help: "NSE closing price / diluted EPS summed over the last four filed quarters. Lower is cheaper." },
  eps_ttm: { key: "eps_ttm", label: "EPS (TTM, diluted)", short: "EPS", format: (v) => (v == null ? "—" : "₹" + v.toFixed(2)), better: null, help: "Diluted earnings per share, summed over the last four filed quarters." },
  dividend_yield: { key: "dividend_yield", label: "Dividend yield", short: "Yield", format: (v) => pct(v, 2), better: true, help: "Dividends with an ex-date in the last 12 months (NSE corporate actions, incl. special) / closing price." },
  return_1y: { key: "return_1y", label: "1-year return", short: "1Y return", format: (v) => pct(v, 1, true), better: true, help: "Change in NSE closing price over one year, adjusted for splits and bonuses (dividends excluded)." },
  volatility_1y: { key: "volatility_1y", label: "Volatility (1Y)", short: "Volatility", format: (v) => pct(v, 0), better: false, help: "Annualised standard deviation of daily returns on NSE closing prices." },
  max_drawdown_1y: { key: "max_drawdown_1y", label: "Max drawdown (1Y)", short: "Drawdown", format: (v) => pct(v, 0), better: true, help: "Largest peak-to-trough fall in the NSE closing price over the last year." },
};

export const TABLE_COLUMNS: MetricKey[] = [
  "market_cap", "market_cap_weight", "revenue_ttm", "revenue_growth_yoy", "earnings_growth_yoy", "operating_margin",
  "profit_margin", "pe_trailing", "dividend_yield", "return_1y", "volatility_1y",
];

export const POSITIONING_METRICS: MetricKey[] = [
  "revenue_growth_yoy", "earnings_growth_yoy", "operating_margin", "profit_margin", "pe_trailing", "dividend_yield", "return_1y", "volatility_1y",
];

export const DETAIL_METRICS: MetricKey[] = [
  "revenue_ttm", "net_income_ttm", "revenue_growth_yoy", "earnings_growth_yoy", "operating_margin", "profit_margin",
  "eps_ttm", "pe_trailing", "dividend_yield", "return_1y", "volatility_1y", "max_drawdown_1y",
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
