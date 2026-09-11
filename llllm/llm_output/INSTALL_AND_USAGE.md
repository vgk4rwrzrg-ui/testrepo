# llm_output — Django LLM Structured-Output Module

Takes a user prompt, injects format-specific structure instructions for your
LLM, then builds the result as an **Excel**, **Word**, or **PDF** file (returned
as a download link) or as **rich chat HTML** rendered into your own chat
bubble div — with native **Chart.js** charts (datalabels, annotation, zoom and
date-fns time-scale plugins supported).

---

## 1. Installation

### 1.1 Copy the app

Copy the `llm_output/` folder into your Django project root (next to
`manage.py`).

### 1.2 Install Python dependencies

```bash
pip install -r llm_output/requirements.txt
```

(Django, openpyxl, python-docx, reportlab, matplotlib, Markdown)

### 1.3 settings.py

```python
INSTALLED_APPS = [
    # ...
    "llm_output",
]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"   # generated files land in media/llm_files/
```

### 1.4 Project urls.py

Since you already use `api/llm/` for other endpoints, wire this into your
existing `api/llm/` URL config:

```python
# In your existing api/llm urls.py (or wherever api/llm/ routes are defined):
from llm_output import views as llm_output_views

urlpatterns = [
    # ... your existing api/llm/ patterns ...
    path("output/generate/", llm_output_views.generate, name="llm_output_generate"),
]
```

**OR** if you prefer to include the whole llm_output URLconf under a subpath:

```python
# In your existing api/llm urls.py:
from django.urls import include

urlpatterns = [
    # ... your existing patterns ...
    path("output/", include("llm_output.urls")),
]
```

Either way, the endpoint will be at **`/api/llm/output/generate/`**

Don't forget media serving for development (add to your project's root urls.py
if not already present):

```python
from django.conf import settings
from django.conf.urls.static import static

# at the end of urlpatterns:
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
```

### 1.5 Wire up your orchestrator

Either call `prepare()`/`finalize()` from your own chat view (recommended --
see section 2), or implement `run_orchestrator()` in `llm_output/views.py`
to use the bundled reference endpoint. A sketch of the expected tool-loop
contract is in its docstring.

### 1.6 Frontend assets

Include on any page that shows chat (order matters — plugins after chart.js):

```html
<script src="chart.js"></script>
<script src="chartjs-adapter-date-fns.js"></script>
<script src="chartjs-plugin-zoom.min.js"></script>
<script src="chartjs-plugin-datalabels.js"></script>
<script src="chartjs-plugin-annotation.min.js"></script>

<link rel="stylesheet" href="{% static 'llm_output/chat.css' %}">
<script src="{% static 'llm_output/chat.js' %}"></script>
```

Then run `python manage.py collectstatic` for production.


---

## 2. How it plugs into your backend (IMPORTANT)

Your RAG, LLM router/orchestrator and AI_tool all live in your Python views
and stay **unchanged**. llm_output runs in exactly two places around them:

```
User types: "give me all the locations of loyalty program personnel
             in a word document"                          (+ picks type in UI)
        |
        v
 YOUR chat view (backend)
        |
        v
 [1] prep = prepare(user_message, output_type)      <- llm_output, PHASE 1
        |     prep.instructions = format rules to APPEND to your
        |     system prompt / RAG context (not the user message!)
        v
 YOUR orchestrator loop (unchanged):
     LLM reads message -> router picks tools -> AI_tool fetches data
     -> results go back to the LLM -> repeat as needed
     -> LLM writes its FINAL message following prep.instructions
        |
        v
 [2] result = finalize(raw_final_message, prep, bubble_template)
        |                                            <- llm_output, PHASE 2
        |     word/pdf/excel: builds the file into MEDIA_ROOT, returns
        |         {"type": "word", "download_url": ..., "filename": ...,
        |          "html": "<bubble with attachment link>"}
        |     chat: renders blocks into the bubble template, returns
        |         {"type": "chat", "html": "<bubble>"}
        v
 JsonResponse(result)  ->  frontend JS appends result.html to the chat
                           (and calls hydrateLlmCharts() for charts)
```

Integration inside YOUR existing chat view is three lines:

```python
from llm_output.pipeline import prepare, finalize

def my_chat_view(request):
    ...
    prep = prepare(user_message, output_type_from_ui)      # [1]
    raw = my_orchestrator(user_message,                    # yours, unchanged
                          extra_system=prep.instructions)
    result = finalize(raw, prep, bubble_template)          # [2]
    return JsonResponse(result)
```

Rules of thumb:
- Append `prep.instructions` to the **system prompt** (or as a RAG context
  document). Never put it in the user message -- it must survive every
  round of the tool loop.
- Pass the LLM's **final** message to `finalize()`, never tool traffic.
- The instructions explicitly tell the LLM that tool calls are exempt from
  the format rules, so your router keeps working exactly as before.
- The frontend stays dumb: it POSTs the message + chosen output type, then
  appends `result.html` to the chat. For files, `result.html` is already a
  bubble containing the download link; `download_url`/`filename` are also
  provided separately if you prefer to render your own attachment UI.

## 3. Usage

### 2.1 Endpoint

`POST /api/llm/output/generate/` with JSON body:

| Field             | Required | Description                                              |
|-------------------|----------|----------------------------------------------------------|
| `prompt`          | yes      | The user's request text                                  |
| `output_type`     | no       | `"excel"` / `"word"` / `"pdf"` / `"chat"`. Auto-detected from keywords ("spreadsheet", "word document", "pdf", ...) if omitted. |
| `bubble_template` | no       | Your chat bubble HTML containing `{{content}}` where the answer goes. |

### 2.2 Responses

File formats:

```json
{"type": "word", "download_url": "/media/llm_files/report_ab12cd34.docx",
 "filename": "report_ab12cd34.docx"}
```

Chat (or graceful fallback when the LLM returns malformed JSON — see
`warning` field):

```json
{"type": "chat", "html": "<div class=\"chat-row bot\">...</div>"}
```

### 2.3 Frontend example

```javascript
const data = await sendLlmPrompt(userText, {
  endpoint: "/api/llm/output/generate/",   // specify since you have custom routing
  bubbleTemplate: `
    <div class="chat-row bot">
      <img class="avatar" src="/static/img/bot.png" alt="">
      <div class="bubble">{{content}}</div>
    </div>`,
});

if (data.type === "chat") {
  chatContainer.insertAdjacentHTML("beforeend", data.html);
  hydrateLlmCharts(chatContainer);   // instantiates Chart.js canvases
} else {
  chatContainer.insertAdjacentHTML("beforeend",
    `<div class="chat-row bot"><div class="bubble">
       📄 <a href="${data.download_url}" download>${data.filename}</a>
     </div></div>`);
}
```

`hydrateLlmCharts` must run after every insert — `insertAdjacentHTML` does not
execute scripts, so chart configs ship as inert JSON `<script>` tags and are
instantiated by the hydrator (this is also what makes the output XSS-safe).

### 2.4 curl smoke test

```bash
curl -X POST http://localhost:8000/api/llm/output/generate/ \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Make me a word document comparing Q1-Q4 sales with a chart"}'
```

---

## 4. The block schema (what the LLM returns)

Word, PDF and chat share one schema; each renderer supports every block:

| Block        | Fields                                                        |
|--------------|---------------------------------------------------------------|
| `heading`    | `level` (1–3), `text`                                          |
| `paragraph`  | `text`                                                         |
| `bullets`    | `items[]`                                                      |
| `numbered`   | `items[]`                                                      |
| `quote`      | `text`                                                         |
| `table`      | `title?`, `headers[]`, `rows[][]`                              |
| `chart`      | see below                                                      |
| `page_break` | (ignored in chat)                                              |

Chart block:

```json
{"type": "chart", "chart_type": "bar",       // bar | line | pie
 "title": "Revenue by quarter",
 "labels": ["Q1", "Q2"],
 "series": [{"name": "2024", "values": [10, 20]}],
 "show_values": true,                         // datalabels plugin
 "annotations": [                             // annotation plugin
   {"type": "line", "value": 15, "label": "Target"},
   {"type": "box", "y_min": 0, "y_max": 5, "label": "Danger"}
 ]}
```

Time-series variant (uses the date-fns adapter + zoom/pan in chat):

```json
{"type": "chart", "chart_type": "line", "time_unit": "day",
 "series": [{"name": "Revenue",
             "points": [{"x": "2024-01-01", "y": 120}]}]}
```

Excel uses its own schema (sheets with headers/rows plus **native, editable
openpyxl charts** referencing table columns) — see `EXCEL_PROMPT` in
`prompts.py`.

Charts render as: Chart.js canvases in chat, matplotlib PNGs embedded in
Word/PDF, and real Excel charts in xlsx.

---

## 5. Customisation

- **Colors**: edit `PALETTE` in `chartjs.py` (chat) and matplotlib defaults in
  `charts.py` (documents).
- **Default bubble**: `DEFAULT_BUBBLE` in `builders.py`.
- **Format detection keywords**: `_FORMAT_PATTERNS` in `prompts.py`.
- **Prompt style/tone**: edit the templates in `prompts.py` — keep the JSON
  shape descriptions intact or the builders won't parse the response.

## 6. Production notes

- Replace `@csrf_exempt` in `views.py` with real CSRF/auth.
- Serve `MEDIA_ROOT` via nginx/S3; add a cron/Celery job to purge old files
  in `media/llm_files/`.
- Using S3/cloud storage? Swap `_out_path` in `builders.py` for
  `django.core.files.storage.default_storage.save()`.
- All LLM text is HTML-escaped server-side and chart configs are inert JSON,
  so inserting the returned HTML is safe. If you change the markdown fallback
  to allow raw HTML, sanitise with `bleach`.
