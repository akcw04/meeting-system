"""Pydantic models - data shapes shared between FastAPI routes and
the LLM extraction step in Phase 7.

Same schemas double as HTTP request/response validators AND as the
JSON-shape contract for Llama 3.1's structured output.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# 'auto' = LLM picks dominant language for summary
# 'en' = force English summary
# 'zh' = force Mandarin summary
PrimaryLanguage = Literal["auto", "en", "zh"]


class HealthResponse(BaseModel):
    status: str = "ok"
    python_version: str
    cuda_available: bool
    gpu_name: str | None = None
    vram_total_mb: int | None = None


class MeetingCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    primary_language: PrimaryLanguage = "auto"
    expected_speakers: int | None = Field(None, ge=1, le=20)


class MeetingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    original_filename: str
    audio_path: str
    duration_seconds: float | None
    language: str | None
    languages_detected: str | None = None  # e.g. "en,zh" when code-switch detected
    primary_language: str
    expected_speakers: int | None
    status: str
    progress: float | None = None   # 0..1 within the current long stage; null when idle
    created_at: datetime
    updated_at: datetime


class SpeakerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    meeting_id: int
    label: str
    display_name: str | None


class SegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    meeting_id: int
    speaker_id: int | None
    start_seconds: float
    end_seconds: float
    text: str
    language: str | None


class WordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    segment_id: int
    start_seconds: float
    end_seconds: float
    text: str
    score: float | None


class SegmentWithWords(SegmentResponse):
    """Segment plus its word-level alignment from Phase 4."""
    words: list[WordResponse] = []


class TranscriptResponse(BaseModel):
    meeting_id: int
    detected_language: str | None
    segment_count: int
    speakers: list[SpeakerResponse] = []
    segments: list[SegmentWithWords]


class PagedSegments(BaseModel):
    """One slice of a meeting's transcript, for the virtualized list UI."""
    meeting_id: int
    total: int
    offset: int
    limit: int
    segments: list[SegmentResponse]


class SegmentUpdate(BaseModel):
    """PATCH body for editing one segment. Only provided fields change."""
    text: str | None = Field(None, min_length=1)
    speaker_id: int | None = None


class SpeakerUpdate(BaseModel):
    """PATCH body for renaming a speaker (display_name shown everywhere)."""
    display_name: str | None = Field(None, max_length=120)


class SearchHit(BaseModel):
    segment_id: int
    index: int          # ordinal position in the time-ordered transcript (for scroll-to)
    start_seconds: float
    snippet: str


class ActionItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    meeting_id: int
    description: str
    owner: str | None
    due_date: str | None
    source_segment_id: int | None


class DecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    meeting_id: int
    description: str
    source_segment_id: int | None


class DeadlineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    meeting_id: int
    description: str
    target_date: str | None
    source_segment_id: int | None


class IssueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    meeting_id: int
    description: str
    severity: str | None
    source_segment_id: int | None


class RiskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    meeting_id: int
    description: str
    mitigation: str | None
    source_segment_id: int | None
