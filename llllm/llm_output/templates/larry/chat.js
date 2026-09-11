/* llm_output chat helpers.
   Requires on the page (before this file):
   - chart.js
   - chartjs-adapter-date-fns (for time-series charts)
   - chartjs-plugin-zoom.min.js
   - chartjs-plugin-datalabels
   - chartjs-plugin-annotation.min.js
*/

/* One-time global registration (skip if your app already does this) */
if (window.ChartDataLabels) {
  Chart.register(ChartDataLabels);
  /* off by default so LLM charts never affect your existing charts */
  Chart.defaults.set("plugins.datalabels", { display: false });
}

/* Scan freshly inserted HTML for chart configs and instantiate Chart.js */
function hydrateLlmCharts(container) {
  container.querySelectorAll("script.llm-chart-config").forEach((tag) => {
    const canvas = document.getElementById(tag.dataset.canvas);
    if (!canvas || canvas.dataset.hydrated) return;
    const ta = document.createElement("textarea");
    ta.innerHTML = tag.textContent;            // un-escape server HTML-escaping
    const config = JSON.parse(ta.value);

    /* merge in function-based options JSON can't carry */
    const dl = config.options?.plugins?.datalabels;
    if (dl?.display) {
      Object.assign(dl, {
        anchor: "end", align: "top", clamp: true,
        font: { size: 10, weight: "600" },
        formatter: (v) => typeof v === "object"
          ? (+v.y).toLocaleString()
          : (+v).toLocaleString(),
      });
    }
    if (config.options?.plugins?.zoom) {
      config.options.plugins.zoom.limits = {
        x: { min: "original", max: "original" },
      };
    }

    canvas.dataset.hydrated = "1";
    new Chart(canvas, config);
    tag.remove();
  });
}

/* Example send helper */
async function sendLlmPrompt(promptText, { outputType, bubbleTemplate, endpoint } = {}) {
  const res = await fetch(endpoint || "/api/llm/output/generate/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: promptText,
      output_type: outputType,          // optional; auto-detected if omitted
      bubble_template: bubbleTemplate,  // optional; server default if omitted
    }),
  });
  return res.json();
}

/* Example usage:
const data = await sendLlmPrompt(userText, {
  endpoint: "/api/llm/output/generate/",
  bubbleTemplate: `
    <div class="chat-row bot">
      <div class="bubble">{{content}}</div>
    </div>`,
});
if (data.type === "chat") {
  chatContainer.insertAdjacentHTML("beforeend", data.html);
  hydrateLlmCharts(chatContainer);
} else {
  chatContainer.insertAdjacentHTML("beforeend",
    `<div class="chat-row bot"><div class="bubble">
       \u{1F4C4} <a href="${data.download_url}" download>${data.filename}</a>
     </div></div>`);
}
*/
