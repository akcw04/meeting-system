import { useRef } from "react";
import { statusInfo, type Meeting } from "../api/client";

// Stages that report a real 0..1 progress number (the two long stages).
const DETERMINATE = new Set(["transcribing", "categorizing"]);

/** Live progress for a processing meeting: a filled bar + % + ETA during
 *  transcription / insight-extraction, an animated bar for the quick stages.
 *  Renders nothing once the meeting is idle. */
export default function ProgressBar({ meeting }: { meeting: Meeting }) {
  // Remember progress over time (within the current stage) to estimate the ETA.
  const ref = useRef<{ stage: string; t0: number; p0: number } | null>(null);

  const st = statusInfo(meeting.status);
  if (!st.busy) return null;

  const hasPct = DETERMINATE.has(meeting.status) && meeting.progress != null;
  const p = hasPct ? (meeting.progress as number) : null;
  const pct = p != null ? Math.round(p * 100) : null;

  let eta: string | null = null;
  if (p != null) {
    const now = Date.now();
    const cur = ref.current;
    if (!cur || cur.stage !== meeting.status || p < cur.p0) {
      ref.current = { stage: meeting.status, t0: now, p0: p }; // (re)baseline
    } else if (p > cur.p0 + 0.005) {
      const rate = (p - cur.p0) / ((now - cur.t0) / 1000); // fraction per second
      if (rate > 0) eta = fmtEta((1 - p) / rate);
    }
  }

  return (
    <div className="progress">
      <div className="progress-track">
        <div
          className={`progress-fill ${hasPct ? "" : "indeterminate"}`}
          style={hasPct ? { width: `${pct}%` } : undefined}
        />
      </div>
      <div className="progress-label">
        {st.label}
        {pct != null ? ` — ${pct}%` : ""}
        {eta ? ` · ~${eta} left` : ""}
      </div>
    </div>
  );
}

function fmtEta(seconds: number): string {
  if (!isFinite(seconds) || seconds <= 0) return "";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}
