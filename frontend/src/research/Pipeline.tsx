import { useEffect, useState } from "react";
import { BadgeCheck, FileSearch, FileText, ListChecks, Route, ShieldCheck } from "lucide-react";
import type { ResearchRun } from "../services/api";
import "./pipeline.css";

const STEPS = [
  { id: "planning", icon: Route, title: "Plan", body: "Identify the company and break the question into targeted sub-questions." },
  { id: "retrieving", icon: FileSearch, title: "Gather sources", body: "Pull filings, financial statements and coverage - each stored verbatim." },
  { id: "extracting", icon: ListChecks, title: "Extract claims", body: "Read every source for factual and financial statements, with the exact quote." },
  { id: "verifying", icon: ShieldCheck, title: "Verify each claim", body: "An independent verifier checks every claim against its quote - never the report." },
  { id: "complete", icon: FileText, title: "Build the report", body: "Only verified claims make it in, each linked to the source it came from." },
] as const;

const ORDER = ["pending", "planning", "retrieving", "extracting", "verifying", "followup", "complete"];

function stepState(step: string, status: string): "done" | "active" | "upcoming" {
  const current = status === "followup" ? "verifying" : status === "pending" ? "planning" : status;
  const s = ORDER.indexOf(step), c = ORDER.indexOf(current);
  return c > s ? "done" : c === s ? "active" : "upcoming";
}

const fmt = (sec: number) => (sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m ${String(sec % 60).padStart(2, "0")}s`);

/**
 * The research pipeline, drawn once and used twice: idle, it explains how
 * a verified report is built; during a run, the same steps become live
 * progress driven by the run's real status (not a timer).
 */
export function Pipeline({ run, running }: { run?: ResearchRun | null; running?: boolean }) {
  const status = run?.status ?? "pending";
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!running) return;
    const start = Date.now();
    const id = window.setInterval(() => setElapsed(Math.round((Date.now() - start) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [running]);

  const doneCount = running ? STEPS.filter((s) => stepState(s.id, status) === "done").length : 0;
  const progress = running ? Math.min(0.96, (doneCount + 0.5) / STEPS.length) : 1;

  return (
    <section className={`pipeline ${running ? "running" : "idle"}`} aria-live={running ? "polite" : undefined}>
      <header className="pipeline-head">
        <div>
          <span className="eyebrow">{running ? "Research in progress" : "How a verified report is built"}</span>
          <h3>{running ? STEPS.find((s) => stepState(s.id, status) === "active")?.title ?? "Starting" : "Five steps. No claim skips verification."}</h3>
          {running && status === "followup" && <p className="pipeline-note">Evidence was thin somewhere - running a targeted follow-up search.</p>}
        </div>
        {running
          ? <span className="pipeline-time">{fmt(elapsed)} · typically 3–5 min</span>
          : <span className="pipeline-badge"><BadgeCheck size={14} /> Every claim checked</span>}
      </header>

      <div className="pipeline-track" aria-hidden="true"><span style={{ transform: `scaleX(${progress})` }} /></div>

      <ol className="pipeline-steps">
        {STEPS.map((step, i) => {
          const state = running ? stepState(step.id, status) : "idle";
          const Icon = step.icon;
          return (
            <li key={step.id} className={`pstep ${state}`} style={{ ["--i" as string]: i }}>
              <span className="pstep-icon"><Icon size={17} /></span>
              <span className="pstep-num">0{i + 1}</span>
              <strong>{step.title}</strong>
              <p>{step.body}</p>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
