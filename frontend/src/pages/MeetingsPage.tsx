import { useCallback, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteMeeting,
  fmtTime,
  listMeetings,
  statusInfo,
  uploadMeeting,
  type Meeting,
} from "../api/client";
import ConfirmDialog from "../components/ConfirmDialog";
import ProgressBar from "../components/ProgressBar";
import Stepper from "../components/Stepper";
import { useCollapse } from "../components/useCollapse";

const ACCEPTED = ".mp4,.mp3,.wav,.m4a,.webm";
const ACCEPTED_EXTS = ["mp4", "mp3", "wav", "m4a", "webm"];
const MAX_UPLOAD_GB = 4; // mirrors the backend MAX_UPLOAD_GB cap

export default function MeetingsPage({ onOpen }: { onOpen: (id: number) => void }) {
  const qc = useQueryClient();
  const meetings = useQuery({
    queryKey: ["meetings"],
    queryFn: listMeetings,
    // Poll while anything is still processing so status pills stay live.
    refetchInterval: (q) =>
      (q.state.data ?? []).some((m) => statusInfo(m.status).busy) ? 3000 : false,
  });

  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [lang, setLang] = useState("en");
  const [speakers, setSpeakers] = useState<string>("");
  const [drag, setDrag] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [guideOpen, toggleGuide] = useCollapse("guide.meetings");

  const upload = useMutation({
    mutationFn: uploadMeeting,
    onSuccess: () => {
      setFile(null);
      setTitle("");
      setSpeakers("");
      qc.invalidateQueries({ queryKey: ["meetings"] });
    },
  });

  // Delete a meeting from the dashboard, guarded by an in-app confirmation.
  const [confirmDelete, setConfirmDelete] = useState<Meeting | null>(null);
  const del = useMutation({
    mutationFn: deleteMeeting,
    onSuccess: () => {
      setConfirmDelete(null);
      qc.invalidateQueries({ queryKey: ["meetings"] });
    },
  });

  const pickFile = useCallback((f: File | null) => {
    if (!f) return;
    const ext = f.name.split(".").pop()?.toLowerCase() ?? "";
    if (!ACCEPTED_EXTS.includes(ext)) {
      setFileError(`Unsupported file type ".${ext}". Use MP4, MP3, WAV, M4A or WebM.`);
      return;
    }
    if (f.size > MAX_UPLOAD_GB * 1024 ** 3) {
      setFileError(`That file is ${(f.size / 1024 ** 3).toFixed(1)} GB — the limit is ${MAX_UPLOAD_GB} GB.`);
      return;
    }
    setFileError(null);
    setFile(f);
    if (!title) setTitle(f.name.replace(/\.[^.]+$/, ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [title]);

  return (
    <div className="meetings-page">
      <Stepper current="upload" />
      <div className="guide">
        <button className="guide-head" onClick={toggleGuide} aria-expanded={guideOpen}>
          <span>How it works</span>
          <span className="guide-chevron" aria-hidden="true">{guideOpen ? "▾ hide" : "▸ show"}</span>
        </button>
        {guideOpen && (
          <>
            <p className="guide-privacy">
              Everything runs on your own computer — nothing is uploaded to the cloud.
            </p>
            <ol>
          <li>
            <b>Upload a recording.</b> MP4, MP3, WAV, M4A or WebM (up to 4 GB). Choose the
            spoken language, and — if you know it — how many people are in the meeting
            (this sharpens speaker detection).
          </li>
          <li>
            <b>It processes automatically.</b> The system extracts the audio, transcribes the
            speech, aligns each word to the audio, works out who spoke when, then writes a
            summary and insights with a local AI model. A ~100-minute meeting takes roughly
            10 minutes — you can leave this page, the status updates live.
          </li>
          <li>
            <b>Review &amp; export.</b> Open the meeting to read and correct the transcript,
            rename speakers, generate the summary and categories, then download a formatted
            Word document.
          </li>
          <li>
            <b>Chain a follow-up meeting.</b> Mark a meeting as the follow-up of an earlier
            one and the system checks what happened to every action agreed there — done,
            under way, stuck or never mentioned — then exports the whole series as one
            document.
          </li>
        </ol>
            <p className="guide-scope">
              <b>What you can do:</b> edit any line, reassign or rename speakers, search, play the
              audio in sync, and (re)generate insights.{" "}
              <b>What you get:</b> a speaker-labelled transcript plus Action Items, Key Decisions,
              Deadlines, Technical Issues and Risks — each linked to the exact line it came from.
            </p>
          </>
        )}
      </div>

      {/* Drop zone: drag a file in, or click anywhere to browse (Option C). */}
      <div
        className={`dropzone ${drag ? "drag" : ""}`}
        role="button"
        tabIndex={0}
        onClick={() => fileInput.current?.click()}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.current?.click(); } }}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          pickFile(e.dataTransfer.files?.[0] ?? null);
        }}
      >
        <div>Drop your meeting recording here, or click to browse</div>
        <div className="hint">MP4, MP3, WAV, M4A or WebM - up to 4 GB</div>
        <input
          ref={fileInput}
          type="file"
          accept={ACCEPTED}
          hidden
          onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
        />
      </div>

      {fileError && <div className="banner error">{fileError}</div>}

      {file && (
        <div className="upload-form">
          <span className="filename">{file.name}</span>
          <input
            placeholder="Meeting title"
            aria-label="Meeting title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            style={{ flex: 1, minWidth: 180 }}
          />
          <select value={lang} onChange={(e) => setLang(e.target.value)} aria-label="Output language" title="The single language your summary, insights and Word document will be in. A mixed-language recording is produced in this one language.">
            <option value="en">English</option>
            <option value="zh">Chinese (Mandarin)</option>
            <option value="ms">Malay (Bahasa Melayu)</option>
            <option value="auto">Auto-detect</option>
          </select>
          <div className="speaker-field">
            <label htmlFor="spk-count">How many people are in this meeting?</label>
            <input
              id="spk-count"
              type="number"
              min={1}
              max={20}
              placeholder="e.g. 5 — optional"
              value={speakers}
              onChange={(e) => setSpeakers(e.target.value)}
            />
            <span className="help">
              Improves speaker accuracy. Leave blank to auto-detect (may over-count).
            </span>
          </div>
          <div
            className="mixed-note"
            style={{ flexBasis: "100%", fontSize: 12, color: "#555", lineHeight: 1.5 }}
          >
            ℹ️ <b>Mixed English, Malay and Mandarin?</b> Any combination is detected
            automatically — you don't have to say which. The{" "}
            <b>transcript is shown exactly as spoken</b>, switching language line by line (so you
            can check it against the recording), while the{" "}
            <b>summary, insights and Word document</b> are written in the one language you choose
            above.
          </div>
          <button
            disabled={!title.trim() || upload.isPending}
            onClick={() =>
              upload.mutate({
                title: title.trim(),
                file,
                primaryLanguage: lang,
                expectedSpeakers: speakers ? Number(speakers) : null,
              })
            }
          >
            {upload.isPending ? "Uploading…" : "Upload & process"}
          </button>
        </div>
      )}
      {upload.isError && (
        <div className="banner error">Upload failed: {(upload.error as Error).message}</div>
      )}

      {meetings.isLoading && <div className="center-note">Loading meetings…</div>}
      {meetings.isError && (
        <div className="banner error">
          Cannot reach the backend at 127.0.0.1:8000 - is uvicorn running?
        </div>
      )}

      <div className="meeting-list">
        {(meetings.data ?? []).map((m: Meeting) => {
          const st = statusInfo(m.status);
          return (
            <div key={m.id} className="meeting-card" onClick={() => onOpen(m.id)}>
              <div className="meeting-card-row">
                <div>
                  <div className="title">{m.title}</div>
                  <div className="meta">
                    {m.original_filename}
                    {m.duration_seconds ? ` · ${fmtTime(m.duration_seconds)}` : ""}
                    {` · uploaded ${m.created_at.slice(0, 16).replace("T", " ")}`}
                  </div>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span
                    className={`status-pill ${st.failed ? "failed" : ""} ${m.status === "ready" ? "ready" : ""}`}
                    title={m.status}
                  >
                    {st.label}
                  </span>
                  <button
                    className="small modal-danger"
                    title="Delete this meeting"
                    aria-label={`Delete meeting: ${m.title}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      setConfirmDelete(m);
                    }}
                  >
                    Delete
                  </button>
                </div>
              </div>
              <ProgressBar meeting={m} />
            </div>
          );
        })}
        {meetings.data?.length === 0 && (
          <div className="center-note">No meetings yet - upload your first recording above.</div>
        )}
      </div>

      <ConfirmDialog
        open={confirmDelete !== null}
        title="Delete meeting?"
        message={
          confirmDelete
            ? `"${confirmDelete.title}" and its transcript, insights and files will be permanently deleted. This cannot be undone.` +
              (del.isError ? ` (Previous attempt failed: ${(del.error as Error).message})` : "")
            : ""
        }
        confirmLabel={del.isPending ? "Deleting…" : "Delete"}
        onConfirm={() => confirmDelete && del.mutate(confirmDelete.id)}
        onCancel={() => setConfirmDelete(null)}
      />
    </div>
  );
}
