import { useEffect, useRef, useState } from "react";

export const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** Animates a number from its previous value to `target` (ease-out cubic).
 * Jumps straight to the value when the user prefers reduced motion. */
export function useCountUp(target: number | null | undefined, duration = 900): number | null {
  const [value, setValue] = useState<number | null>(target ?? null);
  const from = useRef<number>(0);
  useEffect(() => {
    if (target == null || !Number.isFinite(target)) { setValue(null); return; }
    if (prefersReducedMotion()) { setValue(target); from.current = target; return; }
    const start = performance.now();
    const origin = from.current;
    let frame = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      setValue(origin + (target - origin) * eased);
      if (t < 1) frame = requestAnimationFrame(tick);
      else from.current = target;
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, duration]);
  return value;
}
