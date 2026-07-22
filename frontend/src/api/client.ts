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
  languages_detected: string | null;   // e.g. "en,zh" when code-switch detected
  primary_language: string;
  expected_speakers: number | null;
  status: string;
  progress?: number | null;
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

export const audioUrl = (meetingId: number) => `${API}/meetings/${meetingId}/audio`;
/** Export URL; pass a templateId to fill a saved template instead of the default. */
export const exportUrl = (meetingId: number, templateId?: number | null) =>
  `${API}/meetings/${meetingId}/export/docx${templateId ? `?template_id=${templateId}` : ""}`;

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
