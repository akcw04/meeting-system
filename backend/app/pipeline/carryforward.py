"""Cross-meeting carry-forward analysis (Session 22).

A meeting rarely stands alone. When one meeting is the follow-up of an earlier
one, the question a minute-taker actually has to answer is "what happened to
everything we agreed last time?" - and answering it by hand means reading two
sets of minutes side by side. This module answers it automatically.

Given meeting B marked as the follow-up of meeting A, we take A's action items
and read B's transcript for evidence about each one, classifying it as:

    completed     - B says the task is done
    in_progress   - B says it is under way but not finished
    blocked       - B says it is stuck, waiting on something
    changed       - B revised, replaced or dropped the task
    not_discussed - B never mentioned it (assigned in CODE, not by the model)

Design notes, following the patterns the categorizer already earned the hard way:

  * The model is asked to cite [seg N] ids for every verdict, and a verdict
    whose citations are all outside the chunk we sent is discarded. Same
    anti-hallucination grounding as categorize.py.
  * The model refers to previous actions by a small 1-based INDEX, never by a
    database id - an 8B model echoes short ordinals reliably and mangles
    arbitrary primary keys (the bug #17/#21 principle: don't hand the model a
    job a lookup table does perfectly).
  * 'not_discussed' is never something the model returns. It is what the code
    fills in for any action the model produced no grounded verdict about, so a
    silent model failure reads as "no evidence found" rather than inventing
    progress that was never discussed.
  * Long meetings are chunked exactly like categorization. Each chunk sees the
    full previous-actions list, and verdicts are merged across chunks by
    confidence order, so evidence anywhere in the meeting counts.
"""
from __future__ import annotations

from app.db import get_conn
from app.pipeline.categorize import (
    _chat,
    _chunk_segments,
    _coerce_items,
    _language_name,
)
from app.schemas.insights import ExtractedCarryForward

# Verdicts the MODEL may return, ordered weakest -> strongest evidence of
# movement. When two chunks disagree about the same action we keep the later
# one in this order: a chunk that saw the task completed outranks one that only
# saw it being worked on, which is the reading a human minute-taker would take
# from "we started it... and actually we finished it yesterday".
_MODEL_STATUSES = ("changed", "blocked", "in_progress", "completed")
_STATUS_RANK = {s: i for i, s in enumerate(_MODEL_STATUSES)}

# What the code assigns when no grounded verdict came back for an action.
NOT_DISCUSSED = "not_discussed"


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


def _carry_forward_prompt(language: str, actions: list[dict]) -> str:
    listing = "\n".join(
        f"{i}. {a['description']}"
        + (f" (owner: {a['owner']})" if a.get("owner") else "")
        + (f" (was due: {a['due_date']})" if a.get("due_date") else "")
        for i, a in enumerate(actions, start=1)
    )
    return (
        "You are a meeting-minutes analyst tracking progress ACROSS two meetings.\n"
        "Below is the numbered list of action items agreed in the PREVIOUS meeting. "
        "The user will give you transcript lines from the FOLLOW-UP meeting.\n\n"
        "PREVIOUS MEETING'S ACTION ITEMS:\n"
        f"{listing}\n\n"
        "Your job: for each previous action item that the follow-up transcript "
        "ACTUALLY discusses, report what happened to it.\n"
        "Rules:\n"
        "- Report an item ONLY if the transcript lines genuinely refer to it. If an "
        "item is not mentioned in these lines, LEAVE IT OUT entirely. Do not guess, "
        "and never report progress that was not discussed.\n"
        "- 'index' MUST be the number of the item in the list above.\n"
        "- 'status' MUST be exactly one of: completed, in_progress, blocked, changed.\n"
        "    completed   = the transcript says it is done or finished.\n"
        "    in_progress = under way but not yet finished.\n"
        "    blocked     = stuck, waiting on someone or something.\n"
        "    changed     = revised, replaced, postponed or dropped.\n"
        f"- 'note' is a SHORT evidence phrase (max ~20 words) written in {language}.\n"
        "- Cite the supporting [seg N] id(s) in source_segment_ids for every entry.\n\n"
        'Return ONLY a JSON object with exactly this shape. No markdown, no commentary:\n'
        '{\n'
        '  "updates": [{"index": 1, "status": "completed", "note": "...", "source_segment_ids": [12]}]\n'
        '}\n'
        "Use an empty list if these lines discuss none of the previous items."
    )


def _validate_updates(raw: dict, valid_ids: set[int], n_actions: int) -> list[ExtractedCarryForward]:
    """Parse + ground the model's reply: keep only well-formed updates that name
    a real action and cite a segment we actually sent."""
    norm: dict = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str):
                norm["".join(ch for ch in k.lower() if ch.isalnum())] = v
    items = _coerce_items(norm.get("updates"), ExtractedCarryForward)
    kept: list[ExtractedCarryForward] = []
    for it in items:
        if not (1 <= it.index <= n_actions):
            continue  # hallucinated an item number that isn't in the list
        if it.status not in _STATUS_RANK:
            continue  # not one of the four verdicts we asked for
        it.source_segment_ids = [i for i in it.source_segment_ids if i in valid_ids]
        if not it.source_segment_ids:
            continue  # ungrounded - the whole point is that evidence must exist
        kept.append(it)
    return kept


def analyse_carry_forward(meeting_id: int, progress=print) -> list[dict]:
    """Work out what this meeting said about the previous meeting's actions.

    Returns one dict per previous action item (including the ones never
    mentioned), ready to persist. Raises NoPreviousMeeting when the meeting has
    no follow-up link, and ValueError when it has no transcript.
    """
    with get_conn() as conn:
        meeting = conn.execute(
            "SELECT follow_up_of, primary_language, language FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if meeting is None:
            raise ValueError(f"Meeting {meeting_id} not found")
        previous_id = meeting["follow_up_of"]
        if not previous_id:
            raise NoPreviousMeeting(f"Meeting {meeting_id} is not linked to a previous meeting")
        seg_rows = conn.execute(
            "SELECT id, speaker_id, text FROM segments WHERE meeting_id = ? ORDER BY start_seconds",
            (meeting_id,),
        ).fetchall()
        spk_rows = conn.execute(
            "SELECT id, label, display_name FROM speakers WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchall()

    if not seg_rows:
        raise ValueError(f"Meeting {meeting_id} has no transcript segments yet")

    actions = _previous_actions(previous_id)
    if not actions:
        progress(f"[carry-forward] meeting {previous_id} has no action items - nothing to track")
        return []

    segments = [dict(r) for r in seg_rows]
    speaker_labels = {r["id"]: (r["display_name"] or r["label"]) for r in spk_rows}

    primary = (meeting["primary_language"] or "auto").lower()
    out_lang = primary if primary != "auto" else (meeting["language"] or "en")
    language = _language_name(out_lang)

    chunks = _chunk_segments(segments, speaker_labels)
    progress(
        f"[carry-forward] {len(actions)} previous action(s) vs {len(segments)} segment(s) "
        f"in {len(chunks)} chunk(s), notes in {language}"
    )

    # index -> best verdict so far
    best: dict[int, ExtractedCarryForward] = {}
    system = _carry_forward_prompt(language, actions)
    for i, (chunk_text, ids) in enumerate(chunks, start=1):
        try:
            raw = _chat(
                system=system,
                user=(
                    f"Follow-up meeting transcript, part {i} of {len(chunks)}:\n\n{chunk_text}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - one bad chunk must not sink the pass
            progress(f"[carry-forward] chunk {i} SKIPPED: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        for upd in _validate_updates(raw, ids, len(actions)):
            prior = best.get(upd.index)
            if prior is None or _STATUS_RANK[upd.status] >= _STATUS_RANK[prior.status]:
                best[upd.index] = upd

    results: list[dict] = []
    for idx, action in enumerate(actions, start=1):
        upd = best.get(idx)
        results.append(
            {
                "previous_action_id": action["id"],
                "description": action["description"],
                "owner": action.get("owner"),
                "status": upd.status if upd else NOT_DISCUSSED,
                "note": (upd.note or None) if upd else None,
                "source_segment_id": upd.source_segment_ids[0] if upd else None,
            }
        )

    discussed = sum(1 for r in results if r["status"] != NOT_DISCUSSED)
    tally = ", ".join(
        f"{s}={sum(1 for r in results if r['status'] == s)}"
        for s in (*_MODEL_STATUSES, NOT_DISCUSSED)
    )
    progress(
        f"[carry-forward] {discussed}/{len(results)} previous action(s) discussed ({tally})"
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
