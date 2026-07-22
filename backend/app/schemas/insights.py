"""Pydantic models for LLM-extracted meeting insights.

These serve double duty:
  1. As the documented shape we ask Llama to emit. Ollama runs in
     `format=json` mode (lightweight valid-JSON grammar) and the exact field
     shape is specified in the prompt - full Pydantic-schema-constrained
     decoding was ~10x slower on this 6 GB setup (see docs/BUGS.md #25).
  2. As the validation layer (Pydantic) when we parse the response.

Locked decisions (DECISIONS.md 2026-06-10):
  - Categories: Action Items, Key Decisions, Deadlines, Technical Issues, Risks
  - Concise bullet phrasing
  - Action items carry speaker attribution (owner)
  - Overall summary capped at ~300 words
  - All output in the meeting's primary language
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ExtractedActionItem(BaseModel):
    description: str = Field(description="Concise action-oriented bullet, max ~15 words")
    owner: str | None = Field(
        default=None,
        description="Speaker label of whoever owns this task, e.g. 'Speaker 3'. null if unclear.",
    )
    due: str | None = Field(
        default=None,
        description="Due date or timeframe if mentioned, e.g. 'by Friday', '2026-07-01'. null if none.",
    )
    source_segment_ids: list[int] = Field(
        default_factory=list,
        description="Segment id(s) from the transcript that support this item.",
    )


class ExtractedDecision(BaseModel):
    description: str = Field(description="Concise bullet stating what was decided")
    source_segment_ids: list[int] = Field(default_factory=list)


class ExtractedDeadline(BaseModel):
    description: str = Field(description="What the deadline is for")
    date: str | None = Field(
        default=None, description="The date/timeframe, e.g. '3 July', 'end of Q3'"
    )
    source_segment_ids: list[int] = Field(default_factory=list)


class ExtractedIssue(BaseModel):
    description: str = Field(description="Concise bullet describing the technical issue or roadblock")
    source_segment_ids: list[int] = Field(default_factory=list)


class ExtractedRisk(BaseModel):
    description: str = Field(description="Concise bullet describing the risk")
    mitigation: str | None = Field(
        default=None, description="Mitigation mentioned in the meeting, if any"
    )
    source_segment_ids: list[int] = Field(default_factory=list)


class ChunkInsights(BaseModel):
    """What the LLM extracts from ONE transcript chunk (map step)."""
    # No min_length: we no longer use schema-constrained decoding (it ran ~10x
    # slower here - bug #25), so a hard validation floor would just DROP an
    # otherwise-good chunk (losing its items) whenever the 8B model writes a
    # short summary. Length is requested in the prompt instead; default "" keeps
    # the chunk's extracted items even if its summary is weak or missing.
    chunk_summary: str = Field(
        default="",
        description=(
            "Summary of what was discussed in this chunk: 2-4 complete "
            "sentences (at least 40 words) covering topics, outcomes, and context."
        ),
    )
    action_items: list[ExtractedActionItem] = Field(default_factory=list)
    decisions: list[ExtractedDecision] = Field(default_factory=list)
    deadlines: list[ExtractedDeadline] = Field(default_factory=list)
    issues: list[ExtractedIssue] = Field(default_factory=list)
    risks: list[ExtractedRisk] = Field(default_factory=list)


class MeetingInsights(BaseModel):
    """The final consolidated output (after the reduce step)."""
    # Built in code from already-extracted data, so no min_length floor (which
    # would crash the single-chunk path on a short chunk_summary - bug #25).
    summary: str = Field(
        default="",
        description=(
            "Overall meeting summary: a coherent paragraph (or two), at most "
            "~300 words, covering the meeting's purpose, main topics, and outcomes."
        ),
    )
    action_items: list[ExtractedActionItem] = Field(default_factory=list)
    decisions: list[ExtractedDecision] = Field(default_factory=list)
    deadlines: list[ExtractedDeadline] = Field(default_factory=list)
    issues: list[ExtractedIssue] = Field(default_factory=list)
    risks: list[ExtractedRisk] = Field(default_factory=list)


class FinalSummary(BaseModel):
    """Reduce-step LLM output: ONLY the overall summary.

    Bug #21: when the reduce call was asked to merge item lists under a
    constrained schema, the 8B model returned empty lists (the laziest
    valid completion - bug #17's sibling). Item merging is now done
    deterministically in code; the LLM only writes this one text field.
    """
    # No min_length (bug #25): on a short reply the caller falls back to the
    # concatenated chunk summaries rather than crashing on validation.
    summary: str = Field(
        description=(
            "Overall summary of the WHOLE meeting in 300 words or less, "
            "covering purpose, main topics, decisions, and outcomes. "
            "Complete sentences, single coherent narrative."
        ),
    )


# === API response models (read from DB) ===

class InsightItemResponse(BaseModel):
    id: int
    description: str
    owner: str | None = None
    due_date: str | None = None
    target_date: str | None = None
    severity: str | None = None
    mitigation: str | None = None
    source_segment_id: int | None = None
    # True when the cited segment shares no content with this item's wording
    # (a "verify this citation" hint for the user; computed at read time, never
    # removes the item). See app/pipeline/citation.py.
    low_support: bool = False


class InsightsResponse(BaseModel):
    meeting_id: int
    status: str
    summary: str | None
    action_items: list[InsightItemResponse]
    decisions: list[InsightItemResponse]
    deadlines: list[InsightItemResponse]
    issues: list[InsightItemResponse]
    risks: list[InsightItemResponse]


class InsightItemUpdate(BaseModel):
    """User edit to a single extracted item (human-in-the-loop correction).

    All fields optional - only the ones supplied are written, and only those
    valid for the item's category (enforced in the route)."""
    description: str | None = None
    owner: str | None = None
    due_date: str | None = None
    target_date: str | None = None
    severity: str | None = None
    mitigation: str | None = None
