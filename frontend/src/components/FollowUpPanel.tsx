import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CARRY_FORWARD_LABELS,
  deleteCarryForwardItem,
  getCarryForward,
  listMeetings,
  rerunCarryForward,
  setFollowUp,
  statusInfo,
  updateCarryForwardItem,
  localDate,
  type CarryForwardItem,
  type Meeting,
} from "../api/client";
import ConfirmDialog from "./ConfirmDialog";

/** Zone ⑤ — "what happened to what we agreed last time?"
 *
 * The user picks the earlier meeting this one follows up. The system then reads
 * THIS meeting's transcript for evidence about each action agreed in that one,
 * and reports a verdict per action. Every verdict other than "Not discussed" is
 * backed by a transcript line, which is clickable — the same "prove it" contract
 * the insight items follow. */
export default function FollowUpPanel({
  meetingId,
  meeting,
  onJumpToSegment,
}: {
  meetingId: number;
  meeting: Meeting | undefined;
  onJumpToSegment: (segmentId: number) => void;
}) {
  const qc = useQueryClient();
  const meetings = useQuery({ queryKey: ["meetings"], queryFn: listMeetings });

  // Poll only while the analysis is actually running.
  const cf = useQuery({
    queryKey: ["carry-forward", meetingId],
    queryFn: () => getCarryForward(meetingId),
    refetchInterval: (q) => (q.state.data?.status === "analysing" ? 3000 : false),
  });

  const link = useMutation({
    mutationFn: (previousId: number | null) => setFollowUp(meetingId, previousId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["carry-forward", meetingId] });
      qc.invalidateQueries({ queryKey: ["meeting", meetingId] });
    },
  });

  const rerun = useMutation({
    mutationFn: () => rerunCarryForward(meetingId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["carry-forward", meetingId] }),
  });

  // Re-checking replaces every carry-forward row, including any verdict the user
  // has corrected by hand, so it is confirmed in-app exactly as regenerating the
  // insights is. The "Try again" button on the failed banner is deliberately NOT
  // confirmed: a failed run left nothing to lose.
  const [confirmingRerun, setConfirmingRerun] = useState(false);

  // Only meetings that already HAVE a transcript can be followed up, and a
  // meeting can never follow itself. Newest first — a follow-up almost always
  // points at something recent.
  const candidates = useMemo(
    () =>
      (meetings.data ?? []).filter(
        (m) => m.id !== meetingId && !statusInfo(m.status).busy && m.status !== "uploaded",
      ),
    [meetings.data, meetingId],
  );

  const previousId = cf.data?.previous_meeting_id ?? meeting?.follow_up_of ?? null;
  const status = cf.data?.status ?? null;
  const analysing = status === "analysing";
  const failed = !!status?.startsWith("failed");
  const items = cf.data?.items ?? [];
  const discussed = items.filter((i) => i.status !== "not_discussed").length;

  return (
    <div className="followup">
      <h2>
        <span className="zone-num">5</span>Follow-up
        {previousId !== null && !analysing && items.length > 0 && (
          <span className="followup-count">
            {discussed} of {items.length} carried over
          </span>
        )}
      </h2>
      <p className="panel-intro">
        Link the earlier meeting this one follows. The system then reads this transcript for
        what happened to everything agreed there, and you can export both as one document.
        You can <b>edit</b> or <b>delete</b> any verdict before exporting.
      </p>

      <label className="followup-label" htmlFor="followup-select">
        Follows up on
      </label>
      <select
        id="followup-select"
        value={previousId === null ? "" : String(previousId)}
        disabled={link.isPending || analysing}
        onChange={(e) => link.mutate(e.target.value === "" ? null : Number(e.target.value))}
      >
        <option value="">No — this meeting stands alone</option>
        {candidates.map((m) => (
          <option key={m.id} value={m.id}>
            {m.title} ({localDate(m.created_at)})
          </option>
        ))}
      </select>

      {link.isError && <div className="banner error">{(link.error as Error).message}</div>}

      {analysing && (
        <div className="empty-note">
          Reading this meeting for progress on the earlier one's action items…
        </div>
      )}

      {failed && (
        <div className="banner error">
          Couldn't complete the follow-up analysis — {status?.replace(/^failed:\s*/, "")}
          <div style={{ marginTop: 6 }}>
            <button className="ghost small" onClick={() => rerun.mutate()} disabled={rerun.isPending}>
              Try again
            </button>
          </div>
        </div>
      )}

      {previousId !== null && !analysing && !failed && items.length === 0 && (
        <div className="empty-note">
          That meeting has no action items recorded, so there is nothing to carry forward.
        </div>
      )}

      {items.length > 0 && (
        <>
          <ul className="followup-list">
            {items.map((it) => (
              <CarryForwardRow key={it.id} meetingId={meetingId} item={it} onJump={onJumpToSegment} />
            ))}
          </ul>
          <div style={{ marginTop: 8 }}>
            <button
              className="small"
              disabled={rerun.isPending || analysing}
              onClick={() => setConfirmingRerun(true)}
            >
              {rerun.isPending ? "Re-checking…" : "Re-check progress"}
            </button>
            {rerun.isError && (
              <div className="empty-note" style={{ color: "var(--danger)" }}>
                Couldn't start the re-check: {(rerun.error as Error).message}
              </div>
            )}
          </div>
          <ConfirmDialog
            open={confirmingRerun}
            title="Re-check progress?"
            message="Every carried-forward verdict is discarded and worked out again from this meeting's transcript. Any verdict or note you corrected by hand is lost."
            confirmLabel="Re-check"
            onConfirm={() => { setConfirmingRerun(false); rerun.mutate(); }}
            onCancel={() => setConfirmingRerun(false)}
          />
        </>
      )}
    </div>
  );
}

function CarryForwardRow({
  meetingId,
  item,
  onJump,
}: {
  meetingId: number;
  item: CarryForwardItem;
  onJump: (segmentId: number) => void;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.description);
  const [draftStatus, setDraftStatus] = useState(item.status);
  const [draftNote, setDraftNote] = useState(item.note ?? "");
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const reset = () => {
    setDraft(item.description);
    setDraftStatus(item.status);
    setDraftNote(item.note ?? "");
    setEditing(false);
  };

  const save = useMutation({
    mutationFn: () =>
      updateCarryForwardItem(meetingId, item.id, {
        description: draft.trim(),
        status: draftStatus,
        note: draftNote.trim() || null,
      }),
    onSuccess: () => {
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["carry-forward", meetingId] });
    },
  });

  const del = useMutation({
    mutationFn: () => deleteCarryForwardItem(meetingId, item.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["carry-forward", meetingId] }),
  });

  if (editing) {
    return (
      <li className="followup-item">
        <textarea
          className="insight-edit"
          value={draft}
          autoFocus
          rows={2}
          aria-label="Edit the action wording"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") reset(); }}
        />
        <div className="followup-edit-row">
          <label htmlFor={`cf-status-${item.id}`}>Verdict</label>
          <select
            id={`cf-status-${item.id}`}
            value={draftStatus}
            onChange={(e) => setDraftStatus(e.target.value)}
          >
            {Object.entries(CARRY_FORWARD_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>
        <input
          className="insight-edit followup-note-edit"
          value={draftNote}
          aria-label="Evidence note"
          placeholder="Note — why this verdict (optional)"
          onChange={(e) => setDraftNote(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") reset(); }}
        />
        <div className="insight-actions">
          <button
            className="small"
            disabled={!draft.trim() || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
          <button className="ghost small" onClick={reset}>Cancel</button>
        </div>
        {save.isError && (
          <div className="empty-note" style={{ color: "var(--danger)" }}>
            Save failed: {(save.error as Error).message}
          </div>
        )}
      </li>
    );
  }

  const label = CARRY_FORWARD_LABELS[item.status] ?? item.status;
  return (
    <li className="followup-item">
      <div className="followup-item-head">
        <span className={`cf-pill cf-${item.status}`}>{label}</span>
        <span className="followup-desc">{item.description}</span>
      </div>
      <div className="followup-meta">
        {item.owner && <span>Owner: {item.owner}</span>}
        {item.note && <span className="followup-note">“{item.note}”</span>}
        {item.source_segment_id ? (
          <span
            className="jump"
            role="button"
            tabIndex={0}
            onClick={() => onJump(item.source_segment_id as number)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onJump(item.source_segment_id as number);
              }
            }}
            title="Jump to the line in this meeting that shows this"
          >
            ↳ show in transcript
          </span>
        ) : (
          item.status === "not_discussed" && (
            <span className="followup-note">never mentioned in this meeting</span>
          )
        )}
      </div>
      <div className="insight-actions">
        <button className="linkbtn" onClick={() => setEditing(true)}>✎ Edit</button>
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
        title="Remove this carried-forward item?"
        message="It disappears from the follow-up list and the combined export. Re-checking progress brings it back."
        onConfirm={() => { setConfirmingDelete(false); del.mutate(); }}
        onCancel={() => setConfirmingDelete(false)}
      />
      {del.isError && (
        <div className="empty-note" style={{ color: "var(--danger)" }}>
          Delete failed: {(del.error as Error).message}
        </div>
      )}
    </li>
  );
}
