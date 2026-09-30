import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

export interface TabDef<T extends string> { id: T; label: string; icon?: ReactNode; count?: number }

/** Tab bar with one indicator that slides and resizes to the active tab,
 * instead of each tab repainting its own background. */
export function Tabs<T extends string>({ tabs, active, onChange, label }: { tabs: TabDef<T>[]; active: T; onChange: (id: T) => void; label: string }) {
  const bar = useRef<HTMLDivElement>(null);
  const [indicator, setIndicator] = useState<{ left: number; width: number } | null>(null);

  useLayoutEffect(() => {
    const measure = () => {
      const el = bar.current?.querySelector<HTMLButtonElement>(`[data-tab="${active}"]`);
      if (el) setIndicator({ left: el.offsetLeft, width: el.offsetWidth });
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (bar.current) ro.observe(bar.current);
    return () => ro.disconnect();
  }, [active, tabs.length]);

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const i = tabs.findIndex((t) => t.id === active);
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    onChange(next.id);
    bar.current?.querySelector<HTMLButtonElement>(`[data-tab="${next.id}"]`)?.focus();
  };

  return (
    <div className="vtabs" role="tablist" aria-label={label} ref={bar} onKeyDown={onKey}>
      {indicator && <span className="vtabs-indicator" style={{ transform: `translateX(${indicator.left}px)`, width: indicator.width }} aria-hidden="true" />}
      {tabs.map((t) => (
        <button key={t.id} type="button" role="tab" data-tab={t.id} aria-selected={t.id === active} tabIndex={t.id === active ? 0 : -1}
          className={t.id === active ? "active" : ""} onClick={() => onChange(t.id)}>
          {t.icon}{t.label}{t.count ? <em>{t.count}</em> : null}
        </button>
      ))}
    </div>
  );
}
