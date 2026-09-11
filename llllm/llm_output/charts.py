"""Render chart blocks to PNG with matplotlib (used by Word and PDF).
Supports bar/line/pie, ISO-date time series, target-line and box
annotations, and on-bar value labels."""
import uuid
from datetime import date
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from django.conf import settings

OUTPUT_DIR = "llm_files"
ANNOT_COLOR = "#d32f2f"


def render_chart_png(block: dict) -> tuple[Path, str]:
    """Returns (absolute_path, media-relative path)."""
    labels = block.get("labels", [])
    series = block.get("series", [])
    ctype = block.get("chart_type", "bar")
    time_series = bool(series) and "points" in series[0] and ctype != "pie"

    fig, ax = plt.subplots(figsize=(7, 4), dpi=150)

    if ctype == "pie" and series:
        ax.pie(series[0].get("values", []), labels=labels,
               autopct="%1.1f%%", startangle=90)
    elif time_series:
        for s in series:
            xs = [date.fromisoformat(str(p["x"])[:10]) for p in s.get("points", [])]
            ys = [p["y"] for p in s.get("points", [])]
            ax.plot(xs, ys, marker="o", linewidth=2, label=s.get("name", ""))
        fig.autofmt_xdate()
        ax.grid(alpha=.3)
    elif ctype == "line":
        for s in series:
            ax.plot(labels, s.get("values", []), marker="o", linewidth=2,
                    label=s.get("name", ""))
        ax.grid(alpha=.3)
    else:  # grouped bar
        x = np.arange(len(labels))
        w = 0.8 / max(len(series), 1)
        for i, s in enumerate(series):
            ax.bar(x + i * w, s.get("values", []), w, label=s.get("name", ""))
        ax.set_xticks(x + w * (len(series) - 1) / 2)
        ax.set_xticklabels(labels)
        ax.grid(axis="y", alpha=.3)

    if ctype != "pie":
        for a in block.get("annotations", []):
            if a.get("type") == "box":
                ax.axhspan(a.get("y_min", 0), a.get("y_max", 0),
                           color=ANNOT_COLOR, alpha=.08)
            else:
                ax.axhline(a.get("value", 0), color=a.get("color", ANNOT_COLOR),
                           linestyle="--", linewidth=1.5)
                if a.get("label"):
                    ax.annotate(a["label"], xy=(0.99, a["value"]),
                                xycoords=("axes fraction", "data"),
                                ha="right", va="bottom", fontsize=8,
                                color=ANNOT_COLOR)
        if block.get("show_values") and ctype == "bar":
            for container in ax.containers:
                ax.bar_label(container, fontsize=8)

    if block.get("title"):
        ax.set_title(block["title"])
    if ctype != "pie" and sum(1 for s in series if s.get("name")) > 1:
        ax.legend()
    fig.tight_layout()

    rel = f"{OUTPUT_DIR}/chart_{uuid.uuid4().hex[:8]}.png"
    path = Path(settings.MEDIA_ROOT) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return path, rel
