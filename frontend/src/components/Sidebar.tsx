import { useEffect, type ReactNode } from "react";
import { PanelLeftClose, PanelLeftOpen, X } from "lucide-react";
import { BrandMark } from "./Brand";
import { initials, sessionEnds } from "./UserMenu";
import type { SessionUser } from "../services/api";
import "./sidebar.css";

export interface NavItem {
  id: string;
  label: string;
  icon: ReactNode;
  active?: boolean;
  badge?: ReactNode;
  sub?: boolean;
  onSelect: () => void;
}

export interface NavSection { label: string; items: NavItem[] }

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
export const TOGGLE_HINT = isMac ? "⌘B" : "Ctrl+B";

/**
 * Collapsible navigation rail. Collapsed, it is just the VeriFi mark plus
 * icons: hovering the mark turns it into an "expand" affordance, and each
 * icon shows its label as a tooltip. On phones it becomes an off-canvas
 * drawer instead (mobileOpen).
 */
export function Sidebar({
  sections, collapsed, onToggle, mobileOpen, onCloseMobile, user, status,
}: {
  sections: NavSection[];
  collapsed: boolean;
  onToggle: () => void;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  user: SessionUser;
  status: { tone: "teal" | "amber"; label: string };
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") { e.preventDefault(); onToggle(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onToggle]);

  const demo = user.kind === "demo";
  return (
    <>
      <div className={`sb-overlay ${mobileOpen ? "show" : ""}`} onClick={onCloseMobile} aria-hidden="true" />
      <aside className={`sb ${collapsed ? "collapsed" : ""} ${mobileOpen ? "open" : ""}`} aria-label="Main navigation">
        <div className="sb-head">
          <button type="button" className="sb-logo" onClick={collapsed ? onToggle : undefined} tabIndex={collapsed ? 0 : -1}
            aria-label={collapsed ? `Expand sidebar (${TOGGLE_HINT})` : "VeriFi"} data-tip={collapsed ? `Expand · ${TOGGLE_HINT}` : undefined}>
            <span className="sb-logo-mark"><BrandMark size={32} /></span>
            <span className="sb-logo-expand" aria-hidden="true"><PanelLeftOpen size={18} /></span>
          </button>
          <div className="sb-word">
            <strong>Veri<span>Fi</span></strong>
            <small>Every number has a story</small>
          </div>
          <button type="button" className="sb-icon-btn sb-collapse" onClick={onToggle} aria-label={`Collapse sidebar (${TOGGLE_HINT})`} title={`Collapse · ${TOGGLE_HINT}`}>
            <PanelLeftClose size={17} />
          </button>
          <button type="button" className="sb-icon-btn sb-close" onClick={onCloseMobile} aria-label="Close navigation"><X size={18} /></button>
        </div>

        <nav className="sb-nav">
          {sections.map((section) => (
            <div className="sb-section" key={section.label}>
              <span className="sb-label">{section.label}</span>
              {section.items.map((item) => (
                <button key={item.id} type="button" className={`sb-item ${item.active ? "active" : ""} ${item.sub ? "sub" : ""}`}
                  onClick={item.onSelect} aria-current={item.active ? "page" : undefined} data-tip={item.label}>
                  <span className="sb-item-icon">{item.icon}</span>
                  <span className="sb-item-label">{item.label}</span>
                  {item.badge != null && <span className="sb-badge">{item.badge}</span>}
                </button>
              ))}
            </div>
          ))}
        </nav>

        <div className="sb-foot" data-tip={demo ? "Guest explorer" : user.name}>
          <span className={`sb-avatar ${demo ? "demo" : ""}`}>{demo ? "G" : initials(user.name)}</span>
          <div className="sb-foot-copy">
            <strong>{demo ? "Guest explorer" : user.name}</strong>
            <small><i className={`sb-dot ${status.tone}`} />{demo ? `Demo · ends ${sessionEnds(user)}` : status.label}</small>
          </div>
        </div>
      </aside>
    </>
  );
}
