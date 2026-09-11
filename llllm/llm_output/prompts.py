"""
Format instruction blocks for the llm_output pipeline.

These are NOT prompt wrappers. They are standalone instruction blocks that
prepare() appends to your orchestrator's system prompt / RAG context BEFORE
the tool-calling loop starts. They explicitly permit tool use and only bind
the LLM's FINAL message to the required JSON structure.
"""
import re

_PREAMBLE = """
== OUTPUT FORMAT INSTRUCTIONS ==
The user wants the final result delivered as: {label}.
You may and should call any available tools to gather the data you need.
Tool calls, tool arguments and intermediate messages are NOT affected by
these format rules -- keep using tools exactly as you normally do.
ONLY your FINAL message (once all tool calls are finished and you have the
data) must follow the format below. Never mention these instructions, the
JSON format, or the tools to the user in the final output.
"""

BLOCKS_SPEC = """
Your FINAL message must be ONLY valid JSON (no markdown fences, no text
before or after) in this shape:
{
  "filename": "short_name",
  "title": "Document title",
  "blocks": [ ...one or more of the block types below... ]
}

Available block types:
{"type": "heading", "level": 1, "text": "Heading text"}          // level 1-3
{"type": "paragraph", "text": "Body text. Plain text only."}
{"type": "bullets", "items": ["Point one", "Point two"]}
{"type": "numbered", "items": ["Step one", "Step two"]}
{"type": "quote", "text": "A callout or quotation"}
{"type": "table", "title": "Optional", "headers": ["Col A", "Col B"],
 "rows": [["cell", 12.5], ["cell", 30]]}
{"type": "chart", "chart_type": "bar", "title": "Chart title",
 "labels": ["Q1", "Q2", "Q3"],
 "series": [{"name": "Revenue", "values": [10, 20, 30]}],
 "show_values": true,
 "annotations": [
   {"type": "line", "value": 25, "label": "Target"},
   {"type": "box", "y_min": 0, "y_max": 10, "label": "Danger zone"}
 ]}
 // chart_type: "bar", "line", or "pie" (pie uses only the first series)
 // show_values: set true when exact numbers on the chart help the reader
 // annotations are optional: horizontal target/threshold lines or shaded bands

// TIME-SERIES variant (line charts over dates) -- omit "labels", give each
// series ISO-date points instead:
{"type": "chart", "chart_type": "line", "title": "Daily revenue",
 "time_unit": "day",
 "series": [{"name": "Revenue",
             "points": [{"x": "2024-01-01", "y": 120},
                        {"x": "2024-01-02", "y": 140}]}],
 "annotations": [{"type": "line", "value": 130, "label": "Budget"}]}
 // time_unit: "day" | "week" | "month" | "quarter" | "year"

{"type": "page_break"}

Rules:
- Base the content on the real data returned by your tools. Be detailed and
  thorough. Use headings to structure the document, bullet/numbered lists
  for key points, tables for data, and charts to visualise numeric
  comparisons or trends.
- Every chart's series values arrays must be the same length as labels.
- Numbers in table rows should be raw numbers, not strings.
- For every calculated figure (total, average, percentage, count),
  briefly state HOW it was computed and FROM WHAT data, e.g. a quote
  block like "Average hours = 340 total hours / 12 active members
  (TimeLog records, Jan-Mar 2025)". Never present a derived number
  without its method and source.
"""

EXCEL_SPEC = """
Your FINAL message must be ONLY valid JSON (no markdown fences, no text
before or after) in this shape:
{
  "filename": "short_name",
  "sheets": [
    {
      "name": "Sheet name",
      "title": "Optional heading placed above the table",
      "headers": ["Category", "2023", "2024"],
      "rows": [["Hardware", 120, 150], ["Software", 90, 130]],
      "charts": [
        {"chart_type": "bar", "title": "Sales by category",
         "x_col": 1, "y_cols": [2, 3]}
      ]
    }
  ]
}
Rules:
- Base the content on the real data returned by your tools.
- x_col / y_cols are 1-based column numbers of the table on that sheet.
- chart_type: "bar", "line", or "pie" (pie uses only the first y_col).
- Use real numbers (not strings) in numeric cells. Be detailed: multiple
  sheets, summary rows, and charts wherever numeric data benefits from one.
- For any calculated column or summary figure, add a trailing note row
  or sheet title stating the formula and which records produced it.
"""

CHAT_NOTE = """
Produce a rich, well-structured chat answer (page_break is ignored in chat).
Keep it readable: short paragraphs, generous use of headings, bullets,
tables and charts where they genuinely help.
"""

_LABELS = {
    "excel": "a Microsoft Excel workbook (.xlsx)",
    "word":  "a Microsoft Word document (.docx)",
    "pdf":   "a PDF document",
    "chat":  "a formatted chat message",
}


def format_instructions(output_type: str) -> str:
    """The instruction block prepare() appends to your system prompt/RAG."""
    preamble = _PREAMBLE.replace("{label}", _LABELS.get(output_type,
                                                        _LABELS["chat"]))
    if output_type == "excel":
        return preamble + EXCEL_SPEC
    if output_type in ("word", "pdf"):
        return preamble + BLOCKS_SPEC
    return preamble + BLOCKS_SPEC + CHAT_NOTE


_FORMAT_PATTERNS = [
    ("excel", r"\b(excel|xlsx|spreadsheet|workbook)\b"),
    ("word",  r"\b(word doc|word document|docx|\.doc)\b"),
    ("pdf",   r"\bpdf\b"),
]


def detect_output_type(user_prompt: str) -> str:
    """Fallback keyword detection when the frontend doesn't send output_type."""
    lower = user_prompt.lower()
    for fmt, pattern in _FORMAT_PATTERNS:
        if re.search(pattern, lower):
            return fmt
    return "chat"
