import {
  forwardRef,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  fetchWords,
  fmtTime,
  patchSegment,
  speakerName,
  type Segment,
  type Speaker,
} from "../api/client";

export interface TranscriptListHandle {
  scrollToIndex: (index: number, align?: "center" | "auto") => void;
}

/** Virtualized transcript: only the rows on screen are rendered, which is
 * what lets a 1000-segment meeting scroll smoothly (the failure mode that
 * killed Swagger). Editing is in-place with per-segment auto-save. */
const TranscriptList = forwardRef<
  TranscriptListHandle,
  {
    meetingId: number;
    segments: Segment[];
    speakers: Speaker[];
    activeSegmentId: number | null;
    onSeek: (start: number, end: number) => void;
    onEdit?: () => void;
  }
>(function TranscriptList({ meetingId, segments, speakers, activeSegmentId, onSeek, onEdit }, ref) {
  const parentRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: segments.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 64,
    overscan: 10,
  });

  useImperativeHandle(ref, () => ({
    scrollToIndex: (index: number, align: "center" | "auto" = "center") => {
      virtualizer.scrollToIndex(index, { align });
      // Rows are variable-height with an estimated size, so a jump to a far row can
      // first land off-target ("an unrelated part"); re-issue once after layout so it
      // settles on the right line. Only needed for deliberate centered jumps — the
      // "auto" play-follow scrolls are small and accurate already.
      if (align === "center") {
        requestAnimationFrame(() => virtualizer.scrollToIndex(index, { align }));
      }
    },
  }));

  return (
    <div ref={parentRef} style={{ height: "100%", overflowY: "auto" }}>
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((row) => {
          const seg = segments[row.index];
          return (
            <div
              key={seg.id}
              data-index={row.index}
              ref={virtualizer.measureElement}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                transform: `translateY(${row.start}px)`,
              }}
            >
              <SegmentRow
                meetingId={meetingId}
                segment={seg}
                speakers={speakers}
                active={seg.id === activeSegmentId}
                onSeek={onSeek}
                onEdit={onEdit}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
});

export default TranscriptList;

function SegmentRow({
  meetingId,
  segment,
  speakers,
  active,
  onSeek,
  onEdit,
}: {
  meetingId: number;
  segment: Segment;
  speakers: Speaker[];
  active: boolean;
  onSeek: (start: number, end: number) => void;
  onEdit?: () => void;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(segment.text);

  // Low-confidence highlighting: words are fetched lazily, only for the
  // active segment, and only when not editing.
  const words = useQuery({
    queryKey: ["words", meetingId, segment.id],
    queryFn: () => fetchWords(meetingId, segment.id),
    enabled: active && !editing,
    staleTime: Infinity,
  });

  const save = useMutation({
    mutationFn: (body: { text?: string; speaker_id?: number | null }) =>
      patchSegment(meetingId, segment.id, body),
    onSuccess: (updated) => {
      qc.setQueryData<Segment[]>(["segments", meetingId], (old) =>
        old?.map((s) => (s.id === updated.id ? updated : s)),
      );
    },
  });

  const commitText = () => {
    setEditing(false);
    const text = draft.trim();
    if (text && text !== segment.text) save.mutate({ text });
    else setDraft(segment.text);
  };

  const renderText = () => {
    // If we have word data for the active row, underline shaky words. Guard:
    // only swap to word-level rendering when those words actually reconstruct
    // the segment text. Otherwise a word/segment mismatch (or a dropped tail of
    // timings) would make the line appear to "change" to unrelated text the
    // moment it's clicked — fall back to the plain segment text in that case.
    if (active && words.data?.length) {
      const norm = (s: string) => s.replace(/[\s\p{P}]/gu, "").toLowerCase();
      const joined = words.data.map((w) => w.text).join(" ");
      if (norm(joined) === norm(segment.text)) {
        return words.data.map((w, i) => (
          <span
            key={w.id}
            className={w.score !== null && w.score < 0.5 ? "word-low" : undefined}
            title={w.score !== null ? `confidence ${(w.score * 100).toFixed(0)}%` : undefined}
          >
            {w.text}
            {i < words.data.length - 1 ? " " : ""}
          </span>
        ));
      }
    }
    return segment.text;
  };

  return (
    <div className={`seg ${active ? "active" : ""}`}>
      <span
        className="time"
        title="Play just this line"
        onClick={() => onSeek(segment.start_seconds, segment.end_seconds)}
      >
        {fmtTime(segment.start_seconds)}
      </span>

      <select
        className="spk"
        value={segment.speaker_id ?? ""}
        title="Reassign speaker for this segment"
        onChange={(e) =>
          save.mutate({ speaker_id: e.target.value ? Number(e.target.value) : null })
        }
      >
        <option value="">(unknown)</option>
        {speakers.map((sp) => (
          <option key={sp.id} value={sp.id}>
            {speakerName(sp)}
          </option>
        ))}
      </select>

      {editing ? (
        <textarea
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitText}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              commitText();
            }
            if (e.key === "Escape") {
              setDraft(segment.text);
              setEditing(false);
            }
          }}
        />
      ) : (
        <div
          className="text"
          title="Click to edit (pauses playback)"
          onClick={() => {
            onEdit?.();
            setEditing(true);
          }}
        >
          {renderText()}
        </div>
      )}
      {save.isPending && <span className="saving">saving…</span>}
      {save.isError && (
        <span className="saving" style={{ color: "var(--danger)" }} title={(save.error as Error).message}>
          save failed — click the line to retry
        </span>
      )}
    </div>
  );
}
