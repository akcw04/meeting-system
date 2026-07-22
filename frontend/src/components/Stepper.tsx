const STEPS: [string, string][] = [
  ["upload", "Upload"],
  ["process", "Process"],
  ["review", "Review"],
  ["export", "Export"],
];

/** The meeting journey shown as a path, so users always know where they are and
 * what's next: Upload -> Process -> Review -> Export. `current` highlights the
 * active phase; earlier phases render as done. Shown on the Meetings page
 * (Upload) and the Review page (Review) to tie the flow together. */
export default function Stepper({ current }: { current: string }) {
  const idx = STEPS.findIndex(([k]) => k === current);
  return (
    <nav className="stepper" aria-label="Where you are in the process">
      {STEPS.map(([key, label], i) => (
        <span key={key} className="stepper-cell">
          {i > 0 && <span className="stepper-sep" aria-hidden="true">›</span>}
          <span className={`stepper-item${i === idx ? " active" : i < idx ? " done" : ""}`}>
            <span className="stepper-num">{i + 1}</span>
            {label}
          </span>
        </span>
      ))}
    </nav>
  );
}
