"""Generate templates/sample_template.docx - the friendly, PROFESSIONAL starter.

A real-world meeting-minutes layout that the fill engine recognises with NO
code-like tags:
  - plain section HEADINGS (Meeting Summary, Key Decisions, Action Items, ...)
    are filled underneath automatically;
  - inline [[markers]] (e.g. [[Date]]) drop a single value exactly where placed.

Structure follows common professional minutes conventions (a date/attendees
details block, then Summary -> Key Decisions -> Action Items (who/what/when) ->
Deadlines -> Issues/Risks -> full record). Users restyle freely - only the
recognised names matter.

    python templates\\generate_sample_template.py
"""
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ACCENT = RGBColor(0x1F, 0x4E, 0x79)
MUTED = RGBColor(0x6B, 0x77, 0x85)
OUT = Path(__file__).resolve().parent / "sample_template.docx"

# Details block: (label, marker). Labels are static; markers get filled.
META = [
    ("Date", "[[Date]]"),
    ("Duration", "[[Duration]]"),
    ("Language", "[[Language]]"),
    ("Attendees", "[[Attendees]]"),
]
# Section headings the system fills underneath, in professional order.
SECTIONS = ["Meeting Summary", "Key Decisions", "Action Items",
            "Deadlines", "Technical Issues", "Risks"]


def _bottom_border(paragraph, color: str = "1F4E79", size: int = 10) -> None:
    """Add a thin coloured rule under a paragraph (used beneath the title block)."""
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "6")
    bottom.set(qn("w:color"), color)
    pBdr.append(bottom)
    pPr.append(pBdr)


def main() -> None:
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    h1 = doc.styles["Heading 1"]
    h1.font.name = "Calibri"
    h1.font.size = Pt(14)
    h1.font.bold = True
    h1.font.color.rgb = ACCENT

    # --- Header block ---
    org = doc.add_paragraph()
    orun = org.add_run("[ Your organisation — replace with your logo or company name ]")
    orun.italic = True
    orun.font.size = Pt(9)
    orun.font.color.rgb = MUTED

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    trun = title.add_run("MEETING MINUTES")
    trun.bold = True
    trun.font.size = Pt(22)
    trun.font.color.rgb = ACCENT

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    srun = subtitle.add_run("[[Meeting Title]]")
    srun.font.size = Pt(13)
    srun.font.color.rgb = ACCENT
    _bottom_border(subtitle)  # rule under the header

    guide = doc.add_paragraph()
    grun = guide.add_run(
        "Template guide (delete this line before sending): keep the section headings below and the "
        "double-bracket markers in the table above - the system fills them in automatically. "
        "Restyle anything and add your logo above."
    )
    grun.italic = True
    grun.font.size = Pt(8.5)
    grun.font.color.rgb = MUTED

    # --- Details block (meta table) ---
    table = doc.add_table(rows=len(META), cols=2)
    try:
        table.style = "Light Grid Accent 1"
    except Exception:  # noqa: BLE001 - style fallback for odd Word installs
        table.style = "Table Grid"
    table.autofit = False
    for i, (label, marker) in enumerate(META):
        label_cell, value_cell = table.rows[i].cells
        label_cell.text = label
        for r in label_cell.paragraphs[0].runs:
            r.bold = True
        value_cell.text = marker
        label_cell.width = Inches(1.5)
        value_cell.width = Inches(4.8)

    doc.add_paragraph()  # spacer

    # --- Sections (filled underneath each heading) ---
    for section in SECTIONS:
        doc.add_heading(section, level=1)

    # --- Full transcript as an appendix on its own page ---
    doc.add_page_break()
    doc.add_heading("Full Transcript", level=1)

    # --- Footer ---
    doc.add_paragraph()
    foot = doc.add_paragraph()
    foot.add_run("Prepared by: ____________________          "
                 "Next meeting: ____________________").font.size = Pt(10)
    note = doc.add_paragraph()
    nrun = note.add_run("Generated with the Meeting System.")
    nrun.italic = True
    nrun.font.size = Pt(8.5)
    nrun.font.color.rgb = MUTED

    doc.save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
