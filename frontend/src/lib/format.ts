/** Number formatting for Indian-market figures. Money is shown in the
 * units Indian filings and press use - crore (1e7) and lakh crore (1e12) -
 * rather than billions, so figures read the way analysts quote them. */

const DASH = "—";

export function inr(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  const sign = value < 0 ? "−" : "";
  const abs = Math.abs(value);
  if (abs >= 1e12) return `${sign}₹${(abs / 1e12).toFixed(2)} L Cr`;
  if (abs >= 1e7) return `${sign}₹${(abs / 1e7).toLocaleString("en-IN", { maximumFractionDigits: 0 })} Cr`;
  if (abs >= 1e5) return `${sign}₹${(abs / 1e5).toFixed(1)} L`;
  return `${sign}₹${abs.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

export function pct(value: number | null | undefined, digits = 1, signed = false): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  const s = (value * 100).toFixed(digits);
  return `${signed && value > 0 ? "+" : value < 0 ? "−" : ""}${s.replace("-", "")}%`;
}

export function times(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${value.toFixed(1)}×`;
}

export function plain(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return value.toFixed(digits);
}

export function count(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return value.toLocaleString("en-IN");
}

export function ago(iso: string, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

