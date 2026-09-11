"""Convert a chart block into a Chart.js config dict (plugins: datalabels,
annotation, zoom, chartjs-adapter-date-fns time scale)."""
import json, uuid
from django.utils.html import escape

PALETTE = ["#2f5496", "#e8702a", "#4caf50", "#9c27b0", "#00acc1", "#fdd835"]
ANNOT_COLOR = "#d32f2f"


def _color(i: int) -> str:
    return PALETTE[i % len(PALETTE)]


def _is_time_series(block: dict) -> bool:
    series = block.get("series", [])
    return bool(series) and "points" in series[0]


def _annotations(block: dict) -> dict:
    out = {}
    for i, a in enumerate(block.get("annotations", [])):
        key = f"a{i}"
        label = a.get("label", "")
        if a.get("type") == "box":
            out[key] = {
                "type": "box",
                "yMin": a.get("y_min"), "yMax": a.get("y_max"),
                "backgroundColor": "rgba(211, 47, 47, 0.08)",
                "borderColor": "rgba(211, 47, 47, 0.4)",
                "borderWidth": 1,
                "label": {"display": bool(label), "content": label,
                          "position": {"x": "start", "y": "start"},
                          "color": ANNOT_COLOR, "font": {"size": 10}},
            }
        else:  # horizontal line
            out[key] = {
                "type": "line",
                "scaleID": "y",
                "value": a.get("value"),
                "borderColor": a.get("color", ANNOT_COLOR),
                "borderWidth": 2,
                "borderDash": [6, 4],
                "label": {"display": bool(label), "content": label,
                          "position": "end", "backgroundColor": ANNOT_COLOR,
                          "color": "#fff", "font": {"size": 10}},
            }
    return out


def chart_block_to_config(block: dict) -> dict:
    ctype = block.get("chart_type", "bar")
    series = block.get("series", [])
    time_series = _is_time_series(block) and ctype != "pie"

    if ctype == "pie":
        values = series[0].get("values", []) if series else []
        datasets = [{"data": values,
                     "backgroundColor": [_color(i) for i in range(len(values))]}]
        data = {"labels": block.get("labels", []), "datasets": datasets}
    else:
        datasets = []
        for i, s in enumerate(series):
            datasets.append({
                "label": s.get("name", f"Series {i + 1}"),
                "data": s["points"] if time_series else s.get("values", []),
                "backgroundColor": _color(i) + ("80" if ctype == "line" else ""),
                "borderColor": _color(i),
                "borderWidth": 2,
                "tension": 0.3,
                "fill": False,
            })
        data = {"datasets": datasets}
        if not time_series:
            data["labels"] = block.get("labels", [])

    plugins = {
        "title": {"display": bool(block.get("title")),
                  "text": block.get("title", "")},
        "legend": {"display": ctype == "pie" or len(series) > 1},
        # datalabels is registered globally client-side, so explicitly
        # switch it off unless the block asks for values on the chart
        "datalabels": {"display": bool(block.get("show_values"))},
    }
    annots = _annotations(block)
    if annots:
        plugins["annotation"] = {"annotations": annots}
    if time_series:
        plugins["zoom"] = {
            "pan": {"enabled": True, "mode": "x"},
            "zoom": {"wheel": {"enabled": True},
                     "pinch": {"enabled": True}, "mode": "x"},
        }

    config = {
        "type": ctype,
        "data": data,
        "options": {
            "responsive": True,
            "maintainAspectRatio": False,
            "plugins": plugins,
        },
    }
    if ctype != "pie":
        scales = {"y": {"beginAtZero": True}}
        if time_series:
            scales["x"] = {"type": "time",
                           "time": {"unit": block.get("time_unit", "day")}}
        config["options"]["scales"] = scales
    return config


def chart_block_to_html(block: dict) -> str:
    """Emit a canvas plus its config in a JSON script tag (XSS-safe)."""
    cid = f"llm-chart-{uuid.uuid4().hex[:8]}"
    config_json = escape(json.dumps(chart_block_to_config(block)))
    return (
        f'<div class="llm-chart-wrap"><canvas id="{cid}"></canvas></div>'
        f'<script type="application/json" class="llm-chart-config" '
        f'data-canvas="{cid}">{config_json}</script>'
    )
