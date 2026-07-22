"""Pydantic models for user-uploaded export templates (IR §2.2.7).

A template is a user-supplied Word .docx that uses the same docxtpl
placeholder tags as the built-in default. The system fills a chosen
template with a meeting's data at export time, so an organisation can
produce minutes in its own house style without touching code.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    original_filename: str
    created_at: datetime
    # Field keys the engine recognised in the document (headings/markers it can
    # fill). Populated on UPLOAD so the UI can confirm what was found and flag
    # what wasn't (e.g. a 'Riks' typo -> 'risks' missing). null on the list view.
    recognised: list[str] | None = None
