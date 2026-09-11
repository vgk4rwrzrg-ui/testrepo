# AI Agent Core — User Guide

How to use Larry, the AI semantic search bot: chatting, searching tables,
generating big reports, and exporting **Word, PDF, Excel and PowerPoint**
documents with your own corporate theme.

> Administrator setup (install, proxy, Kubernetes, access policies) is in
> [`README.md`](README.md). This guide is for day-to-day use.

---

## 1. Opening the chat

Click the robot icon (Little Larry — he winks). The chat window opens where
your administrator configured it: docked left, docked right, or as a
centered popup. Click the robot again, the ✕, or the backdrop to close.
The window automatically matches your site/OS **dark or light mode**.

## 2. Asking questions

Type a question about your data and press **Send**:

> *find open requisitions for program Alpha*
> *how many incidents are still active?*

Larry searches only the tables **your** groups are allowed to see. Matching
records appear as small cards under his reply. If you get *"No policy
grants your groups access…"*, ask an administrator to map your group in
**Admin → AI Agent Core → Table access policies**.

Some fields may be hidden from you on purpose — administrators can open
specific fields to specific groups only.

## 3. Choosing the answer format (the dropdown)

Left of the message box is the **format dropdown**:

| Option | What you get |
|---|---|
| 💬 Chat | Normal chat answer (default) |
| 📄 Word | A themed `.docx` document |
| 📕 PDF | A themed `.pdf` document |
| 📊 Excel | A themed `.xlsx` workbook (one sheet per data table) |
| 📽 PowerPoint | A themed `.pptx` deck (title slide, agenda, one slide per section, data tables) |

Pick a format, ask your question, and a **"Document ready"** card appears
with a download link. Documents are private to you. You still get the chat
reply as well.

## 4. Big reports — written bit by bit

Ask for a report in plain language:

> *generate a big report on staffing with 8 chapters*
> */report incident trends in 2024*

Larry writes large reports **chapter by chapter** so they never get cut
off. You'll see live progress in the chat:

> Outline ready — 8 chapters planned. Writing chapter 1/8: Executive Summary…
> Finished chapter 1/8 (412 words). Writing chapter 2/8: Introduction & Scope…
> Report complete — 8 chapters, 3,510 words. Ready to download.

When it finishes you get download buttons for **Markdown, Word, PDF, Excel
and PowerPoint** — all rendered in the active document theme. Say
*"with N chapters"* to control the length. If the connection drops, send
the request again to resume.

## 5. Custom document themes (admins)

**Admin → AI Agent Core → Document themes.** A theme defines:

* **Colors** (hex): primary (titles, headings, table headers), secondary
  (subheadings, footers), accent (PowerPoint bars/highlights), text, and
  slide background.
* **Fonts**: heading font and body font (e.g. Georgia / Verdana).
* **Footer text**: e.g. *"Acme Corp — Confidential"*, shown in document
  footers and on every slide.

Mark one theme **default** and every Word/PDF/Excel/PowerPoint file — chat
documents and report downloads alike — is styled with it. Change the theme
once, and every future document follows. No code involved.

## 6. Configuring the bot (admins)

**Admin → AI Agent Core → Bot profiles**: name, greeting, personality,
avatar (animated robot by default; emoji or image optional), accent color,
window position (left/right/popup), dark-mode behaviour, and whether result
cards are shown.

**Searchable tables**: register any project model and its fields; restrict
individual fields to specific groups. **Table access policies**: map tables
to Django groups (read / aggregate-only / deny, priorities, field subsets,
row caps). Every search the bot performs is logged under **Table access
audits**; conversations under **Bot conversations**; report jobs (with
per-chapter status) under **Report jobs**; files under **Generated
documents**.

## 7. Embedding the widget (developers)

```django
{% load ai_agent_tags %}
{% ai_bot_widget %}                 {# floating robot, bottom corner #}

<div style="width:120px;height:130px">
  {% ai_bot_widget inline=True %}   {# robot fills this div, any size #}
</div>
```

Include once per page. Demo pages: `…/widget-demo/` and
`…/widget-demo/?inline=1`.

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "No policy grants your groups access" | Ask an admin to add your group to a Table access policy |
| A field you expect is missing from results | The field is group-restricted; ask an admin |
| Document link 404s for a colleague | Documents are owner-private by design — they generate their own |
| "…is not installed" when exporting | Server needs `pip install python-docx reportlab openpyxl python-pptx` |
| Report stalls | Send the same report request again — it resumes chapter by chapter |
| Bot icon missing | No active Bot profile exists, or the page doesn't include the widget tag |
