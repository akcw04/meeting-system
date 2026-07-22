import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { patchSpeaker, type Speaker } from "../api/client";

/** Rename "Speaker 3" -> "Ms. Lai". The display name flows everywhere:
 * transcript rows, insight owners, and the exported Word document. */
export default function SpeakersPanel({
  meetingId,
  speakers,
}: {
  meetingId: number;
  speakers: Speaker[];
}) {
  return (
    <div>
      <h2 style={{ marginTop: 0 }}><span className="zone-num">3</span>Speakers</h2>
      <p className="panel-intro">
        Rename a speaker once (e.g. "Speaker 3" → "Ms. Lai") and it updates everywhere —
        transcript, insight owners, and the Word export.
      </p>
      {speakers.length === 0 && <div className="empty-note">No speakers detected.</div>}
      {speakers.map((sp) => (
        <SpeakerRow key={sp.id} meetingId={meetingId} speaker={sp} />
      ))}
    </div>
  );
}

function SpeakerRow({ meetingId, speaker }: { meetingId: number; speaker: Speaker }) {
  const qc = useQueryClient();
  const [name, setName] = useState(speaker.display_name ?? "");

  const rename = useMutation({
    mutationFn: (displayName: string | null) =>
      patchSpeaker(meetingId, speaker.id, displayName),
    onSuccess: (updated) => {
      qc.setQueryData<Speaker[]>(["speakers", meetingId], (old) =>
        old?.map((s) => (s.id === updated.id ? updated : s)),
      );
    },
  });

  const commit = () => {
    const trimmed = name.trim();
    if ((speaker.display_name ?? "") !== trimmed) {
      rename.mutate(trimmed || null);
    }
  };

  return (
    <div className="speaker-row">
      <span className="label">{speaker.label}</span>
      <input
        placeholder="Real name (optional)"
        value={name}
        onChange={(e) => setName(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
      {rename.isPending && <span className="saving">saving…</span>}
      {rename.isError && (
        <span className="saving" style={{ color: "var(--danger)" }} title={(rename.error as Error).message}>
          save failed — re-enter to retry
        </span>
      )}
    </div>
  );
}
