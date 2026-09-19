/** Typed client for the FastAPI backend.
 *
 * The backend runs on 127.0.0.1:8000 and already allows CORS from the
 * Vite dev server (:5173), so we call it directly - no proxy needed.
 */
export const API = "http://127.0.0.1:8000";

export interface Meeting {
  id: number;
  title: string;
  original_filename: string;
  duration_seconds: number | null;
  language: string | null;
  languages_detected: string | null;   // e.g. "en,ms" or "en,ms,zh" when code-switch detected
  primary_language: string;
  expected_speakers: number | null;
  status: string;
  progress?: number | null;
  follow_up_of?: number | null;          // the meeting this one follows up, if any
  carry_forward_status?: string | null;  // null | 'analysing' | 'ready' | 'failed: …'
  created_at: string;
  updated_at: string;
}

export interface Speaker {
  id: number;
  meeting_id: number;
  label: string;
  display_name: string | null;
}

export interface Segment {
  id: number;
  meeting_id: number;
  speaker_id: number | null;
  start_seconds: number;
  end_seconds: number;
  text: string;
  language: string | null;
}

export interface Word {
  id: number;
  segment_id: number;
  start_seconds: number;
  end_seconds: number;
  text: string;
  score: number | null;
}

export interface PagedSegments {
  meeting_id: number;
  total: number;
  offset: number;
  limit: number;
  segments: Segment[];
}

export interface SearchHit {
  segment_id: number;
  index: number;
  start_seconds: number;
  snippet: string;
}

export interface InsightItem {
  id: number;
  description: string;
  owner?: string | null;
  due_date?: string | null;
  target_date?: string | null;
  severity?: string | null;
  mitigation?: string | null;
  source_segment_id?: number | null;
  low_support?: boolean; // cited line may not back this item — show a verify hint
}

export interface Insights {
  meeting_id: number;
  status: string;
  summary: string | null;
  action_items: InsightItem[];
  decisions: InsightItem[];
  deadlines: InsightItem[];
  issues: InsightItem[];
  risks: InsightItem[];
}

export interface Template {
  id: number;
  name: string;
  original_filename: string;
  created_at: string;
  recognised?: string[] | null; // field keys found in the doc (set on upload only)
}

/** One of the PREVIOUS meeting's action items, plus what this meeting said
 * about it. `source_segment_id` is the line here that evidences the verdict. */
export interface CarryForwardItem {
  id: number;
  previous_meeting_id: number;
  previous_action_id?: number | null;
  description: string;
  owner?: string | null;
  status: string; // completed | in_progress | blocked | changed | not_discussed
  note?: string | null;
  source_segment_id?: number | null;
}

export interface CarryForward {
  meeting_id: number;
  previous_meeting_id: number | null;
  previous_meeting_title?: string | null;
  status: string | null; // null | 'analysing' | 'ready' | 'failed: …'
  items: CarryForwardItem[];
}

async function check(res: Response): Promise<Response> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res;
}

export async function listMeetings(): Promise<Meeting[]> {
  return (await check(await fetch(`${API}/meetings`))).json();
}

export async function getMeeting(id: number): Promise<Meeting> {
  return (await check(await fetch(`${API}/meetings/${id}`))).json();
}

export async function deleteMeeting(id: number): Promise<void> {
  await check(await fetch(`${API}/meetings/${id}`, { method: "DELETE" }));
}

export async function uploadMeeting(opts: {
  title: string;
  file: File;
  primaryLanguage: string;
  expectedSpeakers?: number | null;
}): Promise<Meeting> {
  const form = new FormData();
  form.append("title", opts.title);
  form.append("primary_language", opts.primaryLanguage);
  if (opts.expectedSpeakers) form.append("expected_speakers", String(opts.expectedSpeakers));
  form.append("file", opts.file);
  return (await check(await fetch(`${API}/meetings`, { method: "POST", body: form }))).json();
}

/** Fetch ALL segments by walking the paged endpoint (500/slice).
 * Even a 2-hour meeting is ~1000 segments (~250 KB) - fine to hold in
 * memory; the virtualized list keeps RENDERING cheap. */
export async function fetchAllSegments(meetingId: number): Promise<Segment[]> {
  const out: Segment[] = [];
  let offset = 0;
  for (;;) {
    const page: PagedSegments = await (
      await check(await fetch(`${API}/meetings/${meetingId}/segments?offset=${offset}&limit=500`))
    ).json();
    out.push(...page.segments);
    offset += page.segments.length;
    if (offset >= page.total || page.segments.length === 0) break;
  }
  return out;
}

export async function fetchSpeakers(meetingId: number): Promise<Speaker[]> {
  const t = await (await check(await fetch(`${API}/meetings/${meetingId}/transcript?include_words=false`))).json();
  return t.speakers as Speaker[];
}

export async function fetchWords(meetingId: number, segmentId: number): Promise<Word[]> {
  return (
    await check(await fetch(`${API}/meetings/${meetingId}/segments/${segmentId}/words`))
  ).json();
}

export async function patchSegment(
  meetingId: number,
  segmentId: number,
  body: { text?: string; speaker_id?: number | null },
): Promise<Segment> {
  return (
    await check(
      await fetch(`${API}/meetings/${meetingId}/segments/${segmentId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    )
  ).json();
}

export async function patchSpeaker(
  meetingId: number,
  speakerId: number,
  displayName: string | null,
): Promise<Speaker> {
  return (
    await check(
      await fetch(`${API}/meetings/${meetingId}/speakers/${speakerId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: displayName }),
      }),
    )
  ).json();
}

export async function searchTranscript(meetingId: number, q: string): Promise<SearchHit[]> {
  return (
    await check(
      await fetch(`${API}/meetings/${meetingId}/search?q=${encodeURIComponent(q)}`),
    )
  ).json();
}

export async function getInsights(meetingId: number): Promise<Insights> {
  return (await check(await fetch(`${API}/meetings/${meetingId}/insights`))).json();
}

export async function triggerCategorize(meetingId: number): Promise<void> {
  await check(await fetch(`${API}/meetings/${meetingId}/categorize`, { method: "POST" }));
}

export type InsightCategory = "action_items" | "decisions" | "deadlines" | "issues" | "risks";

/** Edit one extracted item (human-in-the-loop correction). */
export async function updateInsightItem(
  meetingId: number,
  category: InsightCategory,
  itemId: number,
  body: Partial<Pick<InsightItem, "description" | "owner" | "due_date" | "target_date" | "mitigation" | "severity">>,
): Promise<InsightItem> {
  return (
    await check(
      await fetch(`${API}/meetings/${meetingId}/insights/${category}/${itemId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    )
  ).json();
}

/** Delete one extracted item (e.g. a hallucinated or irrelevant one). */
export async function deleteInsightItem(
  meetingId: number,
  category: InsightCategory,
  itemId: number,
): Promise<void> {
  await check(
    await fetch(`${API}/meetings/${meetingId}/insights/${category}/${itemId}`, { method: "DELETE" }),
  );
}

// ===== Export templates (IR §2.2.7) =====

export async function listTemplates(): Promise<Template[]> {
  return (await check(await fetch(`${API}/templates`))).json();
}

export async function uploadTemplate(name: string, file: File): Promise<Template> {
  const form = new FormData();
  form.append("name", name);
  form.append("file", file);
  return (await check(await fetch(`${API}/templates`, { method: "POST", body: form }))).json();
}

export async function deleteTemplate(id: number): Promise<void> {
  await check(await fetch(`${API}/templates/${id}`, { method: "DELETE" }));
}

export const templateDownloadUrl = (id: number) => `${API}/templates/${id}/download`;
export const starterTemplateUrl = () => `${API}/templates/starter/download`;

/** Link this meeting to the earlier one it follows up (null unlinks it).
 * Linking starts the carry-forward analysis on the backend. */
export async function setFollowUp(
  meetingId: number,
  previousMeetingId: number | null,
): Promise<Meeting> {
  return (
    await check(
      await fetch(`${API}/meetings/${meetingId}/follow-up`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ previous_meeting_id: previousMeetingId }),
      }),
    )
  ).json();
}

export async function getCarryForward(meetingId: number): Promise<CarryForward> {
  return (await check(await fetch(`${API}/meetings/${meetingId}/carry-forward`))).json();
}

export async function rerunCarryForward(meetingId: number): Promise<void> {
  await check(await fetch(`${API}/meetings/${meetingId}/carry-forward`, { method: "POST" }));
}

/** Correct one carry-forward verdict by hand (human-in-the-loop). */
export async function updateCarryForwardItem(
  meetingId: number,
  itemId: number,
  body: Partial<Pick<CarryForwardItem, "description" | "owner" | "status" | "note">>,
): Promise<CarryForwardItem> {
  return (
    await check(
      await fetch(`${API}/meetings/${meetingId}/carry-forward/${itemId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    )
  ).json();
}

/** Drop one carry-forward row from this meeting's follow-up list. */
export async function deleteCarryForwardItem(
  meetingId: number,
  itemId: number,
): Promise<void> {
  await check(
    await fetch(`${API}/meetings/${meetingId}/carry-forward/${itemId}`, { method: "DELETE" }),
  );
}

/** Render a stored timestamp in the viewer's own timezone.
 *
 * SQLite's CURRENT_TIMESTAMP is UTC and is stored with no zone marker
 * ("2026-09-19 10:20:46"). Slicing that string straight into the UI showed
 * every meeting eight hours early in Malaysia (UTC+8) - and `new Date(...)`
 * would not have helped, because browsers read a space-separated, unmarked
 * timestamp as LOCAL time. Tagging it "Z" first is what makes it correct.
 */
function asUtcDate(stored: string): Date | null {
  if (!stored) return null;
  const text = stored.trim().replace(" ", "T");
  const iso = /[Zz]|[+-]\d{2}:?\d{2}$/.test(text) ? text : `${text}Z`;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "2026-09-19 18:20" in local time, for timestamps shown beside a meeting. */
export function localDateTime(stored: string): string {
  const d = asUtcDate(stored);
  if (!d) return stored.slice(0, 16).replace("T", " ");
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "2026-09-19" in local time - the date can differ from the UTC one. */
export function localDate(stored: string): string {
  const d = asUtcDate(stored);
  if (!d) return stored.slice(0, 10);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export const audioUrl = (meetingId: number) => `${API}/meetings/${meetingId}/audio`;
const templateQuery = (templateId?: number | null) =>
  templateId ? `?template_id=${templateId}` : "";

/** Export URL; pass a templateId to fill a saved template instead of the default. */
export const exportUrl = (meetingId: number, templateId?: number | null) =>
  `${API}/meetings/${meetingId}/export/docx${templateQuery(templateId)}`;
/** One document covering this meeting and every meeting it follows up. A
 * templateId fills the user's own document with the whole series (each entry
 * tagged with the meeting it came from) instead of the built-in layout. */
export const combinedExportUrl = (meetingId: number, templateId?: number | null) =>
  `${API}/meetings/${meetingId}/export/combined${templateQuery(templateId)}`;

/** How each carry-forward verdict is worded in the UI. Keys match the backend's
 * stored values; `not_discussed` is the one the SYSTEM assigns when the model
 * found no evidence, never something it claimed. */
export const CARRY_FORWARD_LABELS: Record<string, string> = {
  completed: "Completed",
  in_progress: "In progress",
  blocked: "Blocked",
  changed: "Changed",
  not_discussed: "Not discussed",
};

/** Human label + "is the pipeline still running?" for a status value. */
export function statusInfo(status: string): { label: string; busy: boolean; failed: boolean } {
  if (status.startsWith("error")) return { label: "Failed", busy: false, failed: true };
  if (status.startsWith("categorize_failed"))
    return { label: "Transcript ready (insights failed)", busy: false, failed: true };
  const map: Record<string, { label: string; busy: boolean }> = {
    uploading: { label: "Uploading…", busy: true },
    uploaded: { label: "Queued…", busy: true },
    audio_extracted: { label: "Detecting speech…", busy: true },
    vad_done: { label: "Detecting speech…", busy: true },
    transcribing: { label: "Transcribing…", busy: true },
    aligning: { label: "Aligning words…", busy: true },
    diarizing: { label: "Identifying speakers…", busy: true },
    // 'diarized' is a RESTING state: transcript done, insights not generated.
    // (During a live run it transitions to 'categorizing' within seconds, so
    // any meeting sitting at diarized is waiting for the user, not working.)
    diarized: { label: "Transcript ready", busy: false },
    categorizing: { label: "Extracting insights…", busy: true },
    ready: { label: "Ready", busy: false },
  };
  const hit = map[status] ?? { label: status, busy: false };
  return { ...hit, failed: false };
}

export function speakerName(s: Speaker): string {
  return s.display_name || s.label;
}

export function fmtTime(sec: number): string {
  const s = Math.floor(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`
    : `${m}:${String(r).padStart(2, "0")}`;
}
