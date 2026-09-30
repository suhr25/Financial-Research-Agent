import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

/** Horizontal scroll-snap row with arrow controls and edge fades. Native
 * scrolling underneath, so touch swipe and trackpads just work. */
export function Carousel({ children, label, count }: { children: ReactNode; label: string; count: number }) {
  const track = useRef<HTMLDivElement>(null);
  const [edge, setEdge] = useState({ start: true, end: false });
  const [first, setFirst] = useState(0);

  const update = () => {
    const el = track.current;
    if (!el) return;
    setEdge({ start: el.scrollLeft < 4, end: el.scrollLeft + el.clientWidth > el.scrollWidth - 4 });
    const card = el.firstElementChild as HTMLElement | null;
    if (card) setFirst(Math.round(el.scrollLeft / (card.offsetWidth + 12)));
  };
  useEffect(() => {
    update();
    const ro = new ResizeObserver(update);
    if (track.current) ro.observe(track.current);
    return () => ro.disconnect();
  }, [count]);

  const page = (dir: 1 | -1) => {
    const el = track.current;
    if (el) el.scrollBy({ left: dir * el.clientWidth * 0.9, behavior: "smooth" });
  };

  const visible = track.current ? Math.max(1, Math.round(track.current.clientWidth / ((track.current.firstElementChild as HTMLElement | null)?.offsetWidth || 1))) : 1;
  return (
    <div className={`carousel ${edge.start ? "at-start" : ""} ${edge.end ? "at-end" : ""}`}>
      <div className="carousel-head">
        <span className="carousel-count" aria-live="polite">{Math.min(first + 1, count)}–{Math.min(first + visible, count)} of {count}</span>
        <button type="button" className="carousel-btn" onClick={() => page(-1)} disabled={edge.start} aria-label={`Previous ${label}`}><ChevronLeft size={16} /></button>
        <button type="button" className="carousel-btn" onClick={() => page(1)} disabled={edge.end} aria-label={`Next ${label}`}><ChevronRight size={16} /></button>
      </div>
      <div className="carousel-track" ref={track} onScroll={update} role="region" aria-label={label} tabIndex={0}>
        {children}
      </div>
    </div>
  );
}
