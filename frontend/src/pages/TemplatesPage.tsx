import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteTemplate,
  listTemplates,
  starterTemplateUrl,
  templateDownloadUrl,
  uploadTemplate,
  type Template,
} from "../api/client";
import { useCollapse } from "../components/useCollapse";
import ConfirmDialog from "../components/ConfirmDialog";

/** Manage user-uploaded Word export templates (IR §2.2.7).
 * Users design a normal Word doc (plain section headings or [[markers]] — no codes),
 * upload it here, then pick it when exporting any meeting. */

const FIELD_LABELS: Record<string, string> = {
  title: "Meeting Title", date: "Date", duration: "Duration", language: "Language",
  participants: "Participants", attendees: "Attendees", summary: "Summary",
  action_items: "Action Items", decisions: "Key Decisions", deadlines: "Deadlines",
  issues: "Technical Issues", risks: "Risks", transcript: "Full Transcript",
};
// The core sections users almost always want filled — flag any that AREN'T
// recognised (the usual cause is a misspelt heading, e.g. "Riks" → "Risks").
const CORE_FIELDS = ["summary", "action_items", "decisions", "deadlines", "issues", "risks"];
const fieldLabel = (k: string) => FIELD_LABELS[k] ?? k;

export default function TemplatesPage() {
  const qc = useQueryClient();
  const templates = useQuery({ queryKey: ["templates"], queryFn: listTemplates });

  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [guideOpen, toggleGuide] = useCollapse("guide.templates");
  const [pendingDelete, setPendingDelete] = useState<Template | null>(null);

  const upload = useMutation({
    mutationFn: () => uploadTemplate(name.trim(), file as File),
    onSuccess: () => {
      setName("");
      setFile(null);
      qc.invalidateQueries({ queryKey: ["templates"] });
    },
  });

  const remove = useMutation({
    mutationFn: deleteTemplate,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["templates"] }),
  });

  return (
    <div className="meetings-page">
      <div className="guide">
        <button className="guide-head" onClick={toggleGuide} aria-expanded={guideOpen}>
          <span>Use your own Word layout — no codes needed</span>
          <span className="guide-chevron" aria-hidden="true">{guideOpen ? "▾ hide" : "▸ show"}</span>
        </button>
        {guideOpen && (
          <>
        <p className="guide-scope" style={{ marginBottom: 10, marginTop: 10 }}>
          The system writes each meeting's details into <b>your</b> document and keeps your fonts,
          logo and design. Quickest start: <b>download the sample below</b> — it already works as-is,
          just restyle it to taste.
        </p>
        <ol>
          <li>
            <b>Get a Word document</b> — download the sample (recommended), or use any doc of your own.
          </li>
          <li>
            <b>Show where each thing should go</b>, in either of two simple ways:
            <div className="how-examples">
              <div className="how-ex">
                <div className="how-ex-label">1 · Write a section heading</div>
                <div>You type: <code>Action Items</code> <span style={{ color: "var(--muted)" }}>(or <code>Key Actions</code> — near-misses work)</span></div>
                <div className="how-ex-arrow">→ we fill the action-items list right underneath it</div>
              </div>
              <div className="how-ex">
                <div className="how-ex-label">2 · Or a label in a table</div>
                <div>You type: <code>Date</code> and leave the next cell blank</div>
                <div className="how-ex-arrow">→ we fill the cell beside it: 17 June 2026</div>
              </div>
            </div>
            Both work in <b>English or Chinese</b> (e.g. <code>决策</code> or <code>日期</code>).
          </li>
          <li>
            <b>Upload it here, then export.</b> Give it a name and upload (we check it has at least
            one section we recognise). Then open a finished meeting and pick your template from the
            dropdown next to <b>Export Word</b>.
          </li>
        </ol>
        <p className="guide-scope">
          <b>Names we recognise</b> (as a heading <i>or</i> a table label, any language): Meeting Title ·
          Date · Duration · Language · Attendees · Summary · Key Decisions · Action Items · Deadlines ·
          Technical Issues · Risks · Transcript. Anything else is left exactly as you wrote it.
        </p>
          </>
        )}
        <a href={starterTemplateUrl()} download style={{ display: "inline-block", marginTop: 14 }}>
          <button className="ghost">⬇ Download the sample template</button>
        </a>
      </div>

      <div className="upload-form">
        <input
          placeholder="Template name (e.g. Acme Corp minutes)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          style={{ flex: 1, minWidth: 200 }}
        />
        <button className="ghost" onClick={() => fileInput.current?.click()}>
          {file ? `📄 ${file.name}` : "Choose .docx…"}
        </button>
        <input
          ref={fileInput}
          type="file"
          accept=".docx"
          hidden
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        <button
          disabled={!name.trim() || !file || upload.isPending}
          onClick={() => upload.mutate()}
        >
          {upload.isPending ? "Checking & saving…" : "Upload template"}
        </button>
      </div>
      {upload.isError && (
        <div className="banner error">{(upload.error as Error).message}</div>
      )}
      {remove.isError && (
        <div className="banner error">Couldn't delete: {(remove.error as Error).message}</div>
      )}
      {upload.data?.recognised && <UploadFeedback template={upload.data} />}

      {templates.isLoading && <div className="center-note">Loading templates…</div>}
      {templates.isError && (
        <div className="banner error">
          Cannot reach the backend at 127.0.0.1:8000 — is uvicorn running?
        </div>
      )}

      <div className="meeting-list">
        {(templates.data ?? []).map((t: Template) => (
          <div key={t.id} className="meeting-card" style={{ cursor: "default" }}>
            <div className="meeting-card-row">
              <div>
                <div className="title">{t.name}</div>
                <div className="meta">
                  {t.original_filename} · added {t.created_at.slice(0, 16).replace("T", " ")}
                </div>
              </div>
              <div className="card-actions">
                <a href={templateDownloadUrl(t.id)} download>
                  <button className="ghost small">Download</button>
                </a>
                <button
                  className="ghost small danger"
                  disabled={remove.isPending}
                  onClick={() => setPendingDelete(t)}
                >
                  Delete
                </button>
              </div>
            </div>
          </div>
        ))}
        {templates.data?.length === 0 && (
          <div className="center-note">
            No templates yet — download the sample, lay out your sections in Word, and upload it above.
          </div>
        )}
      </div>
      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this template?"
        message={
          pendingDelete
            ? `"${pendingDelete.name}" is removed from the export options. This cannot be undone.`
            : ""
        }
        onConfirm={() => {
          if (pendingDelete) remove.mutate(pendingDelete.id);
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}

/** After an upload, confirm what the engine recognised and flag any core section
 * it could NOT find (the typical cause is a misspelt heading, e.g. "Riks"). */
function UploadFeedback({ template }: { template: Template }) {
  const recog = template.recognised ?? [];
  const missing = CORE_FIELDS.filter((k) => !recog.includes(k));
  return (
    <div className="banner info">
      <b>✓ Saved “{template.name}”.</b> Recognised {recog.length} field{recog.length === 1 ? "" : "s"}:{" "}
      {recog.map(fieldLabel).join(", ")}.
      {missing.length > 0 && (
        <div style={{ marginTop: 6, color: "var(--danger)" }}>
          ⚠ Not recognised, so these won't be filled: <b>{missing.map(fieldLabel).join(", ")}</b> — check the
          heading spelling (e.g. “Riks” → “Risks”) or add a [[marker]].
        </div>
      )}
    </div>
  );
}
