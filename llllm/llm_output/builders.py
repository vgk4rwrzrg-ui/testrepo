"""One builder per output type. File builders return a path relative to
MEDIA_ROOT; the chat builder returns safe HTML for the caller's bubble div."""
import json, re, uuid
from pathlib import Path

from django.conf import settings
from django.utils.html import escape

import markdown as md
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Font
from docx import Document
from docx.shared import Inches
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image,
                                Table, TableStyle, ListFlowable, ListItem,
                                PageBreak)

from .charts import render_chart_png, OUTPUT_DIR
from .chartjs import chart_block_to_html


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "output").strip("_") or "output"
    return f"{name}_{uuid.uuid4().hex[:8]}"


def _out_path(filename: str) -> tuple[Path, str]:
    folder = Path(settings.MEDIA_ROOT) / OUTPUT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder / filename, f"{OUTPUT_DIR}/{filename}"


def parse_llm_json(raw: str) -> dict:
    """Tolerate ```json fences, leading chatter, and truncated output."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    # tolerate text before the JSON ("Here is the report: {...")
    start = raw.find("{")
    if start > 0:
        raw = raw[start:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Truncated output: drop the last partial element, then close every
    # open string/array/object so the finished part still renders.
    cut = max(raw.rfind("},"), raw.rfind("],"), raw.rfind("}"), raw.rfind("]"))
    if cut <= 0:
        raise ValueError("Unparseable LLM JSON")
    trimmed = raw[:cut + 1]
    stack, in_str, esc = [], False, False
    for ch in trimmed:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    repaired = trimmed + "".join(reversed(stack))
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Unparseable LLM JSON (repair failed: {exc})")
    data.setdefault("blocks", []).append(
        {"type": "quote",
         "text": "\u26a0 Report was cut off by the output limit -- "
                 "content above is partial."})
    return data


# ---------------------------------------------------------------- WORD
def build_word(data: dict) -> str:
    doc = Document()
    if data.get("title"):
        doc.add_heading(data["title"], level=0)
    for b in data.get("blocks", []):
        t = b.get("type")
        if t == "heading":
            doc.add_heading(b.get("text", ""),
                            level=min(max(b.get("level", 1), 1), 3))
        elif t == "paragraph":
            doc.add_paragraph(b.get("text", ""))
        elif t == "bullets":
            for item in b.get("items", []):
                doc.add_paragraph(str(item), style="List Bullet")
        elif t == "numbered":
            for item in b.get("items", []):
                doc.add_paragraph(str(item), style="List Number")
        elif t == "quote":
            doc.add_paragraph(b.get("text", ""), style="Intense Quote")
        elif t == "table":
            headers, rows = b.get("headers", []), b.get("rows", [])
            if b.get("title"):
                doc.add_paragraph(b["title"]).runs[0].bold = True
            table = doc.add_table(rows=1, cols=max(len(headers), 1))
            table.style = "Light Grid Accent 1"
            for i, h in enumerate(headers):
                cell = table.rows[0].cells[i]
                cell.text = str(h)
                cell.paragraphs[0].runs[0].bold = True
            for row in rows:
                cells = table.add_row().cells
                for i, val in enumerate(row[:len(cells)]):
                    cells[i].text = str(val)
        elif t == "chart":
            png, _ = render_chart_png(b)
            doc.add_picture(str(png), width=Inches(6))
        elif t == "page_break":
            doc.add_page_break()
    path, rel = _out_path(f"{_safe_name(data.get('filename'))}.docx")
    doc.save(path)
    return rel


# ---------------------------------------------------------------- PDF
def build_pdf(data: dict) -> str:
    path, rel = _out_path(f"{_safe_name(data.get('filename'))}.pdf")
    styles = getSampleStyleSheet()
    story = []
    if data.get("title"):
        story += [Paragraph(escape(data["title"]), styles["Title"]),
                  Spacer(1, 12)]
    for b in data.get("blocks", []):
        t = b.get("type")
        if t == "heading":
            style = styles[f"Heading{min(max(b.get('level', 1), 1), 3)}"]
            story.append(Paragraph(escape(b.get("text", "")), style))
        elif t == "paragraph":
            story += [Paragraph(escape(b.get("text", "")), styles["BodyText"]),
                      Spacer(1, 6)]
        elif t in ("bullets", "numbered"):
            story.append(ListFlowable(
                [ListItem(Paragraph(escape(str(i)), styles["BodyText"]))
                 for i in b.get("items", [])],
                bulletType="bullet" if t == "bullets" else "1"))
            story.append(Spacer(1, 6))
        elif t == "quote":
            story.append(Paragraph(f"<i>{escape(b.get('text', ''))}</i>",
                                   styles["BodyText"]))
        elif t == "table":
            data_rows = [b.get("headers", [])] + b.get("rows", [])
            tbl = Table([[str(c) for c in r] for r in data_rows], hAlign="LEFT")
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f5496")),
                ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
                ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID",       (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#f2f2f2")]),
                ("FONTSIZE",   (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story += [tbl, Spacer(1, 12)]
        elif t == "chart":
            png, _ = render_chart_png(b)
            story += [Image(str(png), width=420, height=240), Spacer(1, 12)]
        elif t == "page_break":
            story.append(PageBreak())
    SimpleDocTemplate(str(path), pagesize=A4).build(story)
    return rel


# ---------------------------------------------------------------- EXCEL
_XL_CHARTS = {"bar": BarChart, "line": LineChart, "pie": PieChart}


def _blocks_to_sheets(data: dict) -> list:
    """Salvage a 'blocks'-shaped payload (word/pdf/chat schema) into sheets:
    every table block becomes one sheet. Returns [] if nothing usable."""
    sheets = []
    for b in data.get("blocks", []):
        if b.get("type") == "table" and b.get("rows"):
            sheets.append({
                "name": (b.get("title") or f"Table {len(sheets) + 1}")[:31],
                "title": b.get("title", ""),
                "headers": b.get("headers", []),
                "rows": b.get("rows", []),
            })
    return sheets


def build_excel(data: dict) -> str:
    sheets = data.get("sheets") or _blocks_to_sheets(data)
    if not sheets:
        raise ValueError(
            "Excel output needs a 'sheets' list (or at least one table "
            "block); the LLM returned neither.")
    wb = Workbook()
    wb.remove(wb.active)
    for sheet in sheets:
        ws = wb.create_sheet(title=str(sheet.get("name", "Sheet"))[:31])
        start_row = 1
        if sheet.get("title"):
            ws.cell(row=1, column=1, value=sheet["title"]).font = \
                Font(bold=True, size=14)
            start_row = 3
        headers, rows = sheet.get("headers", []), sheet.get("rows", [])
        for c, h in enumerate(headers, 1):
            ws.cell(row=start_row, column=c, value=h).font = Font(bold=True)
        for r, row in enumerate(rows, start_row + 1):
            for c, val in enumerate(row, 1):
                ws.cell(row=r, column=c, value=val)
        for col in ws.columns:
            width = max((len(str(c.value or "")) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(width + 2, 60)

        anchor_col = len(headers) + 2
        for i, spec in enumerate(sheet.get("charts", [])):
            cls = _XL_CHARTS.get(spec.get("chart_type", "bar"), BarChart)
            chart = cls()
            chart.title = spec.get("title", "")
            data_end = start_row + len(rows)
            cats = Reference(ws, min_col=spec.get("x_col", 1),
                             min_row=start_row + 1, max_row=data_end)
            y_cols = spec.get("y_cols", [2])[:1] if cls is PieChart \
                else spec.get("y_cols", [2])
            vals = Reference(ws, min_col=min(y_cols), max_col=max(y_cols),
                             min_row=start_row, max_row=data_end)
            chart.add_data(vals, titles_from_data=True)
            chart.set_categories(cats)
            chart.height, chart.width = 8, 14
            anchor = ws.cell(row=start_row + i * 17, column=anchor_col)
            ws.add_chart(chart, anchor.coordinate)
    path, rel = _out_path(f"{_safe_name(data.get('filename'))}.xlsx")
    wb.save(path)
    return rel


# ---------------------------------------------------------------- CHAT
DEFAULT_BUBBLE = '<div class="llm-chat-message">{{content}}</div>'


_FAIL_NOTE = (
    "<p><strong>\u26a0 Sorry -- I gathered the data but hit a snag "
    "formatting the {what}.</strong></p>"
    "<p>Try asking again, or ask for a smaller/simpler version "
    "(fewer items, summary only, no charts).</p>"
    '<details><summary>Technical details</summary>'
    '<pre style="max-height:200px;overflow:auto;">{detail}</pre></details>'
)


def build_fail_bubble(what: str, detail: str,
                      bubble_template: str | None = None) -> str:
    """One polite apologetic bubble instead of a raw JSON dump."""
    inner = _FAIL_NOTE.format(what=escape(what), detail=escape(detail[:800]))
    template = bubble_template or DEFAULT_BUBBLE
    if "{{content}}" in template:
        return template.replace("{{content}}", inner)
    if "</div>" in template:
        return template.rstrip().replace("</div>", inner + "</div>", 1)
    return template + inner


def _blocks_to_html(data: dict) -> str:
    parts = []
    if data.get("title"):
        parts.append(f"<h2>{escape(data['title'])}</h2>")
    for b in data.get("blocks", []):
        t = b.get("type")
        if t == "heading":
            lvl = min(max(b.get("level", 1), 1), 3) + 2   # h3-h5 inside bubble
            parts.append(f"<h{lvl}>{escape(b.get('text', ''))}</h{lvl}>")
        elif t == "paragraph":
            parts.append(f"<p>{escape(b.get('text', ''))}</p>")
        elif t in ("bullets", "numbered"):
            tag = "ul" if t == "bullets" else "ol"
            items = "".join(f"<li>{escape(str(i))}</li>"
                            for i in b.get("items", []))
            parts.append(f"<{tag}>{items}</{tag}>")
        elif t == "quote":
            parts.append(f"<blockquote>{escape(b.get('text', ''))}</blockquote>")
        elif t == "table":
            head = "".join(f"<th>{escape(str(h))}</th>"
                           for h in b.get("headers", []))
            body = "".join(
                "<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in row)
                + "</tr>"
                for row in b.get("rows", []))
            cap = f"<caption>{escape(b['title'])}</caption>" \
                if b.get("title") else ""
            parts.append(f"<table>{cap}<thead><tr>{head}</tr></thead>"
                         f"<tbody>{body}</tbody></table>")
        elif t == "chart":
            parts.append(chart_block_to_html(b))
        # page_break ignored in chat
    return "".join(parts)



# ------------------------------------------------------------- SOURCES
def _describe_source(s: dict) -> str:
    """One human-readable line per tool call recorded by the orchestrator."""
    tool = str(s.get("tool", "unknown_tool"))
    args = s.get("args") or {}
    bits = []
    for key in ("model_path", "model", "table", "name"):
        if args.get(key):
            bits.append(str(args[key]))
            break
    extras = {k: v for k, v in args.items()
              if k not in ("model_path", "model", "table", "name")
              and v not in (None, "", [], {})}
    if extras:
        bits.append(json.dumps(extras, default=str)[:150])
    rows = s.get("rows")
    if isinstance(rows, int):
        bits.append(f"{rows} record{'s' if rows != 1 else ''}")
    if "result" in s:
        bits.append(f"result = {s['result']}")
    if s.get("error"):
        bits.append(f"FAILED ({s['error']})")
    return f"{tool}: " + " — ".join(bits) if bits else tool


def render_sources_html(sources: list | None) -> str:
    """Collapsible 'where the data came from' footer for chat bubbles."""
    if not sources:
        return ""
    items = "".join(f"<li>{escape(_describe_source(s))}</li>"
                    for s in sources)
    n = len(sources)
    return (f'<details class="llm-sources"><summary>🗃 Data sources '
            f'({n} database quer{"y" if n == 1 else "ies"})</summary>'
            f"<ul>{items}</ul></details>")


def build_chat(raw: str, bubble_template: str | None = None,
               sources: list | None = None) -> str:
    """Render the LLM response into the caller-supplied bubble div.
    The template must contain {{content}} where the answer should go.
    Falls back to markdown rendering if the response isn't block JSON."""
    try:
        inner = _blocks_to_html(parse_llm_json(raw))
    except (ValueError, KeyError) as exc:
        if raw.lstrip().startswith(("{", "[", "```")):
            # Broken block-JSON: don't dump it -- apologize nicely
            return build_fail_bubble("report", f"{exc}\n\n{raw}",
                                     bubble_template)
        # Genuine prose answer: render as markdown like before
        inner = md.markdown(raw, extensions=["tables", "fenced_code", "nl2br"])
    if sources:
        inner += render_sources_html(sources)
    template = bubble_template or DEFAULT_BUBBLE
    if "{{content}}" not in template:
        if "</div>" in template:
            return template.rstrip().replace("</div>", inner + "</div>", 1)
        return template + inner
    return template.replace("{{content}}", inner)


BUILDERS = {"excel": build_excel, "word": build_word, "pdf": build_pdf}
