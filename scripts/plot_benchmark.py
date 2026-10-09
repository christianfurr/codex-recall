#!/usr/bin/env python3
"""Render benchmark evidence as charts and an offline, searchable report.

Optional plotting dependencies are isolated in requirements-benchmark.txt;
the memory server does not need them.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

GREEN = "#197b58"
BLUE = "#306ca2"
ORANGE = "#b96b21"
INK = "#163128"
MUTED = "#5f7069"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10,
        "axes.titlesize": 13, "axes.titleweight": "bold",
        "axes.labelcolor": MUTED, "text.color": INK, "xtick.color": MUTED,
        "ytick.color": MUTED, "axes.edgecolor": "#c5d3cc",
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.alpha": 0.2, "axes.axisbelow": True,
        "figure.facecolor": "#f5f9f6", "axes.facecolor": "#f5f9f6",
        "savefig.facecolor": "#f5f9f6", "svg.fonttype": "none",
    })


def label(group: str) -> str:
    return group.replace("_", " ").replace("paraphrase", "Paraphrase").capitalize()


def export(fig, directory: Path, name: str) -> None:
    fig.savefig(directory / (name + ".png"), dpi=170, bbox_inches="tight")
    svg = directory / (name + ".svg")
    fig.savefig(svg, bbox_inches="tight")
    svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n", encoding="utf-8")
    plt.close(fig)


def overview(metrics: dict, quality: dict, output: Path) -> None:
    scale = metrics["scale"]
    fig, axes = plt.subplots(2, 2, figsize=(13.5, 9.5), layout="constrained")
    fig.suptitle("Codex Recall · synthetic workload results", fontsize=20, fontweight="bold")
    ax = axes[0, 0]
    for field, text, color, marker in (("recall_p50_ms", "Median", GREEN, "o"), ("recall_p95_ms", "95th percentile", BLUE, "s"), ("recall_p99_ms", "99th percentile", ORANGE, "^")):
        values = [row[field] for row in scale]
        ax.plot([row["records"] for row in scale], values, color=color, label=text, marker=marker, linewidth=2)
        ax.annotate(f"{values[-1]:.2f}", (scale[-1]["records"], values[-1]), xytext=(5, 2), textcoords="offset points", fontsize=9, color=color)
    ax.set_xscale("log")
    ax.set_xticks([row["records"] for row in scale], [f"{row['records']:,}" for row in scale])
    ax.set_ylim(bottom=0)
    ax.margins(x=0.14)
    ax.set_title("Search latency as memory grows", loc="left", pad=14)
    ax.set_xlabel("Stored synthetic memories · log spacing")
    ax.set_ylabel("Engine recall latency (ms)")
    ax.legend(frameon=False, fontsize=9)

    ax = axes[0, 1]
    groups = quality["groups"]
    positions = list(range(len(groups)))
    ax.barh([n + 0.17 for n in positions], [row["hit_at_1"] * 100 for row in groups], height=0.31, color=BLUE, label="Target at rank 1")
    ax.barh([n - 0.17 for n in positions], [row["hit_at_5"] * 100 for row in groups], height=0.31, color=GREEN, label="Target in top 5")
    for n, row in zip(positions, groups):
        ax.text(min(row["hit_at_5"] * 100 + 1, 102), n - 0.17, f"{row['hit_at_5']:.0%}", va="center", fontsize=9)
    ax.set_yticks(positions, [f"{label(row['name'])} (n={row['query_count']})" for row in groups], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 116)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_title("Target retrieval by query type", loc="left", pad=14)
    ax.set_xlabel("Labelled queries with target found (%)")
    ax.legend(frameon=False, fontsize=9, loc="lower right")

    ax = axes[1, 0]
    sizes = [row["database_size_bytes"] / 1024**2 for row in scale]
    ax.plot([row["records"] for row in scale], sizes, color=GREEN, marker="o", linewidth=2)
    ax.fill_between([row["records"] for row in scale], sizes, alpha=0.09, color=GREEN)
    ax.annotate(f"{sizes[-1]:.2f} MiB", (scale[-1]["records"], sizes[-1]), xytext=(-10, 10), ha="right", textcoords="offset points")
    ax.set_ylim(bottom=0)
    ax.set_title("Database storage", loc="left", pad=14)
    ax.set_xlabel("Stored synthetic memories")
    ax.set_ylabel("SQLite database and live sidecars (MiB)")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))

    ax = axes[1, 1]
    rates = [row["write_ops_per_second"] for row in scale]
    bars = ax.bar([str(row["records"]) for row in scale], rates, color=GREEN, width=0.55)
    ax.bar_label(bars, labels=[f"{value:,.0f}" for value in rates], padding=5)
    ax.set_ylim(0, max(rates) * 1.2)
    ax.set_title("Loading the corpus through public writes", loc="left", pad=14)
    ax.set_xlabel("Memory count at checkpoint")
    ax.set_ylabel("New memory records per second")
    fig.supxlabel("Measured on one Linux machine · synthetic fixtures · warm cache · target hit rates are not general search accuracy", fontsize=9, color=MUTED)
    export(fig, output, "benchmark-overview")


def operation_latency(metrics: dict, raw: list[dict], output: Path) -> None:
    phases = {}
    for row in raw:
        if row["operation"] == "recall" and str(row.get("success", "true")).lower() in ("true", "1"):
            phases.setdefault(row["phase"], []).append(float(row["duration_ms"]))
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 4.8), layout="constrained")
    fig.suptitle("Latency distributions and concurrency", fontsize=19, fontweight="bold")
    ax = axes[0]
    colors = [GREEN, BLUE, ORANGE, "#8054a6", "#59798a"]
    plotted = 0
    for phase, values in phases.items():
        if len(values) < 10:
            continue
        values = sorted(values)
        cumulative = [(i + 1) / len(values) * 100 for i in range(len(values))]
        ax.step(values, cumulative, where="post", label=f"{phase.replace('_', ' ')} (n={len(values):,})", color=colors[plotted % len(colors)], linewidth=1.8)
        plotted += 1
    ax.set_xscale("log")
    ax.set_ylim(0, 101)
    ax.set_xlabel("Recall latency (ms) · log spacing")
    ax.set_ylabel("Requests completed at or below latency (%)")
    ax.set_title("Observed recall distributions", loc="left", pad=12)
    ax.legend(frameon=False, fontsize=8, loc="lower right")

    ax = axes[1]
    operations = metrics["concurrency"]["summary_by_operation"]
    names = list(operations)
    positions = list(range(len(names)))
    p50 = [operations[name]["p50_ms"] for name in names]
    p95 = [operations[name]["p95_ms"] for name in names]
    ax.barh([i + 0.18 for i in positions], p50, height=0.33, color=GREEN, label="Median")
    ax.barh([i - 0.18 for i in positions], p95, height=0.33, color=BLUE, label="95th percentile")
    ax.set_yticks(positions, [name.replace("_", " ") for name in names])
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlabel("Operation latency under contention (ms) · log spacing")
    ax.set_title(f"{metrics['concurrency']['processes']} concurrent worker processes", loc="left", pad=12)
    ax.legend(frameon=False, fontsize=9)
    fig.supxlabel("Operation mixes and query selectivity differ between phases; these curves are not a pure transport-overhead comparison.", fontsize=9, color=MUTED)
    export(fig, output, "benchmark-latency")


def dataset_charts(metrics: dict, output: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 4.7), layout="constrained")
    fig.suptitle("Generated memory corpus", fontsize=19, fontweight="bold")
    for ax, key, title in zip(axes, ("scopes", "categories"), ("Scope distribution", "Category distribution")):
        values = metrics["corpus"][key]
        names = list(values)
        bars = ax.barh([name.replace("_", " ") for name in names], [values[name] for name in names], color=GREEN)
        ax.bar_label(bars, labels=[f"{values[name]:,}" for name in names], padding=4, fontsize=9)
        ax.invert_yaxis()
        ax.set_xlim(0, max(values.values()) * 1.22)
        ax.set_xlabel("Synthetic memories")
        ax.set_title(title, loc="left", pad=12)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
    export(fig, output, "benchmark-corpus")


def build_dashboard(metrics: dict, quality: dict, rows: list[dict], output: Path) -> None:
    figures = "\n".join("<svg" + (output / (name + ".svg")).read_text().split("<svg", 1)[1] for name in ("benchmark-overview", "benchmark-latency", "benchmark-corpus"))
    # All query text is inserted through textContent, never executable markup.
    safe_rows = json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    mcp = metrics["mcp"]
    last = metrics["scale"][-1]
    concurrency = metrics["concurrency"]
    stats = [
        ("Generated memories", f"{metrics['corpus']['count']:,}"),
        ("Engine recall median", f"{last['recall_p50_ms']:.2f} ms"),
        ("MCP recall median", f"{mcp['recall_p50_ms']:.2f} ms"),
        ("Concurrent operations", f"{concurrency['operations']:,}"),
    ]
    tiles = "".join(f'<div class="stat"><span>{html.escape(name)}</span><strong>{html.escape(value)}</strong></div>' for name, value in stats)
    groups = sorted({row["group"] for row in rows})
    options = "".join(f'<option value="{html.escape(group, quote=True)}">{html.escape(label(group))}</option>' for group in groups)
    dashboard = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; font-src 'none'">
<title>Codex Recall · benchmark report</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f9f6;color:#163128;font:15px/1.55 system-ui,sans-serif}}main{{max-width:1260px;margin:auto;padding:40px 24px}}h1{{font-size:34px;line-height:1.2;margin:8px 0}}h2{{margin-top:32px}}.eyebrow{{letter-spacing:.16em;color:#197b58;font-weight:600}}.muted{{color:#5f7069}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}}.stat{{background:#e9f1eb;padding:18px;border-radius:10px}}.stat span{{display:block;color:#5f7069;font-size:13px}}.stat strong{{display:block;font-size:25px;margin-top:5px}}svg{{width:100%;height:auto;display:block;margin:20px 0}}.controls{{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0}}label{{display:flex;gap:8px;align-items:center}}input,select{{font:inherit;padding:8px;border:1px solid #c5d3cc;border-radius:6px;background:#fff;max-width:100%}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #dce5df;padding:12px 8px;vertical-align:top}}th{{color:#5f7069;font-size:13px}}td.num{{white-space:nowrap;font-variant-numeric:tabular-nums}}td.query{{width:47%;overflow-wrap:anywhere}}.result{{font-weight:600}}.result.miss{{color:#955225}}details{{margin:24px 0}}summary{{cursor:pointer}}.table-wrap{{overflow-x:auto}}a{{color:#197b58}}@media(max-width:650px){{main{{padding:24px 12px}}h1{{font-size:27px}}.stats{{grid-template-columns:repeat(2,1fr)}}label{{width:100%;flex-wrap:wrap}}}}
</style></head><body><main>
<div class="eyebrow">CODEX RECALL · MEASURED LOCALLY</div><h1>Memory under load</h1>
<p class="muted">Synthetic memories, labelled retrieval cases and real MCP stdio calls. These measurements describe this workload on one machine, not general search accuracy.</p>
<div class="stats">{tiles}</div>
{figures}
<h2>Inspect the labelled search cases</h2>
<p class="muted">Expand a query to compare its designated target with the actual returned memories.</p>
<div class="controls"><label>Query type <select id="group"><option value="">All query types</option>{options}</select></label><label>Find a query <input id="search" type="search" placeholder="Bun, authentication, deployment…"></label><label><input type="checkbox" id="misses"> Show top-5 misses only</label></div>
<p id="count" class="muted" aria-live="polite"></p>
<div class="table-wrap"><table><thead><tr><th>Query</th><th>Query type</th><th>Target rank</th><th>Latency</th></tr></thead><tbody id="cases"></tbody></table></div>
<details><summary>What these scores mean</summary><p>Hit at rank 1 and in the top 5 ask whether a designated relevant target was retrieved. Each query has predeclared target facts; this evaluation does not label all returned memories. It cannot establish exhaustive recall or precision. Queries reuse some target facts and are not independent samples of real conversations.</p><p>All memories are generated fixtures. New processes can still benefit from the operating system's warm disk cache. Each write uses the real public transactional API. The memory database used by the normal Codex connection is separate.</p></details>
<p class="muted">Raw metrics, latency observations, corpus and query labels are saved beside this report. Reproduction commands are in the repository's benchmark methodology guide.</p>
</main><script>
const rows={safe_rows};
const group=document.getElementById('group'),search=document.getElementById('search'),misses=document.getElementById('misses'),body=document.getElementById('cases'),count=document.getElementById('count');
function paragraph(parent,title,value){{const p=document.createElement('p'),strong=document.createElement('strong');strong.textContent=title+': ';p.append(strong,document.createTextNode(value));parent.append(p);}}
function render(){{const selected=rows.filter(r=>(!group.value||r.group===group.value)&&(!search.value||r.query.toLowerCase().includes(search.value.toLowerCase()))&&(!misses.checked||!Number(r.hit_at_5)));body.replaceChildren();for(const r of selected){{const tr=document.createElement('tr');for(const [key,value] of [['query',r.query],['group',r.group.replaceAll('_',' ')],['rank',r.first_relevant_rank||'Not in top 5'],['latency',Number(r.latency_ms).toFixed(2)+' ms']]){{const td=document.createElement('td');if(key==='query'){{td.className='query';const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent=value;details.append(summary);paragraph(details,'Filters',Object.entries(r.filters).map(([k,v])=>k+' = '+v).join(', ')||'None');for(const target of r.targets)paragraph(details,'Target',target);paragraph(details,'Returned',r.returned.length+' memories');const ol=document.createElement('ol');for(const result of r.returned){{const li=document.createElement('li');li.textContent=(result.target?'[TARGET] ':'')+result.content+' ('+result.scope+(result.project?' / '+result.project:'')+')';ol.append(li)}}details.append(ol);paragraph(details,'Case notes',r.notes);td.append(details)}}else td.textContent=value;if(key==='rank')td.className='result'+(r.first_relevant_rank?'':' miss');if(key==='latency')td.className='num';tr.append(td)}}body.append(tr)}}count.textContent=selected.length+' of '+rows.length+' labelled queries';}}
for(const control of [group,search,misses])control.addEventListener('input',render);render();
</script></body></html>'''
    (output / "benchmark-dashboard.html").write_text(dashboard, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("verification/benchmark/latest"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    directory = args.input.resolve()
    output = (args.output or directory / "charts").resolve()
    output.mkdir(parents=True, exist_ok=True)
    metrics = read_json(directory / "metrics.json")
    quality = read_json(directory / "quality.json")
    with (directory / "latency.csv").open(newline="", encoding="utf-8") as stream:
        latency = list(csv.DictReader(stream))
    with (directory / "corpus.jsonl").open(encoding="utf-8") as stream:
        corpus = {record["id"]: record for record in map(json.loads, stream)}
    rows = []
    for case in quality["query_results"]:
        row = dict(case)
        row["targets"] = [corpus[record_id]["content"].strip() for record_id in case["expected_ids"]]
        row["returned"] = [
            {"content": corpus[record_id]["content"].strip()[:320],
             "scope": corpus[record_id]["scope"], "project": corpus[record_id]["project"],
             "target": record_id in case["expected_ids"]}
            for record_id in case["returned_ids"]
        ]
        rows.append(row)
    style()
    overview(metrics, quality, output)
    operation_latency(metrics, latency, output)
    dataset_charts(metrics, output)
    build_dashboard(metrics, quality, rows, output)
    print(f"Charts and offline report generated: {output}")


if __name__ == "__main__":
    main()
