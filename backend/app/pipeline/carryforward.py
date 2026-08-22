"""Cross-meeting carry-forward analysis (Session 22; reliability rework 2026-08-21).

A meeting rarely stands alone. When one meeting is the follow-up of an earlier
one, the question a minute-taker has to answer is "what happened to everything we
agreed last time?" - and answering it by hand means reading two sets of minutes
side by side. This module answers it automatically.

Given meeting B marked as the follow-up of meeting A, we take A's action items and
read B's transcript for evidence about each one, classifying it as:

    completed     - B says the task is done
    in_progress   - B says it is under way but not finished
    blocked       - B says it is stuck, waiting on something
    changed       - B revised, replaced or dropped the task
    not_discussed - B never mentioned it (assigned in CODE, not by the model)

Reliability rework (addresses report limitation 6.2.4). The first design sent the
WHOLE action list and a transcript chunk to the model at once and asked it to
match everything simultaneously - which an 8B model could not do across a
trilingual (English / Malay / Mandarin) transcript, misclassifying every item on
the test pair. Two changes make the task tractable:

  * Normalise to English first (B). Action items AND the transcript are translated
    to English by the model before any matching, so it never has to reason across
    three languages at once.
  * Per-item retrieval + focused classification (A). For EACH action item we
    retrieve only the handful of most relevant transcript segments (TF-IDF cosine)
    and ask the model about THAT ONE item against THAT small evidence set - a far
    easier judgement than matching a whole list against a whole transcript.

The anti-hallucination guarantees are unchanged: a verdict is kept only if it
cites a segment we actually sent (grounding, mapped back to the real segment id),
'not_discussed' is filled in by code for anything left ungrounded, and the model
refers to segments by a small local number, never a database id (the bug #17/#21
principle - don't hand an 8B model a job a lookup table does perfectly).
"""
from __future__ import annotations

from sklearn.feature_extraction.text import TfidfVectorizer

from app.db import get_conn
from app.pipeline.categorize import _chat

# The four verdicts the model may return for an action item. 'not_discussed' is
# never one the model is trusted to return - it is what the code assigns when no
# grounded verdict comes back, so a silent failure reads as "no evidence found"
# rather than invented progress.
_CLASSIFY_STATUSES = {"completed", "in_progress", "blocked", "changed"}

# What the code assigns when no grounded verdict came back for an action.
NOT_DISCUSSED = "not_discussed"

# How many transcript segments to put in front of the model per action item.
_RETRIEVE_K = 8


class NoPreviousMeeting(ValueError):
    """This meeting is not linked to an earlier one."""


def _previous_actions(previous_meeting_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, description, owner, due_date FROM action_items "
            "WHERE meeting_id = ? ORDER BY id",
            (previous_meeting_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _translate_to_english(texts: list[str], progress=print, batch: int = 25) -> list[str]:
    """Translate each string to English via the model, preserving order and count.

    Already-English text passes through unchanged. On any failure a text falls
    back to its original wording, so the analysis still runs (just cross-lingual
    for that one line) rather than aborting the whole pass.
    """
    out = list(texts)
    for start in range(0, len(texts), batch):
        block = texts[start:start + batch]
        listing = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(block))
        system = (
            "You are a translator. Translate each numbered line into natural "
            "English. If a line is already in English, return it unchanged. "
            "Preserve names, numbers and dates exactly. Do not add, drop or merge "
            "lines.\n"
            'Return ONLY JSON: {"lines": [{"i": 1, "en": "..."}]}'
        )
        try:
            raw = _chat(system=system, user=listing)
        except Exception as exc:  # noqa: BLE001 - translation is best-effort
            progress(f"[carry-forward] translation batch skipped: {type(exc).__name__}")
            continue
        got = raw.get("lines") if isinstance(raw, dict) else None
        if not isinstance(got, list):
            continue
        for item in got:
            if not isinstance(item, dict):
                continue
            try:
                i = int(item.get("i"))
            except (TypeError, ValueError):
                continue
            en = item.get("en")
            if en and 1 <= i <= len(block):
                out[start + i - 1] = str(en).strip()
    return out


def _retrieve_relevant(
    action_en: str, segment_texts_en: list[str], k: int = _RETRIEVE_K
) -> list[int]:
    """Indices of the segments most relevant to an action item, by TF-IDF cosine.

    Everything is English by the time this runs, so lexical similarity is a sound,
    dependency-light retriever. Falls back to the first k segments if the action
    shares no vocabulary with any segment, so the model always gets something to
    judge and can still answer 'not_discussed'.
    """
    if not segment_texts_en:
        return []
    try:
        vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
        matrix = vec.fit_transform(segment_texts_en + [action_en])
        # TF-IDF rows are L2-normalised, so this dot product IS cosine similarity.
        sims = (matrix[:-1] @ matrix[-1].T).toarray().ravel()
    except Exception:  # noqa: BLE001 - never let retrieval sink the pass
        return list(range(min(k, len(segment_texts_en))))
    order = list(sims.argsort()[::-1])
    top = [int(i) for i in order if sims[i] > 0][:k]
    if not top:
        top = [int(i) for i in order[:k]]
    return sorted(top)  # chronological order reads naturally as excerpts


def _classify_action(
    action_en: str, excerpts: list[tuple[int, str]]
) -> tuple[str | None, str | None, list[int]]:
    """Ask the model what became of ONE action item, given a few excerpts.

    `excerpts` is [(local_number, english_text), ...]. Returns
    (status, note, cited_local_numbers). Returns (None, None, []) - which the
    caller records as not_discussed - when the model declines, returns a status
    outside the four, or cites nothing we actually sent (ungrounded).
    """
    if not excerpts:
        return None, None, []
    listing = "\n".join(f"[{n}] {t}" for n, t in excerpts)
    system = (
        "You track ONE action item across two meetings. You are given an action "
        "item agreed in the PREVIOUS meeting and numbered excerpts from the "
        "FOLLOW-UP meeting's transcript. Decide what the follow-up meeting says "
        "became of THIS action.\n"
        "'status' MUST be exactly one of: completed, in_progress, blocked, "
        "changed, not_discussed.\n"
        "  completed     = an excerpt says it is done or finished.\n"
        "  in_progress   = under way but not yet finished.\n"
        "  blocked       = stuck, waiting on someone or something.\n"
        "  changed       = revised, replaced, postponed or dropped.\n"
        "  not_discussed = none of the excerpts actually refer to this action.\n"
        "Choose one of the first four ONLY if an excerpt genuinely refers to this "
        "action; otherwise not_discussed. In 'cited' list the excerpt number(s) "
        "that support the verdict. Keep 'note' under 20 words.\n"
        'Return ONLY JSON: {"status": "...", "cited": [1], "note": "..."}'
    )
    user = f"ACTION ITEM (previous meeting):\n{action_en}\n\nFOLLOW-UP EXCERPTS:\n{listing}"
    try:
        raw = _chat(system=system, user=user)
    except Exception:  # noqa: BLE001 - one failed item must not sink the pass
        return None, None, []
    if not isinstance(raw, dict):
        return None, None, []
    status = str(raw.get("status", "")).strip().lower().replace(" ", "_")
    if status not in _CLASSIFY_STATUSES:
        return None, None, []  # not_discussed / garbage -> code assigns not_discussed
    valid = {n for n, _ in excerpts}
    cited: list[int] = []
    cited_raw = raw.get("cited")
    if isinstance(cited_raw, list):
        for c in cited_raw:
            try:
                n = int(c)
            except (TypeError, ValueError):
                continue
            if n in valid and n not in cited:
                cited.append(n)
    if not cited:
        return None, None, []  # ungrounded -> not_discussed
    note = raw.get("note")
    return status, (str(note).strip() if note else None), cited


def analyse_carry_forward(meeting_id: int, progress=print) -> list[dict]:
    """Work out what this meeting said about the previous meeting's actions.

    Returns one dict per previous action item (including the ones never
    mentioned), ready to persist. Raises NoPreviousMeeting when the meeting has no
    follow-up link, and ValueError when it has no transcript.
    """
    with get_conn() as conn:
        meeting = conn.execute(
            "SELECT follow_up_of FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if meeting is None:
            raise ValueError(f"Meeting {meeting_id} not found")
        previous_id = meeting["follow_up_of"]
        if not previous_id:
            raise NoPreviousMeeting(f"Meeting {meeting_id} is not linked to a previous meeting")
        seg_rows = conn.execute(
            "SELECT id, text FROM segments WHERE meeting_id = ? ORDER BY start_seconds",
            (meeting_id,),
        ).fetchall()

    if not seg_rows:
        raise ValueError(f"Meeting {meeting_id} has no transcript segments yet")

    actions = _previous_actions(previous_id)
    if not actions:
        progress(f"[carry-forward] meeting {previous_id} has no action items - nothing to track")
        return []

    segments = [dict(r) for r in seg_rows]

    # --- Normalise to English (approach B) so matching is single-language ---
    actions_en = _translate_to_english([a["description"] for a in actions], progress)
    segments_en = _translate_to_english([s["text"] for s in segments], progress)
    progress(
        f"[carry-forward] {len(actions)} action(s) vs {len(segments)} segment(s); "
        "normalised to English, classifying one item at a time"
    )

    results: list[dict] = []
    tally: dict[str, int] = {}
    for idx, action in enumerate(actions):
        # --- Retrieve only the few most relevant segments for THIS item (A) ---
        top = _retrieve_relevant(actions_en[idx], segments_en)
        excerpts = [(pos + 1, segments_en[si]) for pos, si in enumerate(top)]
        localnum_to_segment_id = {pos + 1: segments[si]["id"] for pos, si in enumerate(top)}

        # --- Classify THIS item against just those excerpts ---
        status, note, cited = _classify_action(actions_en[idx], excerpts)
        if status and cited:
            source_segment_id = localnum_to_segment_id.get(cited[0])
        else:
            status, note, source_segment_id = NOT_DISCUSSED, None, None

        tally[status] = tally.get(status, 0) + 1
        results.append(
            {
                "previous_action_id": action["id"],
                "description": action["description"],
                "owner": action.get("owner"),
                "status": status,
                "note": note,
                "source_segment_id": source_segment_id,
            }
        )

    discussed = sum(1 for r in results if r["status"] != NOT_DISCUSSED)
    progress(
        f"[carry-forward] {discussed}/{len(results)} previous action(s) discussed "
        f"({', '.join(f'{k}={v}' for k, v in sorted(tally.items()))})"
    )
    return results


def run_carry_forward(meeting_id: int) -> None:
    """Analyse and persist. Idempotent: replaces any previous run's rows.

    Mirrors run_categorization's contract - it sets its own status column so the
    UI can show progress, and it is safe to re-run at any time.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT follow_up_of FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"Meeting {meeting_id} not found")
    previous_id = row["follow_up_of"]
    if not previous_id:
        raise NoPreviousMeeting(f"Meeting {meeting_id} is not linked to a previous meeting")

    # Llama needs the card to itself, exactly as categorization does.
    from app.gpu import free_all_torch_models

    free_all_torch_models()

    _set_cf_status(meeting_id, "analysing")
    results = analyse_carry_forward(meeting_id)

    with get_conn() as conn:
        conn.execute("DELETE FROM carry_forward WHERE meeting_id = ?", (meeting_id,))
        for r in results:
            conn.execute(
                "INSERT INTO carry_forward (meeting_id, previous_meeting_id, previous_action_id, "
                "description, owner, status, note, source_segment_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    meeting_id,
                    previous_id,
                    r["previous_action_id"],
                    r["description"],
                    r["owner"],
                    r["status"],
                    r["note"],
                    r["source_segment_id"],
                ),
            )
        conn.execute(
            "UPDATE meetings SET carry_forward_status = 'ready', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (meeting_id,),
        )


def carry_forward_safe(meeting_id: int) -> None:
    """run_carry_forward with error capture - never raises.

    Used as the background task behind linking, so a failure leaves a readable
    status on the meeting instead of a silent dead background thread.
    """
    from app.pipeline.runner import _PIPELINE_LOCK

    _PIPELINE_LOCK.acquire()
    try:
        run_carry_forward(meeting_id)
    except Exception as exc:  # noqa: BLE001
        try:
            _set_cf_status(
                meeting_id, f"failed: {type(exc).__name__}: {str(exc)[:200]}"
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        _PIPELINE_LOCK.release()


def _set_cf_status(meeting_id: int, status: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE meetings SET carry_forward_status = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, meeting_id),
        )


def load_carry_forward(meeting_id: int) -> list[dict]:
    """Persisted carry-forward rows for a meeting, in the previous meeting's
    action order."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM carry_forward WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def meeting_chain(meeting_id: int) -> list[int]:
    """The full follow-up chain ending at `meeting_id`, oldest first.

    Walks `follow_up_of` backwards. Guarded against cycles (a corrupt link would
    otherwise spin forever) - the route layer prevents them, this is belt and
    braces for a hand-edited database.
    """
    chain: list[int] = []
    seen: set[int] = set()
    current: int | None = meeting_id
    with get_conn() as conn:
        while current is not None and current not in seen:
            seen.add(current)
            chain.append(current)
            row = conn.execute(
                "SELECT follow_up_of FROM meetings WHERE id = ?", (current,)
            ).fetchone()
            current = row["follow_up_of"] if row else None
    chain.reverse()
    return chain
