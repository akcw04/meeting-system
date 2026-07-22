"""Template routes - manage user-uploaded Word export templates (IR §2.2.7).

A template is an ordinary Word .docx the user designs themselves - NO code-like
tags. They mark where data should go either with plain section HEADINGS (e.g.
"Summary", "Action Items", "决策") or friendly [[markers]] (e.g. [[Date]]); the
fill engine (pipeline/fill_template.py) recognises those and fills the document
at export time, preserving the user's fonts/layout.

GET    /templates                    -> list saved templates (newest first)
POST   /templates                    -> upload + validate (has a fillable spot) + save
GET    /templates/starter/download   -> download the friendly sample to start from
GET    /templates/{id}/download      -> download a saved template file
DELETE /templates/{id}               -> delete a saved template (row + file)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from app.config import settings
from app.db import get_conn
from app.pipeline.export import TemplateRenderError
from app.pipeline.fill_template import recognised_fields
from app.schemas.template import TemplateResponse

router = APIRouter(prefix="/templates", tags=["templates"])

ALLOWED_SUFFIX = ".docx"
# Templates are tiny (a few KB of XML + any embedded logo), so a small cap
# is plenty and guards against an accidental wrong-file upload.
MAX_TEMPLATE_BYTES = 10 * 1024 * 1024  # 10 MB
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
# The friendly "starter" users download: plain headings + [[marker]] examples
# (NOT the docxtpl default). Regenerate with templates/generate_sample_template.py.
SAMPLE_PATH = Path(__file__).resolve().parent.parent.parent / "templates" / "sample_template.docx"


def _stored_path(template_id: int) -> Path:
    """On-disk location of a saved template's .docx (named by its DB id)."""
    return settings.templates_dir / f"{template_id}.docx"


@router.get("", response_model=list[TemplateResponse])
def list_templates() -> list[TemplateResponse]:
    """All saved templates, newest first (for the management UI + export picker)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM templates ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [TemplateResponse.model_validate(dict(r)) for r in rows]


@router.post("", response_model=TemplateResponse, status_code=status.HTTP_201_CREATED)
async def upload_template(
    name: str = Form(..., min_length=1, max_length=120),
    file: UploadFile = File(...),
) -> TemplateResponse:
    """Save an uploaded .docx after checking it has at least one fillable spot.

    A valid template references at least one recognised field via a plain section
    heading or a [[marker]] (recognised_fields). A non-.docx, or a doc with no
    recognised headings/markers, is rejected with a clear 400 rather than
    producing an unchanged document at export time.
    """
    if not file.filename:
        raise HTTPException(400, "No filename provided")
    if Path(file.filename).suffix.lower() != ALLOWED_SUFFIX:
        raise HTTPException(400, "Template must be a Word .docx file")

    # Read with a cap (read one byte past the limit to detect an over-size file).
    data = await file.read(MAX_TEMPLATE_BYTES + 1)
    if len(data) > MAX_TEMPLATE_BYTES:
        raise HTTPException(
            413, f"Template exceeds the {MAX_TEMPLATE_BYTES // (1024 * 1024)} MB limit"
        )
    if not data:
        raise HTTPException(400, "Uploaded template is empty")

    settings.ensure_dirs()
    # Stage to a temp file, confirm it's a valid .docx with at least one
    # recognised heading/marker, and only then commit a DB row + move into place.
    tmp_path = settings.templates_dir / f"_upload_{Path(file.filename).name}"
    try:
        tmp_path.write_bytes(data)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Failed to save uploaded template: {exc}")
    try:
        fields = recognised_fields(tmp_path)
    except TemplateRenderError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))
    if not fields:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            400,
            "We couldn't find anything fillable in this document. Add a section "
            "heading like 'Summary' or 'Action Items' (English or Chinese), or a "
            "marker like [[Summary]], then upload again - the sample shows how.",
        )

    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO templates (name, original_filename) VALUES (?, ?)",
            (name.strip(), file.filename),
        )
        template_id = cursor.lastrowid
    if template_id is None:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(500, "Failed to allocate template id")

    tmp_path.replace(_stored_path(template_id))  # atomic move into place

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    resp = TemplateResponse.model_validate(dict(row))
    resp.recognised = fields  # surface what was recognised so the UI can confirm/flag
    return resp


# NOTE: declared BEFORE /{template_id}/download so the literal 'starter' path
# wins (a typed int path param would otherwise 422 on "starter").
@router.get("/starter/download")
def download_starter() -> FileResponse:
    """Download the friendly sample (plain headings + [[marker]] examples) to start from."""
    if not SAMPLE_PATH.is_file():
        raise HTTPException(
            500,
            "Sample template missing on the server. Regenerate it with: "
            "python templates\\generate_sample_template.py",
        )
    return FileResponse(
        path=str(SAMPLE_PATH),
        filename="meeting_minutes_sample_template.docx",
        media_type=DOCX_MEDIA_TYPE,
    )


@router.get("/{template_id}/download")
def download_template(template_id: int) -> FileResponse:
    """Download a previously saved template (e.g. to re-edit it in Word)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"Template {template_id} not found")
    path = _stored_path(template_id)
    if not path.is_file():
        raise HTTPException(500, f"Template {template_id} file is missing on disk.")
    return FileResponse(
        path=str(path), filename=row["original_filename"], media_type=DOCX_MEDIA_TYPE
    )


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(template_id: int) -> None:
    """Delete a saved template: remove the DB row and its file on disk."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Template {template_id} not found")
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
    _stored_path(template_id).unlink(missing_ok=True)
