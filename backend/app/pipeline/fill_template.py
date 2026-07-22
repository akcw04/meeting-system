"""Fill a USER's own Word document with a meeting's data - no code-like syntax.

Unlike the built-in docxtpl template (which uses {{ Jinja }} tags), a user
brings an ordinary .docx and we fill it WITHOUT learning any markup. For each
field we look, in this order:

  1. MARKER  - a plain-English placeholder like [[Summary]] or [[Action Items]]
               dropped exactly where the user wants that content. Inline for
               scalars (e.g. "Date: [[Date]]"); on its own line for lists/tables.
  2. HEADING - failing a marker, a section heading whose text matches the field
               name (e.g. "Action Items", "决策") - we fill underneath it.

Recognised names work in English and Chinese and are case-insensitive. We only
INSERT content (never restyle), so the document's fonts/layout are preserved.

`fill_user_document` returns the saved path + a report of which fields were
placed (and how) and which had data but found no spot.
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph

from app.config import settings
from app.pipeline.export import TemplateRenderError, _slug, build_meeting_context

# --- recognised field names (headings AND [[markers]]), English + Chinese ---
_FIELD_SYNONYMS = {
    "title": ["meeting title", "title", "meeting name", "subject", "meeting subject",
              "会议标题", "标题", "会议主题", "主题"],
    "date": ["date", "meeting date", "日期", "会议日期"],
    "duration": ["duration", "length", "会议时长", "时长"],
    "language": ["language", "languages", "语言"],
    "participants": ["participant count", "number of participants", "no of participants",
                     "no. of participants", "headcount", "人数", "参与人数", "与会人数", "出席人数"],
    "attendees": ["attendees", "attendee", "participants", "participant", "present",
                  "attendance", "出席", "出席人员", "与会者", "参与者", "参会人员", "参加人员"],
    "summary": ["summary", "meeting summary", "overview", "abstract", "executive summary",
                "摘要", "会议摘要", "概要", "总结", "会议总结", "内容摘要"],
    "action_items": ["action items", "action item", "actions", "action points", "action plan",
                     "tasks", "task list", "to-do", "to do", "todo", "follow-ups", "follow ups",
                     "行动项", "行动事项", "待办", "待办事项", "任务", "任务清单", "行动计划", "跟进事项"],
    "decisions": ["key decisions", "decisions", "decision", "decisions made", "resolutions",
                  "agreements", "决策", "决定", "关键决策", "议决", "决议", "达成的决定"],
    "deadlines": ["deadlines", "deadline", "due dates", "due date", "timeline", "timelines",
                  "key dates", "schedule", "期限", "截止日期", "截止时间", "时间表", "时间节点", "重要日期"],
    "issues": ["technical issues", "issues", "issue", "problems", "problem", "roadblocks",
               "blockers", "challenges", "技术问题", "问题", "障碍", "技术难题", "待解决问题", "难点"],
    "risks": ["risks", "risk", "risks and mitigations", "risk and mitigation",
              "风险", "风险与缓解", "风险点", "潜在风险", "隐患"],
    "transcript": ["transcript", "full transcript", "meeting transcript", "verbatim", "minutes",
                   "逐字稿", "全文", "会议记录", "记录", "完整记录", "会议全文"],
}
_SCALAR = {"title", "date", "duration", "language", "participants", "attendees"}
_BLOCK = {"summary", "action_items", "decisions", "deadlines", "issues", "risks", "transcript"}

_MARKER = re.compile(r"\[\[\s*(.+?)\s*\]\]")


def _norm(s: str | None) -> str:
    """Normalise a heading/marker name for matching: lowercase, trim, drop a
    trailing colon/period, collapse whitespace."""
    s = (s or "").strip().lower().rstrip(":：.。").strip()
    return re.sub(r"\s+", " ", s)


# normalised synonym -> field key
_LOOKUP: dict[str, str] = {}
for _key, _syns in _FIELD_SYNONYMS.items():
    for _syn in _syns:
        _LOOKUP[_norm(_syn)] = _key


# --- value rendering ------------------------------------------------------

def _scalar_value(key: str, ctx: dict) -> str:
    m = ctx["meeting"]
    return {
        "title": m["title"],
        "date": m["date"],
        "duration": m["duration"],
        "language": m["language_name"],
        "participants": str(m["speaker_count"]),
        "attendees": ", ".join(ctx["attendees"]) or "—",
    }.get(key, "")


def _block_plaintext(key: str, ctx: dict) -> str:
    """A one-line text rendering, used when a block marker sits inline in a
    sentence or inside a table cell (where we can't insert a table/bullets)."""
    if key == "summary":
        return ctx["summary"]
    if key == "action_items":
        return "; ".join(a["description"] + (f" ({a['owner']})" if a.get("owner") else "")
                         for a in ctx["action_items"]) or "None recorded."
    if key == "deadlines":
        return "; ".join(d["description"] + (f" ({d['date']})" if d.get("date") else "")
                         for d in ctx["deadlines"]) or "None recorded."
    if key in ("decisions", "issues"):
        return "; ".join(i["description"] for i in ctx[key]) or "None recorded."
    if key == "risks":
        return "; ".join(r["description"] + (f" (Mitigation: {r['mitigation']})" if r.get("mitigation") else "")
                         for r in ctx["risks"]) or "None recorded."
    if key == "transcript":
        return " / ".join(f"[{s['time']}] {s['speaker']}: {s['text']}" for s in ctx["transcript"])
    return ""


# --- docx insertion helpers ----------------------------------------------

def _insert_paragraph_after(paragraph: Paragraph, text: str = "", italic: bool = False) -> Paragraph:
    """Insert a new paragraph right after `paragraph`; return the new Paragraph."""
    new_p = OxmlElement("w:p")
    paragraph._p.addnext(new_p)
    para = Paragraph(new_p, paragraph._parent)
    if text:
        run = para.add_run(text)
        run.italic = italic
    return para


def _insert_table_after(doc, paragraph: Paragraph, headers: list[str], rows: list[list]) -> None:
    table = doc.add_table(rows=1, cols=len(headers))  # created at end of body...
    try:
        table.style = "Light Grid Accent 1"
    except Exception:  # noqa: BLE001 - style may not exist in an arbitrary user doc
        pass
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = h
        for r in cell.paragraphs[0].runs:
            r.bold = True
    for row in rows:
        cells = table.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = "" if v is None else str(v)
    paragraph._p.addnext(table._tbl)  # ...then moved to sit right after the anchor


def _delete_paragraph(paragraph: Paragraph) -> None:
    paragraph._p.getparent().remove(paragraph._p)


def _insert_field(doc, anchor: Paragraph, key: str, ctx: dict) -> None:
    """Insert a field's content as block(s) right after `anchor`."""
    if key in _SCALAR:
        _insert_paragraph_after(anchor, _scalar_value(key, ctx))
        return
    if key == "summary":
        _insert_paragraph_after(anchor, ctx["summary"])
        return
    if key == "action_items":
        items = ctx["action_items"]
        if not items:
            _insert_paragraph_after(anchor, "None recorded.", italic=True)
            return
        rows = [[str(i),
                 a["description"] + (f"  [{a['source']}]" if a.get("source") else ""),
                 a.get("owner") or "-", a.get("due") or "-"]
                for i, a in enumerate(items, 1)]
        _insert_table_after(doc, anchor, ["No.", "Action", "Owner", "Due"], rows)
        return
    if key == "deadlines":
        items = ctx["deadlines"]
        if not items:
            _insert_paragraph_after(anchor, "None recorded.", italic=True)
            return
        rows = [[str(i),
                 d["description"] + (f"  [{d['source']}]" if d.get("source") else ""),
                 d.get("date") or "-"] for i, d in enumerate(items, 1)]
        _insert_table_after(doc, anchor, ["No.", "Deadline", "Date"], rows)
        return
    if key in ("decisions", "issues", "risks"):
        items = ctx[key]
        if not items:
            _insert_paragraph_after(anchor, "None recorded.", italic=True)
            return
        cur = anchor
        for it in items:
            line = "• " + it["description"]
            if it.get("source"):
                line += f"  [{it['source']}]"
            if key == "risks" and it.get("mitigation"):
                line += "  —  Mitigation: " + it["mitigation"]
            cur = _insert_paragraph_after(cur, line)
        return
    if key == "transcript":
        cur = anchor
        for seg in ctx["transcript"]:
            cur = _insert_paragraph_after(cur, f"[{seg['time']}] {seg['speaker']}: {seg['text']}")
        return


def _set_paragraph_text(para: Paragraph, new_text: str) -> None:
    """Replace a paragraph's text, keeping the first run's formatting."""
    if para.text == new_text:
        return
    if para.runs:
        para.runs[0].text = new_text
        for r in para.runs[1:]:
            r.text = ""
    else:
        para.add_run(new_text)


def _resolve_inline(text: str, ctx: dict, filled: dict) -> str:
    """Replace every recognised [[marker]] in a line with its value (scalar, or
    a one-line rendering for block fields). Unknown markers are left untouched."""
    def repl(match: re.Match) -> str:
        key = _LOOKUP.get(_norm(match.group(1)))
        if not key:
            return match.group(0)
        filled.setdefault(key, "marker")
        return _block_plaintext(key, ctx) if key in _BLOCK else _scalar_value(key, ctx)
    return _MARKER.sub(repl, text)


# --- the two passes -------------------------------------------------------

def _fill_markers(doc, ctx: dict, filled: dict) -> None:
    # Body paragraphs: a line that is ONLY a block marker becomes the real block.
    for para in list(doc.paragraphs):
        only = _MARKER.fullmatch(para.text.strip())
        if only:
            key = _LOOKUP.get(_norm(only.group(1)))
            if key in _BLOCK:
                if key not in filled:
                    _insert_field(doc, para, key, ctx)
                    filled[key] = "marker"
                _delete_paragraph(para)
                continue
        if "[[" in para.text:
            _set_paragraph_text(para, _resolve_inline(para.text, ctx, filled))
    # Table cells: scalar / inline-block markers only (no block insertion).
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    if "[[" in para.text:
                        _set_paragraph_text(para, _resolve_inline(para.text, ctx, filled))


def _fill_headings(doc, ctx: dict, filled: dict) -> None:
    for para in list(doc.paragraphs):
        key = _LOOKUP.get(_norm(para.text))
        if key and key not in filled:
            _insert_field(doc, para, key, ctx)
            filled[key] = "heading"


# --- public API -----------------------------------------------------------

def _all_paragraphs(doc):
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs


def recognised_fields(template_path: Path) -> list[str]:
    """Field keys the document references via a heading or a [[marker]]. Used to
    validate an upload has at least one fillable spot. Raises TemplateRenderError
    if the file cannot be opened as a .docx."""
    try:
        doc = Document(str(template_path))
    except Exception as exc:  # noqa: BLE001 - zipfile/docx raise various types
        raise TemplateRenderError(
            f"Could not open this file as a Word .docx ({type(exc).__name__}). "
            "Please upload a valid .docx saved from Word."
        ) from exc
    found: set[str] = set()
    for para in _all_paragraphs(doc):
        if _norm(para.text) in _LOOKUP:
            found.add(_LOOKUP[_norm(para.text)])
        for name in _MARKER.findall(para.text):
            key = _LOOKUP.get(_norm(name))
            if key:
                found.add(key)
    return sorted(found)


def _data_fields(ctx: dict) -> list[str]:
    """Fields that actually have content worth placing (for the skipped report)."""
    fields = ["title", "date", "duration", "language", "participants", "summary", "transcript"]
    if ctx["attendees"]:
        fields.append("attendees")
    for key in ("action_items", "decisions", "deadlines", "issues", "risks"):
        if ctx[key]:
            fields.append(key)
    return fields


def fill_user_document(meeting_id: int, template_path: Path) -> tuple[Path, dict]:
    """Fill the user's .docx with the meeting's data via markers + headings.

    Returns (output_path, report) where report = {placed: {field: 'marker'|'heading'},
    skipped: [fields that had data but found no marker/heading]}.
    """
    ctx = build_meeting_context(meeting_id)  # ValueError if missing / no transcript
    try:
        doc = Document(str(template_path))
    except Exception as exc:  # noqa: BLE001
        raise TemplateRenderError(
            f"Could not open this file as a Word .docx ({type(exc).__name__}). "
            "Please upload a valid .docx saved from Word."
        ) from exc

    filled: dict[str, str] = {}
    _fill_markers(doc, ctx, filled)
    _fill_headings(doc, ctx, filled)

    skipped = [k for k in _data_fields(ctx) if k not in filled]

    out_dir = settings.output_dir / str(meeting_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_slug(ctx['meeting']['title'])}_minutes.docx"
    doc.save(str(out_path))
    return out_path, {"placed": filled, "skipped": skipped}
