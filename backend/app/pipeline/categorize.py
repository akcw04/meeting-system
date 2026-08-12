"""LLM content categorization via Ollama + Llama 3.1 (Phase 7).

Map-reduce over the transcript:

  MAP    Split the diarized transcript into ~4500-token chunks. For each
         chunk, ask Llama to extract action items, decisions, deadlines,
         issues, risks + a 2-4 sentence chunk summary. The reply is requested
         as JSON (Ollama `format=json`, shaped by the prompt) and validated
         against ChunkInsights - full-schema-constrained decoding was ~10x
         slower on this 6 GB setup (see docs/BUGS.md #25).

  REDUCE If there was more than one chunk, send all collected items +
         chunk summaries through one consolidation call that dedupes
         overlapping items and writes the final <=300-word summary
         (MeetingInsights schema).

Anti-hallucination grounding:
  Every transcript line is sent as "[seg <id>] <Speaker>: <text>". The
  model must cite source_segment_ids per item. After parsing we drop any
  cited id that doesn't exist in the chunk we actually sent - an item
  with zero valid citations is discarded entirely.

VRAM:
  Ollama runs Llama in its own process (~5 GB). Call
  gpu.free_all_torch_models() before invoking us (the runner does this).
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import httpx

from app.config import settings
from app.schemas.insights import (
    ChunkInsights,
    ExtractedActionItem,
    ExtractedDeadline,
    ExtractedDecision,
    ExtractedIssue,
    ExtractedRisk,
    FinalSummary,
    MeetingInsights,
)

# Llama 3.1 8B context handling: Ollama defaults to a small num_ctx, so we
# set it explicitly. 8192 tokens keeps KV-cache VRAM modest on a 6 GB card.
NUM_CTX = 8192
# Leave room for the system prompt, schema overhead, and the response.
CHUNK_TOKEN_BUDGET = 4500
# Rough chars-per-token heuristic for budgeting (English ~4, CJK denser).
CHARS_PER_TOKEN = 4

LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Chinese (Simplified)",
    "ms": "Malay (Bahasa Melayu)",
}


class OllamaUnavailable(RuntimeError):
    """Ollama server is not reachable or the model is missing."""


def _language_name(code: str | None) -> str:
    return LANGUAGE_NAMES.get((code or "en").lower(), "English")


def _chat(system: str, user: str, timeout: float | None = None, retries: int = 2) -> dict:
    """One JSON chat call to Ollama. Returns parsed JSON.

    Uses Ollama's lightweight `format="json"` (valid-JSON grammar) rather than a
    full Pydantic-schema grammar. Measured on this 6 GB setup, full-schema
    constrained decoding ran ~10x slower (<0.4 tok/s vs ~4 tok/s) and pushed
    Chinese chunks past the timeout (bug #25). The schema's only real benefit -
    a guaranteed field shape - is recovered by stating the exact JSON shape in
    the prompt and validating the reply with Pydantic afterwards.

    Retries on unparseable output - truncation / repetition is often transient
    on the 8B model, especially for Chinese (bugs #22/#23). CRUCIAL (bug #32):
    the first pass runs at temperature 0 (greedy: deterministic + most complete
    extraction), but a greedy call is DETERMINISTIC - re-sending the identical
    request produces byte-identical output, so a plain retry cannot recover a
    parse failure. Each retry therefore nudges the temperature up so the model
    samples a genuinely DIFFERENT continuation that has a real chance to parse.
    """
    url = f"{settings.ollama_host}/api/chat"

    def _payload(temperature: float) -> dict:
        return {
            "model": settings.ollama_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": "json",      # lightweight valid-JSON grammar (NOT full-schema; see bug #25)
            "stream": False,
            "options": {
                "num_ctx": NUM_CTX,
                "temperature": temperature,
                "num_predict": settings.ollama_num_predict,  # cap output (anti-runaway)
                "repeat_penalty": settings.ollama_repeat_penalty,  # break repetition loops
            },
        }

    # attempt 0 = greedy (temp 0, best extraction); each retry nudges temperature
    # up so a failed parse is re-tried with DIFFERENT (sampled) output, not the
    # same deterministic reply again (bug #32).
    temps = [0.0] + [round(0.3 + 0.2 * k, 2) for k in range(retries)]
    last_content = ""
    for temperature in temps:
        payload = _payload(temperature)
        try:
            resp = httpx.post(url, json=payload, timeout=timeout or settings.ollama_timeout)
        except httpx.ConnectError as exc:
            raise OllamaUnavailable(
                f"Cannot reach Ollama at {settings.ollama_host}. Is it running? "
                f"(It usually auto-starts; otherwise run 'ollama serve'.)"
            ) from exc
        if resp.status_code == 404:
            raise OllamaUnavailable(
                f"Ollama model '{settings.ollama_model}' not found. "
                f"Pull it with: ollama pull {settings.ollama_model}"
            )
        resp.raise_for_status()
        last_content = resp.json()["message"]["content"]
        # Extract the outermost {...} ONLY when both braces exist - a truncated
        # reply has no closing brace, so keep it whole (don't slice to empty).
        i, j = last_content.find("{"), last_content.rfind("}")
        snippet = last_content[i:j + 1] if (i != -1 and j > i) else last_content
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            continue  # unparseable - retry with a higher temperature (different output)
    raise ValueError(
        f"Ollama returned unparseable JSON after {retries + 1} attempts "
        f"(truncated or repetitive output). Raw content[:200]: {last_content[:200]!r}"
    )


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF       # CJK Unified Ideographs
        or 0x3400 <= o <= 0x4DBF    # Extension A
        or 0xF900 <= o <= 0xFAFF    # Compatibility Ideographs
        or 0x3000 <= o <= 0x303F    # CJK punctuation
    )


def _estimate_tokens(text: str) -> float:
    """Estimate token count. CJK characters are ~1 token EACH; Latin/other run
    ~CHARS_PER_TOKEN chars/token. A flat chars/4 budget under-counted Chinese by
    3-4x, so chunks overflowed num_ctx and the LLM returned nothing (bug #22).
    Counting CJK per-character also keeps EN-ZH code-switched chunks sized right.
    """
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk + (len(text) - cjk) / CHARS_PER_TOKEN


def _chunk_segments(
    segments: list[dict],
    speaker_labels: dict[int | None, str],
) -> list[tuple[str, set[int]]]:
    """Render segments into prompt-ready text chunks, each ~CHUNK_TOKEN_BUDGET
    tokens (CJK-aware, so Chinese / code-switch chunks fit the context window).

    Returns [(chunk_text, set_of_segment_ids_in_chunk), ...].
    """
    chunks: list[tuple[str, set[int]]] = []
    lines: list[str] = []
    ids: set[int] = set()
    used = 0.0

    for seg in segments:
        who = speaker_labels.get(seg.get("speaker_id"), "Unknown")
        line = f"[seg {seg['id']}] {who}: {seg['text'].strip()}"
        line_tokens = _estimate_tokens(line)
        if used + line_tokens > CHUNK_TOKEN_BUDGET and lines:
            chunks.append(("\n".join(lines), ids))
            lines, ids, used = [], set(), 0.0
        lines.append(line)
        ids.add(seg["id"])
        used += line_tokens

    if lines:
        chunks.append(("\n".join(lines), ids))
    return chunks


def _extraction_system_prompt(language: str) -> str:
    return (
        "You are a meticulous meeting-minutes analyst.\n"
        "Extract structured insights from the meeting transcript chunk the user provides.\n"
        "Rules:\n"
        f"- Write ALL output text in {language}, regardless of what languages appear in the transcript.\n"
        "- Phrase every item as a concise, action-oriented bullet (max ~15 words).\n"
        "- Extract ONLY what the transcript explicitly supports. Never invent or embellish.\n"
        "- For every item, cite the supporting segment id(s) in source_segment_ids, "
        "using the [seg N] numbers from the transcript.\n"
        "- If a category has genuinely nothing, return an empty list for it. But read "
        "carefully first: most meetings DO contain at least one decision, deadline, and "
        "risk - do not leave a category empty just because it takes effort to find.\n"
        "\nHow to classify each item:\n"
        "- ACTION ITEM: a task someone must do. Set 'owner' to the responsible speaker "
        "label (e.g. 'Speaker 3') when identifiable, else null. When a time is mentioned "
        "('today', 'by Friday', 'on Thursday', 'next week'), copy that timeframe into the "
        "'due' field AS WELL AS describing the task in 'description'. Keep 'due' to a SHORT "
        "literal timeframe from the transcript (e.g. 'Wednesday', 'by Friday', '25 June'); "
        "do not explain or rephrase it; use null when no time is given.\n"
        "- DECISION: something the group agreed or confirmed. Capture EVERY explicit "
        "decision - especially around phrases like 'the decision is', 'we agreed', "
        "'confirm', \"let's do that\", or 'I suggest X' that is then accepted "
        "(e.g. a decision to postpone a launch by one week).\n"
        "- DEADLINE: a date or timeframe something is due by, plus what it is for. "
        "Anything committed to a time ('today', 'Thursday', 'by Friday') belongs here.\n"
        "- ISSUE: a problem that EXISTS NOW - already happening or already true "
        "(e.g. 'only 6 of 12 staff attended training', 'engineering has only tested "
        "5,000 users so far', 'the payment gateway keeps timing out'). A current technical "
        "failure or error is an issue even when someone is also assigned to fix it (that "
        "fix is also an action item).\n"
        "- RISK: a POTENTIAL FUTURE problem, usually conditional - signalled by 'if', "
        "'may', 'might', 'could', or an unresolved dependency (e.g. 'if the vendor delays, "
        "users may face payment issues'; 'the vendor has not given final confirmation "
        "yet'; 'expected traffic is ~8,000 but only 5,000 were tested'). When a problem "
        "is framed as a future or possible consequence, it is a RISK, not an issue.\n"
        "- The same underlying fact may legitimately appear in more than one category.\n"
        "- chunk_summary MUST be 2-4 complete sentences (40+ words) describing the "
        "discussion's topics and outcomes - never a title or fragment.\n\n"
        "Return ONLY a JSON object with EXACTLY these keys. Keep the keys in English "
        "exactly as shown; write all string VALUES in "
        f"{language}. No markdown, no commentary:\n"
        '{\n'
        '  "action_items": [{"description": "...", "owner": "Speaker N or null", "due": "timeframe or null", "source_segment_ids": [12]}],\n'
        '  "decisions": [{"description": "...", "source_segment_ids": [12]}],\n'
        '  "deadlines": [{"description": "...", "date": "date or null", "source_segment_ids": [12]}],\n'
        '  "issues": [{"description": "...", "source_segment_ids": [12]}],\n'
        '  "risks": [{"description": "...", "mitigation": "... or null", "source_segment_ids": [12]}],\n'
        '  "chunk_summary": "WRITE THIS LAST: 2-4 sentences (40+ words) summarising the discussion above. NEVER leave empty."\n'
        '}\n'
        "Use [] for any category with nothing found. Every list item MUST include "
        "source_segment_ids citing the [seg N] numbers from the transcript. "
        "chunk_summary must always be a non-empty 2-4 sentence paragraph."
    )


def _summary_system_prompt(language: str) -> str:
    return (
        "You write executive meeting summaries.\n"
        "The user gives you section summaries from sequential chunks of ONE meeting, "
        "plus the key extracted items.\n"
        f"- Write the summary in {language}.\n"
        "- 300 words or less; complete sentences; one coherent narrative covering the "
        "meeting's purpose, main topics, decisions, and outcomes.\n"
        "- Base it ONLY on the provided material - do not invent anything.\n"
        'Return ONLY a JSON object: {"summary": "..."} with the summary text in '
        f"{language}. No markdown, no other keys."
    )


def _validate_sources(items: list, valid_ids: set[int]) -> list:
    """Drop citations to segments we never sent; drop items with zero valid citations."""
    kept = []
    for item in items:
        item.source_segment_ids = [i for i in item.source_segment_ids if i in valid_ids]
        if item.source_segment_ids:
            kept.append(item)
    return kept


def _coerce_items(raw_list, model):
    """Validate each item independently, dropping ONLY the malformed ones.

    free-JSON mode (bug #25) isn't grammar-guaranteed, so the 8B occasionally
    emits one off-shape item (a bare int for source_segment_ids, a capitalised
    field name, a missing field). A single bad item must not sink the whole
    chunk, so we lower-case the item's keys, normalise the citation, then
    validate item-by-item and keep the good ones.
    """
    out = []
    if not isinstance(raw_list, list):
        return out
    for it in raw_list:
        if isinstance(it, dict):
            it = {(k.lower() if isinstance(k, str) else k): v for k, v in it.items()}
            ss = it.get("source_segment_ids")
            if isinstance(ss, (int, str)):  # model cited a single id, not a list
                it["source_segment_ids"] = [ss]
        try:
            obj = model.model_validate(it)
        except Exception:  # noqa: BLE001
            continue
        # The 8B occasionally emits a blank-description row - drop it. Guarded on
        # the model actually HAVING a description, so this helper stays reusable
        # for shapes that carry no description at all (e.g. carry-forward
        # verdicts, which reference a previous item by index).
        if "description" in model.model_fields and not (obj.description or "").strip():
            continue
        out.append(obj)
    return out


def _parse_chunk(raw: dict) -> ChunkInsights:
    """Tolerantly build ChunkInsights from a free-JSON reply: salvage every
    well-formed item and default a missing/short summary to '' (bug #25), so a
    structurally-imperfect-but-valid-JSON reply still yields usable insights
    instead of being skipped wholesale.

    Top-level keys are matched case/punctuation-insensitively because the 8B
    sometimes capitalises them (e.g. 'Issues' instead of 'issues'), which would
    otherwise silently drop a whole category.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
    norm = {}
    for k, v in raw.items():
        if isinstance(k, str):
            norm["".join(ch for ch in k.lower() if ch.isalnum())] = v
    return ChunkInsights(
        chunk_summary=str(norm.get("chunksummary") or "").strip(),
        action_items=_coerce_items(norm.get("actionitems"), ExtractedActionItem),
        decisions=_coerce_items(norm.get("decisions"), ExtractedDecision),
        deadlines=_coerce_items(norm.get("deadlines"), ExtractedDeadline),
        issues=_coerce_items(norm.get("issues"), ExtractedIssue),
        risks=_coerce_items(norm.get("risks"), ExtractedRisk),
    )


def _ensure_summary(summary: str, chunk_results: list, action_items: list, decisions: list) -> str:
    """Guarantee a non-empty, in-language meeting summary.

    The 8B model sometimes returns an empty chunk_summary (especially in
    Chinese) even when it extracts items fine - bug #25. Rather than show the
    user a blank summary (which reads as a failure), fall back through:
    chunk summaries -> key decision/action descriptions (already in the
    meeting's language) -> a plain note.
    """
    if summary and summary.strip():
        return summary.strip()
    joined = " ".join(c.chunk_summary.strip() for c in chunk_results if c.chunk_summary.strip())
    if joined:
        return joined[:1500]
    points = ([d.description for d in decisions if d.description][:5]
              + [a.description for a in action_items if a.description][:5])
    if points:
        return " / ".join(points)[:1500]
    return ("No narrative summary could be generated for this meeting; "
            "please refer to the extracted items and the full transcript.")


# Tokens the 8B uses to mean "no due date"; a 'due' whose first word is one of these
# (e.g. 'null', 'none', even 'null (immediate)') is treated as empty.
_DUE_NULLISH = {"null", "none", "n/a", "na", "tbd", "unknown", "无", "没有", "未定",
                "tiada", "tidak", "belum"}

# Date / time expressions that signal a deadline, scanned in an action item's
# wording. The 8B fills the structured 'due' field erratically but reliably keeps
# the date in the sentence ('...by Friday'), so we read the sentence too. EN + ZH
# + MS (Bahasa Melayu, added 2026-08-07). Recall-biased: a stray deadline is
# harmless and editable; a MISSED one was the graded failure (mtg 28). Only
# ACTION ITEMS are scanned - not decisions (so 'postpone by one week' is not
# mistaken for a deadline).
# NOTE: every group here MUST stay non-capturing - _extract_date_cues() uses
# re.findall, which returns group contents instead of whole matches if any
# capturing group exists.
_WD = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_MON = (r"january|february|march|april|may|june|july|august|september|october|"
        r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
# Malay weekdays and months. 'Mac' (March) and 'Mei' (May) are short and could
# collide with ordinary words, so they are only ever matched next to a number.
_MS_WD = r"isnin|selasa|rabu|khamis|jumaat|sabtu|ahad"
_MS_MON = (r"januari|februari|mac|april|mei|jun|julai|ogos|september|oktober|"
           r"november|disember")
_MS_NUM = r"satu|dua|tiga|empat|lima|enam|tujuh|lapan|sembilan|sepuluh|\d+"
_DATE_RE = re.compile("|".join([
    rf"\b(?:by|before|on|due)\s+(?:next\s+|this\s+)?(?:{_WD})\b",
    rf"\b(?:this|next)\s+(?:{_WD})\b",
    rf"\b(?:{_WD})\b",
    r"\b(?:today|tonight|tomorrow|the\s+day\s+after\s+tomorrow)\b",
    r"\b(?:by\s+(?:the\s+)?end\s+of(?:\s+the)?|end\s+of(?:\s+the)?|this|next)\s+(?:week|month|quarter)\b",
    r"\b(?:eow|eod)\b",
    r"\b(?:by|before|in|end\s+of)\s+q[1-4]\b",
    r"\b(?:within|in)\s+(?:a|an|one|two|three|\d+)\s+(?:day|days|week|weeks|month|months)\b",
    r"\b(?:one|two|three|\d+)\s+(?:day|days|week|weeks|month|months)\b",
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MON})\b",
    rf"\b(?:{_MON})\s+\d{{1,2}}(?:st|nd|rd|th)?\b",
    r"\b\d{4}-\d{1,2}-\d{1,2}\b",
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    r"今天|今晚|明天|明晚|后天|大后天",
    r"这个?周|本周|下个?周|这个?星期|下个?星期|周末|这个?月|下个?月|月底|月初",
    r"(?:星期|周|礼拜)[一二三四五六日天]",
    r"\d{1,2}\s*月\s*\d{1,2}\s*[号日]",
    r"\d{1,2}\s*[号日]",
    r"(?:一|两|二|三|四|五|\d+)\s*(?:天|周|个?星期|个月)",
    r"(?:第[一二三四1234]|下个?|这个?)?季度",
    # --- Bahasa Melayu ---
    rf"\b(?:sebelum|menjelang|pada|hari)\s+(?:{_MS_WD})\b",
    rf"\b(?:{_MS_WD})\b",
    r"\b(?:hari\s+ini|malam\s+ini|esok|besok|lusa)\b",
    r"\b(?:minggu|bulan|suku\s+tahun)\s+(?:ini|depan|hadapan)\b",
    r"\bhujung\s+(?:minggu|bulan|tahun)\b",
    r"\bakhir\s+(?:minggu|bulan|tahun)\s+(?:ini|depan|hadapan)\b",
    rf"\b(?:dalam|sebelum)\s+(?:{_MS_NUM})\s+(?:hari|minggu|bulan)\b",
    rf"\b(?:{_MS_NUM})\s+(?:hari|minggu|bulan)\s+(?:lagi|depan|hadapan)\b",
    rf"\b\d{{1,2}}\s+(?:{_MS_MON})\b",
    rf"\b(?:{_MS_MON})\s+\d{{1,2}}\b",
]), re.IGNORECASE)
# Leading prepositions/articles stripped before comparing two date cues for dedup
# (so 'by Friday' and 'Friday' count as the same date). The Malay 'hari' is only
# stripped before a weekday, so 'hari ini' (today) is never reduced to 'ini'.
_DATE_LEAD = re.compile(
    r"^(?:by|before|on|due|this|next|the|in|within"
    rf"|sebelum|menjelang|pada|dalam|hari(?=\s+(?:{_MS_WD})))\s+",
    re.IGNORECASE,
)
# Leading words stripped from a cue for DISPLAY ('on Thursday' -> 'Thursday', 'by
# Friday' -> 'Friday'); 'this/next/end of' are kept because they carry meaning.
_DISPLAY_LEAD = re.compile(
    r"^(?:by|on|before|due|sebelum|menjelang|pada)\s+", re.IGNORECASE
)


def _norm_date(s: str | None) -> str:
    s = (s or "").lower().strip()
    prev = None
    while s != prev:
        prev = s
        s = _DATE_LEAD.sub("", s)
    return s.strip(" .,:;'\"-—")


def _extract_date_cues(text: str) -> list[str]:
    """Every date/time expression in `text`, in order - candidate deadline dates."""
    return [m.strip() for m in _DATE_RE.findall(text or "")]


# === Turning a spoken date cue into an actual calendar date ================
#
# A deadline that reads "by Friday" is only meaningful if the reader knows which
# Friday. Each cue is therefore resolved against the meeting's own date and the
# result appended in parentheses - "by Friday (15/08/2026)" - rather than
# replacing the words that were actually spoken, so the user can still see what
# the transcript said and correct the interpretation if it is wrong.
#
# The anchor is the date the recording was uploaded. That is right when a
# meeting is uploaded the day it happens, and wrong by exactly the delay when it
# is not, which is why the resolved date is offered as an editable suggestion
# rather than presented as fact.

_WEEKDAY_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
    "isnin": 0, "selasa": 1, "rabu": 2, "khamis": 3, "jumaat": 4,
    "sabtu": 5, "ahad": 6,
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}

_MONTH_INDEX = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    "januari": 1, "februari": 2, "mac": 3, "mei": 5, "julai": 7, "ogos": 8,
    "oktober": 10, "disember": 12,
}

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6,
    "tujuh": 7, "lapan": 8, "sembilan": 9, "sepuluh": 10,
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
}


def _end_of_month(d: date) -> date:
    first_next = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return first_next - timedelta(days=1)


def _next_weekday(anchor: date, target: int) -> date:
    """The soonest `target` weekday on or after `anchor`.

    'by Friday' said on a Friday means that day, not a week later, so the
    anchor's own weekday counts as a match.
    """
    return anchor + timedelta(days=(target - anchor.weekday()) % 7)


def resolve_date_cue(cue: str, anchor: date) -> date | None:
    """Best-effort calendar date for a spoken cue. None when it cannot be read.

    Vague horizons ('this quarter', 'soon') deliberately return None - inventing
    a precise date for them would be a guess dressed as data.
    """
    if not cue or anchor is None:
        return None
    c = " ".join(cue.lower().split())

    # --- explicit calendar dates ------------------------------------------
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", c)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", c)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else anchor.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]", c)
    if m:
        try:
            return date(anchor.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    names = "|".join(sorted(_MONTH_INDEX, key=len, reverse=True))
    m = (re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({names})\b", c)
         or re.search(rf"\b({names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", c))
    if m:
        a, b = m.group(1), m.group(2)
        day, mon = (int(a), _MONTH_INDEX[b]) if a.isdigit() else (int(b), _MONTH_INDEX[a])
        try:
            resolved = date(anchor.year, mon, day)
        except ValueError:
            return None
        # A month already past almost certainly means next year.
        if resolved < anchor - timedelta(days=180):
            try:
                resolved = date(anchor.year + 1, mon, day)
            except ValueError:
                return None
        return resolved

    # --- relative horizons -------------------------------------------------
    if re.search(r"\b(today|tonight)\b|hari ini|malam ini|今天|今晚", c):
        return anchor
    if re.search(r"\btomorrow\b|\besok\b|\bbesok\b|明天|明晚", c):
        return anchor + timedelta(days=1)
    if re.search(r"day after tomorrow|\blusa\b|后天", c):
        return anchor + timedelta(days=2)
    if re.search(r"next week|minggu depan|minggu hadapan|下个?周|下个?星期", c):
        return anchor + timedelta(days=7)
    if re.search(r"end of (?:the )?month|hujung bulan|月底", c):
        return _end_of_month(anchor)
    if re.search(r"next month|bulan depan|bulan hadapan|下个?月", c):
        return _end_of_month(_end_of_month(anchor) + timedelta(days=1))
    if re.search(r"end of (?:the )?week|hujung minggu|周末|this week|minggu ini|本周", c):
        return _next_weekday(anchor, 6)          # the coming Sunday
    if re.search(r"this month|bulan ini|这个?月", c):
        return _end_of_month(anchor)

    m = re.search(r"\b(?:within|in|dalam)\s+([a-z一-鿿]+|\d+)\s+"
                  r"(day|days|week|weeks|month|months|hari|minggu|bulan|天|周|个月)\b", c)
    if not m:
        m = re.search(r"\b([a-z一-鿿]+|\d+)\s+"
                      r"(day|days|week|weeks|month|months|hari|minggu|bulan|天|周|个月)\b", c)
    if m:
        raw, unit = m.group(1), m.group(2)
        n = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
        if n:
            if unit.startswith(("day", "hari")) or unit == "天":
                return anchor + timedelta(days=n)
            if unit.startswith(("week", "minggu")) or unit == "周":
                return anchor + timedelta(weeks=n)
            d = anchor
            for _ in range(n):
                d = _end_of_month(d) + timedelta(days=1)
            return d - timedelta(days=1)

    # --- weekday names ------------------------------------------------------
    m = re.search(r"(?:星期|周|礼拜)\s*([一二三四五六日天])", c)
    if m:
        return _next_weekday(anchor, _WEEKDAY_INDEX[m.group(1)])
    latin = "|".join(k for k in _WEEKDAY_INDEX if k.isalpha() and k.isascii())
    m = re.search(rf"\b({latin})\b", c)
    if m:
        target = _WEEKDAY_INDEX[m.group(1)]
        base = anchor + timedelta(days=7) if "next" in c else anchor
        return _next_weekday(base, target)
    return None


def annotate_deadline_dates(deadlines: list, anchor: date | None) -> list:
    """Append a resolved calendar date to each deadline's date text.

    'by Friday' becomes 'by Friday (15/08/2026)'. A cue that cannot be resolved,
    or one already carrying a date, is left exactly as it is. Several cues joined
    with commas are resolved individually.
    """
    if anchor is None:
        return deadlines
    for item in deadlines:
        raw = (getattr(item, "date", None) or "").strip()
        if not raw or "(" in raw:
            continue
        parts = []
        for cue in [p.strip() for p in raw.split(",") if p.strip()]:
            resolved = resolve_date_cue(cue, anchor)
            parts.append(f"{cue} ({resolved:%d/%m/%Y})" if resolved else cue)
        item.date = ", ".join(parts)
    return deadlines


def _augment_deadlines(deadlines: list, action_items: list) -> list:
    """Build the Deadlines section deterministically from dated action items.

    The 8B routinely leaves Deadlines empty even when an action clearly carries a
    date ('...by Friday') - it treats the two as redundant (mtg 28: 4 dated
    actions, 0 deadlines). So we derive deadlines in code from each action's 'due'
    field AND a scan of its wording (the bug #21 principle: don't ask a small LLM
    to do what a code loop can do reliably). Each action yields at most ONE
    deadline row with its dates joined, so a merged 'do X Thursday and Y by
    Friday' action reads as a single entry dated 'Friday, Thursday' rather than two
    identical rows. Model-emitted deadlines are kept and dates they already cover
    are not repeated.
    """
    merged = list(deadlines)

    def covered(desc: str, nd: str) -> bool:
        return any(_similar(d.description, desc) and _norm_date(d.date) == nd for d in merged)

    for ai in action_items:
        due = (getattr(ai, "due", None) or "").strip()
        due_norm = due.lower().strip(" .()-—\"'")
        cues: list[str] = []
        if due_norm and due_norm.split()[0] not in _DUE_NULLISH:
            cues.append(due)                       # a clean structured 'due' value
        cues += _extract_date_cues(ai.description)  # plus any date in the wording
        picked: list[str] = []
        seen: set[str] = set()
        for cue in cues:
            nd = _norm_date(cue)
            if not nd or nd in seen:
                continue
            seen.add(nd)
            if not covered(ai.description, nd):
                picked.append(_DISPLAY_LEAD.sub("", cue).strip())
        if picked:
            merged.append(
                ExtractedDeadline(
                    description=ai.description,
                    date=", ".join(picked),
                    source_segment_ids=list(ai.source_segment_ids),
                )
            )
    return merged


# First-person self-assignment cues: when the speaker of a cited line says one of
# these, that speaker is reliably the action's owner (EN + ZH + MS).
_SELF_ASSIGN = re.compile(
    r"\b(?:i'?ll|i will|i can|i'?ve|i'?m going to|i shall|i'?d|let me)\b"
    r"|我来|我会|我负责|我可以|我去|由我|我把|我处理"
    r"|\b(?:saya\s+(?:akan|boleh|nak|akan\s+buat|uruskan|urus|handle|ambil|jaga|"
    r"sediakan|hantar)|biar\s+saya|aku\s+(?:akan|boleh))\b",
    re.IGNORECASE,
)


def _attribute_owners(action_items, seg_by_id, speaker_labels) -> None:
    """Fill a MISSING action-item owner from the cited line's speaker, but only
    when that line is a first-person self-assignment ('I'll handle X') - then the
    speaker IS the owner. Delegations ('Sarah, can you...') are left to the LLM,
    so we never assign a WRONG owner (a wrong owner is worse than a blank one).
    """
    for ai in action_items:
        if ai.owner:
            continue  # the model already named someone
        for sid in ai.source_segment_ids:
            seg = seg_by_id.get(sid)
            if seg and _SELF_ASSIGN.search(seg.get("text", "") or ""):
                label = speaker_labels.get(seg.get("speaker_id"))
                if label:
                    ai.owner = label
                    break


# Lines likely to contain a risk or issue - used to gather candidate segments for
# the focused recovery pass (the 8B under-extracts these categories).
# EN + ZH + MS (Bahasa Melayu, added 2026-08-07).
_RISK_CUE = re.compile(
    r"\b(?:if|unless|might|may|could|risk|risky|concern|concerned|worried|worry|"
    r"fail|fails|failing|delay|delayed|behind|blocker|blocked|issue|problem|"
    r"unable|can'?t|cannot|shortage|exceed|overrun|bottleneck|not enough)\b"
    r"|风险|如果|可能|担心|忧虑|问题|延迟|延误|失败|不够|赶不上|超出|瓶颈|阻碍|来不及"
    r"|\b(?:risiko|berisiko|jika|kalau|sekiranya|andai|mungkin|bimbang|risau|"
    r"khuatir|masalah|bermasalah|isu|lewat|kelewatan|tertunda|gagal|kegagalan|"
    r"halangan|terhalang|tersekat|kekurangan|melebihi|tidak\s+cukup|tak\s+cukup|"
    r"tidak\s+dapat|tak\s+dapat|tak\s+sempat|belum\s+selesai)\b",
    re.IGNORECASE,
)


def _risk_issue_prompt(language: str) -> str:
    return (
        "You are a meeting-minutes analyst. From the transcript lines below, extract "
        "ONLY two things:\n"
        "- RISKS: potential FUTURE problems, usually conditional - 'if X then Y', "
        "'may', 'might', 'could', or an unresolved dependency.\n"
        "- ISSUES: problems that EXIST NOW - already happening or already true.\n"
        f"Write all output in {language}. Phrase each as a concise bullet (max ~15 words). "
        "Extract ONLY what the lines explicitly support - never invent. Cite the [seg N] "
        "id(s) for every item.\n"
        'Return ONLY JSON with exactly these keys: '
        '{"risks": [{"description": "...", "source_segment_ids": [12]}], '
        '"issues": [{"description": "...", "source_segment_ids": [12]}]}. '
        "Use [] for a category with genuinely none. No markdown, no commentary."
    )


def _recover_risks_issues(segments, seg_by_id, speaker_labels, language,
                          existing_risks, existing_issues, progress=print):
    """Focused second pass to lift Risks/Issues recall (the 8B routinely under-
    extracts them - mtg 28 returned 0 risks). Deterministically gather only the
    lines carrying risk/issue cues that AREN'T already cited, then ask the LLM
    specifically for risks+issues over just those lines (grounded). One small,
    bounded call - fired only when a category came back empty. Returns
    (new_risks, new_issues), deduped against what we already have."""
    already = {sid for it in (*existing_risks, *existing_issues) for sid in it.source_segment_ids}
    candidates = [s for s in segments
                  if s["id"] not in already and (s.get("text", "") or "").strip()]
    if not candidates:
        return [], []

    def _pack(segs):
        lines: list[str] = []
        ids: set[int] = set()
        used = 0.0
        for s in segs:
            who = speaker_labels.get(s.get("speaker_id"), "Unknown")
            line = f"[seg {s['id']}] {who}: {s['text'].strip()}"
            t = _estimate_tokens(line)
            if used + t > CHUNK_TOKEN_BUDGET and lines:
                break
            lines.append(line)
            ids.add(s["id"])
            used += t
        return lines, ids

    # If the un-cited transcript fits one call, feed it ALL (best recall - catches
    # cue-less risks too); otherwise focus on cue-bearing lines to keep it to one
    # bounded call on long meetings.
    lines, ids = _pack(candidates)
    if len(ids) < len(candidates):
        cues = [s for s in candidates if _RISK_CUE.search(s.get("text", "") or "")]
        if not cues:
            return [], []
        lines, ids = _pack(cues)
    progress(f"[categorize] risk/issue recovery over {len(ids)} line(s)...")
    try:
        raw = _chat(system=_risk_issue_prompt(language),
                    user="Transcript lines:\n\n" + "\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        progress(f"[categorize] recovery pass skipped: {type(exc).__name__}")
        return [], []
    norm: dict = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str):
                norm["".join(ch for ch in k.lower() if ch.isalnum())] = v
    new_risks = _validate_sources(_coerce_items(norm.get("risks"), ExtractedRisk), ids)
    new_issues = _validate_sources(_coerce_items(norm.get("issues"), ExtractedIssue), ids)

    def _dedupe(new, existing):
        return [it for it in new if not any(_similar(e.description, it.description) for e in existing)]

    return _dedupe(new_risks, existing_risks), _dedupe(new_issues, existing_issues)


def categorize_transcript(
    segments: list[dict],
    speaker_labels: dict[int | None, str],
    output_language: str | None,
    progress=print,
    on_progress=None,
    meeting_date: date | None = None,
) -> MeetingInsights:
    """Run the full map-reduce extraction over a diarized transcript.

    Args:
        segments: [{id, speaker_id, text, ...}] ordered by time.
        speaker_labels: {speaker_id: "Speaker 1", ...}
        output_language: 'en' / 'zh' / None (None -> English)
        progress: callable for status lines (default print)
    """
    language = _language_name(output_language)
    seg_by_id = {s["id"]: s for s in segments}  # for owner attribution from cited lines
    chunks = _chunk_segments(segments, speaker_labels)
    progress(f"[categorize] {len(segments)} segments -> {len(chunks)} chunk(s), output in {language}")

    chunk_results: list[ChunkInsights] = []
    all_ids: set[int] = set()
    for i, (chunk_text, ids) in enumerate(chunks, start=1):
        progress(f"[categorize] extracting chunk {i}/{len(chunks)} ({len(ids)} segments)...")
        try:
            raw = _chat(
                system=_extraction_system_prompt(language),
                user=f"Meeting transcript chunk {i} of {len(chunks)}:\n\n{chunk_text}",
            )
            parsed = _parse_chunk(raw)
        except OllamaUnavailable:
            raise  # Ollama is down - fail loudly rather than silently skip everything
        except Exception as exc:  # noqa: BLE001
            # One bad chunk (e.g. the 8B model looping/truncating on Chinese) must
            # not sink the whole meeting - log and skip it (bug #23).
            progress(f"[categorize] chunk {i} SKIPPED: {type(exc).__name__}: {str(exc)[:120]}")
            if on_progress is not None:
                on_progress(i / len(chunks))
            continue
        # Ground every category against the ids we actually sent.
        parsed.action_items = _validate_sources(parsed.action_items, ids)
        parsed.decisions = _validate_sources(parsed.decisions, ids)
        parsed.deadlines = _validate_sources(parsed.deadlines, ids)
        parsed.issues = _validate_sources(parsed.issues, ids)
        parsed.risks = _validate_sources(parsed.risks, ids)
        chunk_results.append(parsed)
        all_ids |= ids
        if on_progress is not None:
            on_progress(i / len(chunks))

    if not chunk_results:
        # Every chunk failed to parse (e.g. the 8B model could not categorize this
        # language/content). Don't crash - return an honest placeholder so the
        # meeting still reaches 'ready' and the transcript stays usable (bug #23).
        return MeetingInsights(
            summary=(
                "Automatic insight extraction did not produce usable structured output for "
                "this meeting. The local language model could not reliably categorize this "
                "content (this can happen with non-English audio on the 8B model). The full "
                "transcript is available for manual review."
            ),
        )

    if len(chunk_results) == 1:
        only = chunk_results[0]
        progress("[categorize] single chunk - skipping consolidation call")
        _attribute_owners(only.action_items, seg_by_id, speaker_labels)
        risks, issues = only.risks, only.issues
        if not risks or not issues:  # focused recovery when a category came back empty
            nr, ni = _recover_risks_issues(segments, seg_by_id, speaker_labels,
                                           language, risks, issues, progress)
            risks, issues = risks + nr, issues + ni
        return MeetingInsights(
            summary=_ensure_summary(only.chunk_summary, [only], only.action_items, only.decisions),
            action_items=only.action_items,
            decisions=only.decisions,
            deadlines=annotate_deadline_dates(
                _augment_deadlines(only.deadlines, only.action_items), meeting_date
            ),
            issues=issues,
            risks=risks,
        )

    # REDUCE - two parts:
    #  1. Items: merged DETERMINISTICALLY in code (concat + near-dupe fold).
    #     Chunks are disjoint transcript spans, so true duplicates are rare;
    #     asking the LLM to merge lists made it return empty lists instead
    #     (bug #21 - constrained-decoding laziness, like bug #17).
    #  2. Summary: ONE LLM call whose schema contains only a text field.
    progress(f"[categorize] merging items from {len(chunk_results)} chunks (in code)...")

    def fold(get_items, prefer_fields: tuple[str, ...]) -> list:
        """Concatenate a category across chunks, folding near-duplicates."""
        merged: list = []
        for c in chunk_results:
            for it in get_items(c):
                dup = None
                for m in merged:
                    if _similar(m.description, it.description):
                        dup = m
                        break
                if dup is None:
                    merged.append(it)
                else:
                    dup.source_segment_ids = sorted(
                        set(dup.source_segment_ids) | set(it.source_segment_ids)
                    )
                    # Prefer the longer description and any non-null detail.
                    if len(it.description) > len(dup.description):
                        dup.description = it.description
                    for f in prefer_fields:
                        if getattr(dup, f, None) in (None, "") and getattr(it, f, None):
                            setattr(dup, f, getattr(it, f))
        return merged

    action_items = fold(lambda c: c.action_items, ("owner", "due"))
    decisions = fold(lambda c: c.decisions, ())
    deadlines = fold(lambda c: c.deadlines, ("date",))
    issues = fold(lambda c: c.issues, ())
    risks = fold(lambda c: c.risks, ("mitigation",))

    _attribute_owners(action_items, seg_by_id, speaker_labels)  # fill self-assigned owners
    if not risks or not issues:  # focused recovery when a category came back empty
        nr, ni = _recover_risks_issues(segments, seg_by_id, speaker_labels,
                                       language, risks, issues, progress)
        risks, issues = risks + nr, issues + ni
    deadlines = _augment_deadlines(deadlines, action_items)  # derive from dated actions (bug #21)
    deadlines = annotate_deadline_dates(deadlines, meeting_date)  # 'by Friday (15/08/2026)'

    progress(
        f"[categorize] merged -> {len(action_items)} actions, {len(decisions)} decisions, "
        f"{len(deadlines)} deadlines, {len(issues)} issues, {len(risks)} risks"
    )

    progress("[categorize] writing final summary (LLM)...")
    key_points = {
        "chunk_summaries": [c.chunk_summary for c in chunk_results],
        "decisions": [d.description for d in decisions][:15],
        "action_items": [a.description for a in action_items][:15],
    }
    try:
        raw = _chat(
            system=_summary_system_prompt(language),
            user="Material from the meeting:\n\n" + json.dumps(key_points, ensure_ascii=False, indent=1),
        )
        summary = FinalSummary.model_validate(raw).summary
    except OllamaUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        # Final-summary call failed (e.g. looping on Chinese) - fall back to the
        # already-generated chunk summaries so the meeting still completes (#23).
        progress(f"[categorize] final summary failed ({type(exc).__name__}); using chunk summaries")
        summary = " ".join(c.chunk_summary for c in chunk_results)[:1500]

    return MeetingInsights(
        summary=_ensure_summary(summary, chunk_results, action_items, decisions),
        action_items=action_items,
        decisions=decisions,
        deadlines=deadlines,
        issues=issues,
        risks=risks,
    )


def _similar(a: str, b: str) -> bool:
    """Near-duplicate check for fold(): normalized equality or containment."""
    na = _norm(a)
    nb = _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(shorter) >= 12 and shorter in longer


def _norm(s: str) -> str:
    return " ".join("".join(ch for ch in s.lower() if ch.isalnum() or ch.isspace()).split())
