import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteInsightItem,
  getInsights,
  triggerCategorize,
  updateInsightItem,
  type InsightCategory,
  type InsightItem,
  type Insights,
} from "../api/client";
import ConfirmDialog from "./ConfirmDialog";

/** Summary + the five IR categories. Each item links to its supporting segment
 * (the anti-hallucination citation) and can be edited or deleted in place — the
 * human-in-the-loop correction the IR is built around — before exporting. */
export default function InsightsPanel({
  meetingId,
  meetingStatus,
  onJumpToSegment,
}: {
  meetingId: number;
  meetingStatus: string;
  onJumpToSegment: (segmentId: number) => void;
}) {
  const qc = useQueryClient();
  const insights = useQuery({
    queryKey: ["insights", meetingId],
    queryFn: () => getInsights(meetingId),
    refetchInterval: (q) =>
      q.state.data && ["categorizing", "diarized"].includes(q.state.data.status) ? 4000 : false,
  });

  const categorize = useMutation({
    mutationFn: () => triggerCategorize(meetingId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["insights", meetingId] });
      qc.invalidateQueries({ queryKey: ["meeting", meetingId] });
    },
  });

  // Regenerating discards the current insights, including any the user has
  // corrected by hand, so it is confirmed in-app like the delete actions.
  const [confirmingRegen, setConfirmingRegen] = useState(false);

  const data = insights.data;
  const canGenerate =
    meetingStatus === "diarized" ||
    meetingStatus === "ready" || // allow re-running (e.g. after editing the transcript)
    meetingStatus.startsWith("categorize_failed");
  // The pipeline replaces the whole insight set in ONE transaction at the very end
  // of the run (runner.categorize_step), so until it lands the database still holds
  // the PREVIOUS summary and items. Showing them during a run made a stale set look
  // like the new one - and let the user edit rows that were about to be deleted.
  // Treat a run reported by either query as "generating" and hide the old set
  // until the new one is written.
  const generating =
    meetingStatus === "categorizing" ||
    categorize.isPending ||
    insights.data?.status === "categorizing";
  const buttonLabel = meetingStatus.startsWith("categorize_failed")
    ? "Retry insight extraction"
    : meetingStatus === "ready"
      ? "Regenerate insights"
      : "Generate insights";

  return (
    <div>
      <h2><span className="zone-num">4</span>Insights &amp; export</h2>
      <p className="panel-intro">
        Summary and the five categories are extracted from the transcript by a local AI model.
        Every item links to the line it came from — click "↳ show in transcript" to verify it.
        You can <b>edit</b> or <b>delete</b> any item, or regenerate the whole set.
      </p>
      <h2>Meeting Summary</h2>
      {generating ? (
        <div className="empty-note">
          Extracting insights with the local AI model on your machine — this can take several
          minutes (longer for long meetings)…
          {data?.summary && " The previous summary and items stay saved until the new set replaces them."}
        </div>
      ) : data?.summary ? (
        <div className="summary">{data.summary}</div>
      ) : (
        <div className="empty-note">No insights yet.</div>
      )}
      {canGenerate && (
        <div style={{ marginTop: 8 }}>
          <button
            className="small"
            disabled={generating}
            onClick={() => {
              if (meetingStatus === "ready") setConfirmingRegen(true);
              else categorize.mutate();
            }}
          >
            {buttonLabel}
          </button>
          {categorize.isError && (
            <div className="empty-note" style={{ color: "var(--danger)" }}>
              Couldn't start extraction: {(categorize.error as Error).message}
            </div>
          )}
        </div>
      )}
      <ConfirmDialog
        open={confirmingRegen}
        title="Regenerate insights?"
        message="The current summary and all five categories are discarded and extracted again from the transcript. Any wording you corrected by hand is lost."
        confirmLabel="Regenerate"
        onConfirm={() => { setConfirmingRegen(false); categorize.mutate(); }}
        onCancel={() => setConfirmingRegen(false)}
      />

      {/* Hidden while a run is in flight: these rows would still be the PREVIOUS
        set, and editing one would lose the edit when the new set replaces it. */}
      {!generating && (
        <>
          <Category title="Action Items" category="action_items" meetingId={meetingId}
            items={data?.action_items} onJump={onJumpToSegment}
            render={(it) => (
              <>
                {it.description}
                <div className="meta">
                  {it.owner ? `Owner: ${it.owner}` : ""}
                  {it.owner && it.due_date ? " · " : ""}
                  {it.due_date ? `Due: ${it.due_date}` : ""}
                </div>
              </>
            )}
          />
          <Category title="Key Decisions" category="decisions" meetingId={meetingId}
            items={data?.decisions} onJump={onJumpToSegment}
            render={(it) => it.description}
          />
          <Category title="Deadlines" category="deadlines" meetingId={meetingId}
            items={data?.deadlines} onJump={onJumpToSegment}
            render={(it) => (
              <>
                {it.description}
                {it.target_date && <div className="meta">Date: {it.target_date}</div>}
              </>
            )}
          />
          <Category title="Technical Issues" category="issues" meetingId={meetingId}
            items={data?.issues} onJump={onJumpToSegment}
            render={(it) => it.description}
          />
          <Category title="Risks" category="risks" meetingId={meetingId}
            items={data?.risks} onJump={onJumpToSegment}
            render={(it) => (
              <>
                {it.description}
                {it.mitigation && <div className="meta">Mitigation: {it.mitigation}</div>}
              </>
            )}
          />
        </>
      )}
    </div>
  );
}

function Category({
  title,
  category,
  meetingId,
  items,
  onJump,
  render,
}: {
  title: string;
  category: InsightCategory;
  meetingId: number;
  items: InsightItem[] | undefined;
  onJump: (segmentId: number) => void;
  render: (item: InsightItem) => React.ReactNode;
}) {
  return (
    <div>
      <h2>{title}</h2>
      {!items?.length && <div className="empty-note">None recorded.</div>}
      {items?.map((it) => (
        <InsightItemRow
          key={it.id}
          meetingId={meetingId}
          category={category}
          item={it}
          onJump={onJump}
          render={render}
        />
      ))}
    </div>
  );
}

function InsightItemRow({
  meetingId,
  category,
  item,
  onJump,
  render,
}: {
  meetingId: number;
  category: InsightCategory;
  item: InsightItem;
  onJump: (segmentId: number) => void;
  render: (item: InsightItem) => React.ReactNode;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.description);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const save = useMutation({
    mutationFn: (description: string) =>
      updateInsightItem(meetingId, category, item.id, { description }),
    onSuccess: () => {
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["insights", meetingId] });
    },
  });
  const del = useMutation({
    mutationFn: () => deleteInsightItem(meetingId, category, item.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["insights", meetingId] }),
  });

  if (editing) {
    return (
      <div className="insight-item">
        <textarea
          className="insight-edit"
          value={draft}
          autoFocus
          rows={2}
          aria-label="Edit item"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") { setDraft(item.description); setEditing(false); }
          }}
        />
        <div className="insight-actions">
          <button className="small" disabled={!draft.trim() || save.isPending}
            onClick={() => save.mutate(draft.trim())}>
            {save.isPending ? "Saving…" : "Save"}
          </button>
          <button className="ghost small"
            onClick={() => { setDraft(item.description); setEditing(false); }}>
            Cancel
          </button>
        </div>
        {save.isError && (
          <div className="empty-note" style={{ color: "var(--danger)" }}>
            Save failed: {(save.error as Error).message}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="insight-item">
      {render(item)}
      {item.source_segment_id != null && (
        <div
          className="jump"
          role="button"
          tabIndex={0}
          onClick={() => onJump(item.source_segment_id!)}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onJump(item.source_segment_id!); } }}
        >
          ↳ show in transcript
        </div>
      )}
      {item.low_support && (
        <div
          className="cite-warn"
          title="The linked transcript line may not fully support this item — open it to verify."
        >
          ⚠ verify this citation
        </div>
      )}
      <div className="insight-actions">
        <button className="linkbtn" onClick={() => { setDraft(item.description); setEditing(true); }}>
          ✎ Edit
        </button>
        <button
          className="linkbtn danger"
          disabled={del.isPending}
          onClick={() => setConfirmingDelete(true)}
        >
          {del.isPending ? "Deleting…" : "🗑 Delete"}
        </button>
      </div>
      <ConfirmDialog
        open={confirmingDelete}
        title="Delete this item?"
        message="It is removed from the insights and the export. It comes back if you regenerate insights."
        onConfirm={() => { setConfirmingDelete(false); del.mutate(); }}
        onCancel={() => setConfirmingDelete(false)}
      />
      {del.isError && (
        <div className="empty-note" style={{ color: "var(--danger)" }}>
          Delete failed: {(del.error as Error).message}
        </div>
      )}
    </div>
  );
}

// re-export to satisfy isolatedModules users importing the type
export type { Insights };
