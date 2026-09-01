import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  audioUrl,
  combinedExportUrl,
  exportUrl,
  fetchAllSegments,
  fetchSpeakers,
  getMeeting,
  listTemplates,
  searchTranscript,
  statusInfo,
  type SearchHit,
  type Segment,
} from "../api/client";
import TranscriptList, { type TranscriptListHandle } from "../components/TranscriptList";
import SpeakersPanel from "../components/SpeakersPanel";
import InsightsPanel from "../components/InsightsPanel";
import FollowUpPanel from "../components/FollowUpPanel";
import ProgressBar from "../components/ProgressBar";
import { useCollapse } from "../components/useCollapse";

/** Index of the transcript line playing at time `t`. The small tolerance covers
 * the audio element snapping currentTime a hair below a line's start right after
 * a seek, which would otherwise highlight the PREVIOUS line. Assumes `segs` is
 * sorted by start_seconds. */
function segIndexAt(segs: Segment[], t: number): number {
  let lo = 0, hi = segs.length - 1, found = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (segs[mid].start_seconds <= t + 0.05) { found = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  return found;
}

export default function ReviewPage({
  meetingId,
  onBack,
}: {
  meetingId: number;
  onBack: () => void;
}) {
  const meeting = useQuery({
    queryKey: ["meeting", meetingId],
    queryFn: () => getMeeting(meetingId),
    refetchInterval: (q) =>
      q.state.data && statusInfo(q.state.data.status).busy ? 3000 : false,
  });

  const st = meeting.data ? statusInfo(meeting.data.status) : null;
  const hasTranscript =
    !!meeting.data &&
    ["diarized", "categorizing", "ready"].some((s) => meeting.data!.status.startsWith(s)) ||
    !!meeting.data?.status.startsWith("categorize_failed");

  const segments = useQuery({
    queryKey: ["segments", meetingId],
    queryFn: () => fetchAllSegments(meetingId),
    enabled: hasTranscript,
  });
  const speakers = useQuery({
    queryKey: ["speakers", meetingId],
    queryFn: () => fetchSpeakers(meetingId),
    enabled: hasTranscript,
  });

  // Saved export templates (IR §2.2.7) for the export picker; "default" = built-in.
  const templates = useQuery({ queryKey: ["templates"], queryFn: listTemplates });
  const [templateId, setTemplateId] = useState<number | "default">("default");
  const savedTemplates = templates.data ?? [];
  // The template actually chosen, or undefined for the built-in layout. Both
  // export buttons and the picker's own styling key off this, so a user can
  // see which layout they are about to get before they click.
  const chosenTemplate =
    templateId === "default" ? undefined : savedTemplates.find((t) => t.id === templateId);

  // --- search ---
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  useEffect(() => {
    if (q.trim().length < 2) {
      setHits(null);
      return;
    }
    const t = setTimeout(async () => {
      try {
        setHits(await searchTranscript(meetingId, q.trim()));
      } catch {
        setHits([]);
      }
    }, 300); // debounce typing
    return () => clearTimeout(t);
  }, [q, meetingId]);

  // --- audio + transcript coordination ---
  const audioRef = useRef<HTMLAudioElement>(null);
  const listRef = useRef<TranscriptListHandle>(null);
  const [activeSegmentId, setActiveSegmentId] = useState<number | null>(null);
  const lastIdxRef = useRef(-1);
  const [introOpen, dismissIntro] = useCollapse("review.intro");
  // Single-line "preview" playback: when set, pause once playback passes this time.
  const previewEndRef = useRef<number | null>(null);
  // seekTo() just armed a preview; consumed by the 'play' handler so a user-driven
  // play (the ▶ button) is recognised as continuous and clears the preview stop-point.
  const previewArmRef = useRef(false);

  // Highlight line `idx`, bringing it into view only when it actually changes (not
  // 4x/second). `align`: "center" for deliberate jumps (click a line / search hit) so
  // it lands mid-screen; "auto" for play-follow so we scroll the minimum amount and
  // only when the line has drifted off-screen — smooth following, never a yank.
  const highlight = (
    idx: number,
    segs: Segment[],
    align: "center" | "auto" | "none" = "auto",
  ) => {
    setActiveSegmentId(segs[idx].id);
    const changed = idx !== lastIdxRef.current;
    lastIdxRef.current = idx;
    // "none" updates the highlight but never moves the view — used while clicking /
    // previewing a line, since that line is already on screen. Otherwise scroll only
    // when the active line actually changes.
    if (changed && align !== "none") listRef.current?.scrollToIndex(idx, align);
  };

  // Keep the highlighted line in step with the audio. During continuous playback the
  // highlight follows the spoken line (scroll "auto", only when off-screen). A
  // single-line preview (clicking a timestamp) pauses once playback passes that
  // line's end, leaving the highlight parked on the previewed line.
  useEffect(() => {
    const audio = audioRef.current;
    const segs = segments.data;
    if (!audio || !segs?.length) return;
    const sync = () => {
      const previewing = previewEndRef.current !== null;
      if (previewing && audio.currentTime >= previewEndRef.current!) {
        previewEndRef.current = null;
        audio.pause(); // end of the previewed line; highlight stays where it is
        return;
      }
      // While previewing one line, only update the highlight — never scroll (the line
      // is already in view). Continuous playback follows with a gentle auto-scroll.
      highlight(segIndexAt(segs, audio.currentTime), segs, previewing ? "none" : "auto");
    };
    // A user-initiated play (the ▶ button) is continuous: drop any preview stop-point
    // so it doesn't pause at a previously-clicked line. seekTo() arms the flag first so
    // its OWN play() call isn't misread as continuous.
    const onPlay = () => {
      if (previewArmRef.current) previewArmRef.current = false;
      else previewEndRef.current = null;
    };
    audio.addEventListener("timeupdate", sync);
    audio.addEventListener("seeking", sync);
    audio.addEventListener("play", onPlay);
    return () => {
      audio.removeEventListener("timeupdate", sync);
      audio.removeEventListener("seeking", sync);
      audio.removeEventListener("play", onPlay);
    };
  }, [segments.data]);

  // Click a line's timestamp = "preview just this line": jump there, play, and
  // auto-pause at the line's end. The highlight is pinned to THAT line immediately
  // (centered) rather than waiting for the audio event, whose currentTime can snap a
  // hair below the line's start and pick the previous line (the seek bug). Press ▶
  // afterwards to resume normal continuous, synced playback from there.
  const seekTo = (start: number, end: number) => {
    const audio = audioRef.current;
    const segs = segments.data;
    if (!audio) return;
    previewEndRef.current = end;
    previewArmRef.current = true;
    audio.currentTime = start;
    audio.play().catch(() => {/* autoplay may need a user gesture */});
    // "none": the line you clicked is already on screen, so don't move the view at
    // all — scrolling it (even to the nearest edge) is what made the transcript "jump
    // to something else". Pressing ▶ for continuous playback is what scrolls to follow.
    if (segs?.length) highlight(segIndexAt(segs, start), segs, "none");
  };

  // Clicking a line to edit pauses playback so the audio doesn't roll on while typing.
  const pauseAudio = () => audioRef.current?.pause();

  const jumpToHit = (hit: SearchHit) => {
    if (segments.data?.length) highlight(hit.index, segments.data, "center");
    setHits(null);
    setQ("");
  };

  const jumpToSegment = (segmentId: number) => {
    const segs = segments.data;
    const idx = segs?.findIndex((s) => s.id === segmentId) ?? -1;
    if (segs && idx >= 0) highlight(idx, segs, "center");
  };

  return (
    <div className="review">
      <div className="review-head">
        <button className="ghost small" onClick={onBack} title="Return to your meetings list">
          ← Back to meetings
        </button>
        <h1>{meeting.data?.title ?? `Meeting ${meetingId}`}</h1>
        <LanguageChip codes={meeting.data?.languages_detected ?? null} />

        <div className="searchbox">
          <input
            placeholder="Search transcript…"
            aria-label="Search transcript"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            disabled={!hasTranscript}
          />
          {hits && (
            <div className="search-results">
              {hits.length === 0 && <div className="hit">No matches.</div>}
              {hits.map((h) => (
                <div key={h.segment_id} className="hit" onClick={() => jumpToHit(h)}>
                  <span className="t">#{h.index + 1}</span>
                  {h.snippet}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="export-group">
          {/* The picker is a labelled pill rather than a bare dropdown, and
              tints itself when a saved template is chosen: the commonest
              worry is "did it actually use MY layout?", so the control that
              governs both export buttons has to answer that at a glance. */}
          <div
            className={
              "template-picker" +
              (chosenTemplate ? " active" : "") +
              (hasTranscript ? "" : " disabled")
            }
            title={
              chosenTemplate
                ? `Both exports will be filled into your own document: ${chosenTemplate.name}`
                : "Exports use the system's built-in layout. Pick one of your own templates to use your house style."
            }
          >
            <span className="template-picker-label">Template</span>
            <span className="template-picker-value">
              {/* Hidden mirror of the SELECTED label: it sets the width and the
                  <select> is laid over it, so the pill hugs what it shows
                  instead of reserving room for the longest entry. */}
              <span aria-hidden="true" className="template-picker-sizer">
                {chosenTemplate ? chosenTemplate.name : "Default layout"}
              </span>
              <select
                value={String(templateId)}
                onChange={(e) =>
                  setTemplateId(e.target.value === "default" ? "default" : Number(e.target.value))
                }
                disabled={!hasTranscript}
                aria-label="Export template"
              >
                <optgroup label="Built in">
                  <option value="default">Default layout</option>
                </optgroup>
                {savedTemplates.length > 0 ? (
                  <optgroup label="Your templates">
                    {savedTemplates.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.name}
                      </option>
                    ))}
                  </optgroup>
                ) : (
                  <option value="none" disabled>
                    No saved templates — add one on the Templates tab
                  </option>
                )}
              </select>
            </span>
          </div>
          <a
            href={exportUrl(meetingId, templateId === "default" ? null : templateId)}
            download
          >
            <button disabled={!hasTranscript}>Export Word</button>
          </a>
          {/* Only offered once a previous meeting is linked — a "combined"
              document of one meeting would just be the ordinary export. */}
          {meeting.data?.follow_up_of && (
            <a
              href={combinedExportUrl(
                meetingId,
                templateId === "default" ? null : templateId,
              )}
              download
            >
              <button
                className="ghost"
                disabled={!hasTranscript}
                title={
                  "One document covering this meeting and every meeting it follows up, " +
                  "including progress on the previous actions" +
                  (chosenTemplate ? `, filled into ${chosenTemplate.name}` : "")
                }
              >
                Export combined
              </button>
            </a>
          )}
        </div>
      </div>

      {hasTranscript && introOpen && (
        <div className="review-intro">
          <button className="review-intro-x" onClick={dismissIntro} aria-label="Dismiss this tip">×</button>
          <div className="review-intro-title">New here? This page has five parts</div>
          <ol className="review-intro-zones">
            <li><b>① Transcript</b> — click a line's time to play just that line; click its text to edit.</li>
            <li><b>② Audio</b> — press ▶ for continuous playback; the spoken line follows along.</li>
            <li><b>③ Speakers</b> — rename a speaker once; it updates everywhere.</li>
            <li><b>④ Insights &amp; export</b> — generate the summary, then pick a template and Export Word (top-right).</li>
            <li><b>⑤ Follow-up</b> — link an earlier meeting to see what happened to everything agreed there, and export both as one document (your chosen template applies here too).</li>
          </ol>
        </div>
      )}

      {st?.busy && meeting.data && (
        <div className="banner info" style={{ margin: "10px 20px 0" }}>
          <ProgressBar meeting={meeting.data} />
          <div style={{ marginTop: 6, fontSize: 12 }}>This page updates automatically.</div>
        </div>
      )}
      {st?.failed && meeting.data && (
        <div className="banner error" style={{ margin: "10px 20px 0" }}>
          {st.label}
          {meeting.data.status.includes(":") &&
            ` — ${meeting.data.status.split(":").slice(1).join(":").trim()}`}
        </div>
      )}

      <div className="review-body">
        <div className="transcript-pane">
          <div className="zone-head">
            <span className="zone-num">1</span>Transcript
          </div>
          <div className="transcript-scroll" style={{ position: "relative" }}>
            {!hasTranscript && (
              <div className="center-note">
                {st?.failed
                  ? "Processing failed before a transcript was produced — see the status above."
                  : "Transcript will appear here once processing reaches the speaker-labeling stage."}
              </div>
            )}
            {hasTranscript && segments.data && segments.data.length === 0 && !segments.isLoading && (
              <div className="center-note">No speech was detected in this recording.</div>
            )}
            {hasTranscript && segments.data && segments.data.length > 0 && speakers.data && (
              <TranscriptList
                ref={listRef}
                meetingId={meetingId}
                segments={segments.data}
                speakers={speakers.data}
                activeSegmentId={activeSegmentId}
                onSeek={seekTo}
                onEdit={pauseAudio}
              />
            )}
            {hasTranscript && segments.isLoading && (
              <div className="center-note">Loading transcript…</div>
            )}
          </div>
          {hasTranscript && (
            <div className="audio-bar" style={{ display: "flex", alignItems: "center" }}>
              <span className="zone-num" title="Audio">2</span>
              <span className="zone-label">Audio</span>
              <audio ref={audioRef} controls preload="metadata" src={audioUrl(meetingId)} style={{ flex: 1, minWidth: 0 }} />
            </div>
          )}
        </div>

        <div className="side-pane panel">
          {hasTranscript && speakers.data && (
            <SpeakersPanel meetingId={meetingId} speakers={speakers.data} />
          )}
          <InsightsPanel
            meetingId={meetingId}
            meetingStatus={meeting.data?.status ?? ""}
            onJumpToSegment={jumpToSegment}
          />
          {hasTranscript && (
            <FollowUpPanel
              meetingId={meetingId}
              meeting={meeting.data}
              onJumpToSegment={jumpToSegment}
            />
          )}
        </div>
      </div>
    </div>
  );
}

/** Human names for the languages the system transcribes. */
const LANG_NAMES: Record<string, string> = {
  en: "English",
  ms: "Malay",
  zh: "Mandarin",
};

/** Shows which languages the recording actually turned out to contain.
 *
 * With three supported languages, a Malaysian meeting that code-switches is
 * the normal case rather than the exception — so this chip is the user's
 * confirmation that the mixing was recognised, and that a line reading in
 * another language is faithful capture rather than a transcription error. */
function LanguageChip({ codes }: { codes: string | null }) {
  const list = (codes ?? "").split(",").map((c) => c.trim()).filter(Boolean);
  if (list.length === 0) return null;
  const names = list.map((c) => LANG_NAMES[c] ?? c).join(" + ");
  const mixed = list.length > 1;
  return (
    <span
      className={`lang-chip ${mixed ? "mixed" : ""}`}
      title={
        mixed
          ? "This recording switches between languages. Every transcript line is kept in the language it was spoken, so you can check it against the audio."
          : "Only one language was detected in this recording."
      }
    >
      {mixed ? `Mixed: ${names}` : names}
    </span>
  );
}
