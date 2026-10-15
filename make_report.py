#!/usr/bin/env python3
"""make_report.py — generate a self-contained HTML comparison report for
disco results on disk.

Usage:
    python3 make_report.py [--results <dir>] [--out <file>]

Reads every <results>/<storageclass-label>/run-*/ directory (newest run per
label), extracts per-phase fio JSON metrics, and renders a comparison report:
summary cards, per-phase throughput bars, headroom badges, latency table and
auto-generated insights. Regenerate any time new runs are exported.
"""
import argparse
import datetime
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score import (DEMAND_1X, DEMAND_SOURCES, REF_COMPUTE_H,  # noqa: E402
                   VOLUMES, WEIGHTS, phase_metrics)

PHASES = [
    ("00-ingest-write", "Staging ingest", "sequential write, network-rate analog"),
    ("02-reorder", "Reorder copy", "concurrent seq read + same-size write"),
    ("03-major-cycles", "Major-cycle re-reads", "sequential read, byte-dominant"),
    ("04-column-rmw", "Column RMW", "chunked read+write (MS column add)"),
    ("06-imagechain-gen1", "Image chain", "many-file 8-way parallel R/W"),
    ("08-carta-tiles", "CARTA tiles", "random 256K read, latency-bound"),
    ("09-qa-sparse", "Sparse QA", "random 8K read, single in-flight"),
    ("10-smallfiles", "Small-file fan-out", "2000 files + catalogues"),
]
PALETTE = ["#0ea5e9", "#f59e0b", "#8b5cf6", "#10b981", "#ef4444", "#6366f1"]


def collect(results_dir):
    """Return {label: {phase: {thr_mbs, iops, p99, gb, reps}}} from newest
    run per label. Pools repetition files ({phase}-N.json), falling back to
    single {phase}.json from runs before REPEATS existed."""
    from score import phase_files, phase_metrics_aggregate
    classes = {}
    for label_dir in sorted(glob.glob(os.path.join(results_dir, "*"))):
        if not os.path.isdir(label_dir):
            continue
        label = os.path.basename(label_dir)
        runs = sorted(glob.glob(os.path.join(label_dir, "run-*")),
                      key=lambda p: os.path.basename(p))
        if not runs:
            continue
        run = runs[-1]
        logs = os.path.join(run, "logs")
        data = {}
        for phase, _, _ in PHASES:
            files = phase_files(logs, phase)
            if not files:
                continue
            rd, wr, iops, rt, p99, nreps = phase_metrics_aggregate(files)
            if not rt:
                continue
            data[phase] = {
                "thr": (rd + wr) / rt / 1e6,
                "iops": iops,
                "p99": p99,
                "gb": (rd + wr) / 1e9,
                "reps": nreps,
                "run": os.path.basename(run),
            }
        if data:
            classes[label] = data
    return classes


def composite(data):
    total_w = 0.0
    acc = 0.0
    for phase, w in WEIGHTS["dsc-065"].items():
        if phase in data:
            total_w += w
            acc += w * data[phase]["thr"]
    return acc / total_w if total_w else 0.0


def geomean_headroom(data):
    # SCORE: byte-weighted geometric mean of headroom over capability phases.
    # Phases are slices of one workload -> importance = byte share (WEIGHTS).
    # CARTA excluded: rate-capped, its ratio measures the cap, not capability.
    num, den = 0.0, 0.0
    for p, d in data.items():
        if p == "08-carta-tiles":
            continue
        w = WEIGHTS["dsc-065"][p]
        num += w * math.log(d["thr"] / DEMAND_1X[p])
        den += w
    return math.exp(num / den) if den else 0.0


def io_pressure(data):
    """I/O time as a share of the reference run's compute budget."""
    return predicted_hours(data) / REF_COMPUTE_H


def predicted_hours(data):
    total = 0.0
    for p, d in data.items():
        vol = VOLUMES["dsc-065"].get(p, 0)
        total += vol * 1024 / d["thr"] if d["thr"] else float("inf")
    return total / 3600.0


def badge(headroom):
    if headroom is None:
        return '<span class="badge none">n/a</span>'
    if headroom >= 2:
        css = "good"
    elif headroom >= 1:
        css = "ok"
    else:
        css = "bad"
    return f'<span class="badge {css}">{headroom:.1f}×</span>'


def bar(thr, best, color):
    if not best:
        return ""
    w = max(thr / best * 100.0, 0.7)
    return (f'<div class="barwrap"><div class="bar" style="width:{w:.1f}%;'
            f'background:{color}"></div></div>')


def render(classes, out, results_dir):
    labels = sorted(classes)
    colors = {l: PALETTE[i % len(PALETTE)] for i, l in enumerate(labels)}
    nclasses = len(labels)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # ---------- summary cards ----------
    cards = ""
    for l in labels:
        d = classes[l]
        run_phases = len(d)
        heads = {p: d[p]["thr"] / DEMAND_1X[p] for p in d}
        below = [p for p, h in heads.items() if h < 1]
        weakest = min(heads, key=heads.get) if heads else None
        flag = (f'<div class="flag bad">⚠ {len(below)} phase(s) below 1× demand</div>'
                if below else '<div class="flag good">✓ all phases ≥ 1× demand</div>')
        cards += f"""
        <div class="card">
          <h3 style="border-color:{colors[l]}">{l}</h3>
          <div class="big">{geomean_headroom(d):.1f}<span class="unit">×</span></div>
          <div class="sub">score — byte-weighted geometric mean of per-phase headroom</div>
          <div class="row">I/O pressure <b>{io_pressure(d):.2f}</b> — I/O
          time as a share of the reference run's {REF_COMPUTE_H} h compute time</div>
          <div class="row">I/O time (storage-bound) {predicted_hours(d):.1f} h ·
          composite {composite(d):,.0f} MB/s</div>
          <div class="row">{run_phases}/{len(PHASES)} phases run</div>
          {flag}
          <div class="row sub">weakest: {weakest.replace('-gen1','') if weakest else '—'
              } ({heads[weakest]:.1f}×)</div>
        </div>"""

    # ---------- per-phase sections ----------
    sections = ""
    for phase, nice, desc in PHASES:
        w = WEIGHTS["dsc-065"].get(phase)
        if w:
            extra = " · not counted in the score" if phase == "08-carta-tiles" else ""
            wbadge = (f'<span class="wgt">{w*100:.0f}% of the I/O{extra}</span>')
        else:
            wbadge = ""
        rows = ""
        present = [l for l in labels if phase in classes[l]]
        best = max((classes[l][phase]["thr"] for l in present), default=0)
        for l in labels:
            d = classes[l].get(phase)
            if not d:
                rows += (f'<tr><td class="lbl" style="color:{colors[l]}">{l}</td>'
                         '<td colspan="5" class="none">not run</td></tr>')
                continue
            h = d["thr"] / DEMAND_1X[phase]
            p99v = d["p99"]
            if p99v is None:
                p99 = "—"
            else:
                cls = "good" if p99v < 10 else ("ok" if p99v < 50 else "bad")
                p99 = f'<span class="badge {cls}">{p99v:.1f} ms</span>' 
            rep_note = f' <span class="desc">({d["reps"]} reps)</span>' if d.get("reps", 1) > 1 else ""
            rows += f"""
            <tr>
              <td class="lbl" style="color:{colors[l]}">{l}{rep_note}</td>
              <td>{bar(d['thr'], best, colors[l])}</td>
              <td class="num"><b>{d['thr']:,.0f}</b> MB/s</td>
              <td class="num">{d['iops']:,.0f}</td>
              <td class="num">{p99}</td>
              <td>{badge(h)}</td>
            </tr>"""
        sections += f"""
        <div class="phase">
          <h2>{nice} <span class="desc">— {desc}</span>{wbadge}</h2>
          <table>
            <tr><th style="width:22%">storage class</th><th style="width:34%"></th>
                <th>throughput</th><th>IOPS</th><th>p99 ms</th><th>headroom</th></tr>
            {rows}
          </table>
        </div>"""

    # ---------- composite chart ----------
    comp_rows = ""
    best_comp = max((composite(classes[l]) for l in labels), default=1)
    for l in labels:
        c = composite(classes[l])
        comp_rows += f"""
        <tr><td class="lbl" style="color:{colors[l]}">{l}</td>
            <td>{bar(c, best_comp, colors[l])}</td>
            <td class="num"><b>{c:,.0f}</b> MB/s</td></tr>"""

    # ---------- insights ----------
    insights = []
    for l in labels:
        heads = {p: classes[l][p]["thr"] / DEMAND_1X[p] for p in classes[l]}
        below = sorted((p, h) for p, h in heads.items() if h < 1)
        if below:
            worst = min(below, key=lambda x: x[1])
            insights.append(f'<b style="color:{colors[l]}">{l}</b>: '
                             f'{len(below)} phase(s) below 1× demand — worst '
                             f'<b>{worst[0].replace("-gen1","")}</b> at '
                             f'<b>{worst[1]:.2f}×</b> (real DSC would run slower '
                             f'than the SWE-SRC reference in that phase).')
    if nclasses >= 2 and all(len(classes[l]) >= 6 for l in labels):
        spreads = []
        for phase, _, _ in PHASES:
            thrs = {l: classes[l][phase]["thr"] for l in labels if phase in classes[l]}
            if len(thrs) >= 2 and min(thrs.values()) > 0:
                spreads.append((max(thrs.values()) / min(thrs.values()), phase, thrs))
        spreads.sort(reverse=True)
        for ratio, phase, thrs in spreads[:2]:
            best_l = max(thrs, key=thrs.get)
            worst_l = min(thrs, key=thrs.get)
            insights.append(
                f'largest divergence: <b>{phase.replace("-gen1","")}</b> — '
                f'<b style="color:{colors[best_l]}">{best_l}</b> is '
                f'<b>{ratio:.0f}×</b> faster than '
                f'<b style="color:{colors[worst_l]}">{worst_l}</b> '
                f'({thrs[best_l]:,.0f} vs {thrs[worst_l]:,.0f} MB/s).')
    if nclasses >= 2:
        for phase, _, _ in PHASES:
            if phase not in ("06-imagechain-gen1", "10-smallfiles"):
                continue
            thrs = {l: classes[l][phase]["thr"] for l in labels if phase in classes[l]}
            if len(thrs) >= 2 and max(thrs.values()) / max(min(thrs.values()), 0.01) < 1.5:
                insights.append(
                    f'<b>{phase.replace("-gen1", "")}</b>: near-identical throughput across '
                    f'classes (~{max(thrs.values()):,.0f} MB/s) — working set fits the node '
                    f'page cache at score scale; these numbers reflect RAM, not storage. '
                    f'Increase scale (or drop caches) for a cold-cache reading.')
    lats = {l: {p: classes[l][p]["p99"] for p in classes[l] if classes[l][p]["p99"]}
            for l in labels}
    if any(lats.values()):
        parts = []
        for l, m in lats.items():
            if m:
                parts.append(f'<b style="color:{colors[l]}">{l}</b>: '
                             + ", ".join(f'{p.replace("-gen1","")} {v:.1f} ms'
                                         for p, v in sorted(m.items())))
        insights.append("read-latency tails (p99): " + "; ".join(parts) + ".")
    insights_html = "".join(f"<li>{i}</li>" for i in insights) or "<li>insufficient data</li>"

    ref_rows = ""
    for phase, _, _ in PHASES:
        s = DEMAND_SOURCES[phase]
        link = (f'<a href="{s["url"]}">confluence</a>' if s.get("url") else "")
        src = s["source"] + ((" · " + link) if link else "")
        ref_rows += (f'<tr><td>{phase.replace("-gen1","")}</td>'
                     f'<td class="num">{DEMAND_1X[phase]} MB/s</td>'
                     f'<td>{s["kind"]}</td><td class="srcd">{src}</td></tr>\n')

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>disco — storage class comparison</title>
<style>
  :root {{ --bg:#0f172a; --panel:#1e293b; --ink:#e2e8f0; --mut:#94a3b8; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:1000px; margin:0 auto; padding:32px 24px 64px; }}
  h1 {{ font-size:26px; margin:0 0 4px; }}
  .meta {{ color:var(--mut); margin-bottom:28px; font-size:13px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
            gap:16px; margin-bottom:28px; }}
  .card {{ background:var(--panel); border-radius:12px; padding:18px 20px; }}
  .card h3 {{ margin:0 0 10px; font-size:14px; font-weight:600; padding-left:10px;
             border-left:4px solid; }}
  .big {{ font-size:34px; font-weight:700; }}
  .unit {{ font-size:14px; font-weight:400; color:var(--mut); margin-left:4px; }}
  .sub {{ color:var(--mut); font-size:12px; }}
  .row {{ margin-top:6px; font-size:13px; }}
  .flag {{ margin-top:10px; font-size:13px; font-weight:600; padding:4px 8px;
          border-radius:6px; display:inline-block; }}
  .flag.good {{ background:rgba(16,185,129,.15); color:#34d399; }}
  .flag.bad {{ background:rgba(239,68,68,.15); color:#f87171; }}
  .phase {{ background:var(--panel); border-radius:12px; padding:18px 20px;
           margin-bottom:18px; }}
  .phase h2 {{ font-size:16px; margin:0 0 12px; }}
  .desc {{ color:var(--mut); font-weight:400; font-size:13px; }}
  .wgt {{ float:right; font-size:12px; font-weight:400; color:var(--mut);
         border:1px solid #334155; border-radius:10px; padding:2px 10px; }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{ text-align:left; color:var(--mut); font-size:11px; text-transform:uppercase;
       letter-spacing:.06em; padding:4px 8px; border-bottom:1px solid #334155; }}
  td {{ padding:7px 8px; vertical-align:middle; font-size:14px; }}
  td.num {{ white-space:nowrap; text-align:right; font-variant-numeric:tabular-nums; }}
  td.lbl {{ font-weight:600; white-space:nowrap; }}
  .barwrap {{ background:#334155; border-radius:4px; height:14px;
              min-width:60px; }}
  .bar {{ height:100%; border-radius:4px; }}
  .badge {{ font-weight:700; font-size:12px; padding:3px 9px; border-radius:20px;
           white-space:nowrap; }}
  .badge.good {{ background:rgba(16,185,129,.15); color:#34d399; }}
  .badge.ok {{ background:rgba(245,158,11,.15); color:#fbbf24; }}
  .badge.bad {{ background:rgba(239,68,68,.15); color:#f87171; }}
  .badge.none {{ background:#334155; color:var(--mut); }}
  .none {{ color:var(--mut); }}
  h2.comp {{ margin:30px 0 10px; }}
  ul {{ margin:0; padding-left:20px; }}
  li {{ margin-bottom:8px; }}
  .footer {{ color:var(--mut); font-size:12px; margin-top:30px; }}
  table.src {{ margin-top:10px; font-size:12px; }}
  table.src td {{ padding:4px 8px; vertical-align:top; }}
  table.src td.srcd {{ color:var(--mut); max-width:520px; }}
  a {{ color:#7dd3fc; }}
</style></head><body><div class="wrap">
<h1>disco — storage class comparison</h1>
<div class="meta">generated {now} · newest run per class ·
score scenario (8 phases, uncapped) ·
<details style="margin-top:8px"><summary style="cursor:pointer">
<b>What is the 1× reference?</b></summary>
<p>The <b>1× line</b> is the rate at which the <b>real DSC workloads demanded
I/O</b>, as <b>measured on SWE-SRC</b> (psrecord traces) or <b>estimated</b>
where no direct measurement exists. Headroom = this storage's measured
throughput ÷ that reference: <b>&gt;1× keeps up with margin</b>;
<b>&lt;1× means a real DSC would run slower than the SWE-SRC reference in
that phase</b>.</p>
<p>Which workload? The reference comes chiefly from the <b>DSC-065
(DEEP_FIELD) imaging run</b> — the heaviest workload with complete
measurements — with one rate from the DSC-008 run and two from staging
measurements (per-phase sources below). Other DSC families share these I/O
patterns in different proportions, so the per-phase results stay valid for
them; only the weighting of the score would change.</p>
<p><b>Caveat:</b> SWE-SRC's own rates were partly storage-latency-bound
(CPU was ~60% while reading; the same storage burst to 261–574 MB/s), so 1×
means "the reference experience", not a fundamental requirement.</p>
<table class="src"><tr><th>phase</th><th>reference</th><th>kind</th>
<th>source</th></tr>{ref_rows}</table>
</details>
<details style="margin-top:8px"><summary style="cursor:pointer">
<b>What is headroom?</b></summary>
<p>Each row of the table carries a <b>headroom</b> badge (e.g. 5.5&times;).
It answers: <b>how many times over could this storage handle what the real
workload needs for that step?</b> It is the measured throughput divided by
the 1&times; reference demand.</p>
<p>2&times; = twice as fast as needed; 1&times; = exactly enough;
<b>below 1&times; (red badge) = too slow for that step</b> — the real
workload would be held back by the storage there.</p>
<p>Headroom is shown per step because storage classes make different
trade-offs — one can be fast for large sequential transfers but slow for
small random ones, and a single average would hide that.</p>
</details>
<details style="margin-top:8px"><summary style="cursor:pointer">
<b>What is the score?</b></summary>
<p>The headline score is the <b>byte-weighted geometric mean of the
per-phase headroom</b>: score = exp(&Sigma; w&#8346; &middot; ln(headroom&#8346;)),
where headroom&#8346; is the measured throughput &divide; the 1&times; reference
demand, and w&#8346; is each phase's share of the workload's bytes.</p>
<p><b>Why a geometric mean:</b> headroom values are ratios — a phase at
2&times; and a phase at 0.5&times; average out to 1&times; (neutral), and a
phase below 1&times; pulls the score down no matter how fast the others
are. <b>Why byte-weighted:</b> the phases are parts of one workload, so each
counts according to how much data it moves — a 10&times; difference on a
phase that carries 1% of the data counts for 1%, not as much as the phase
that carries 40%.</p>
<p>Reading it: <b>1&times; = the workload's demand just met</b>; above 1&times;
= margin. Because the score is an average, one slow phase can be hidden by
fast ones — check the per-phase table below for red "below 1&times;"
badges: those mark the parts of the workload this storage cannot keep up
with.</p>
<p>The CARTA phase is not part of the score. That phase imitates a person
browsing images in a viewer, so it reads slowly on purpose — its
throughput number reflects that chosen pace, not the storage's speed. For
that phase, look at the p99 latency in the table instead: how long a single
read takes (lower is better).</p>
</details></div>
<div class="cards">{cards}</div>
<h2 class="comp">Bulk throughput (bytes-weighted composite, dsc-065 mix) — secondary metric: compensates tail failures, see cards above</h2>
<div class="phase"><table>
<tr><th style="width:22%">storage class</th><th style="width:34%"></th><th></th></tr>
{comp_rows}</table></div>
{sections}
<div class="phase"><h2>Insights</h2><ul>{insights_html}</ul></div>
<div class="phase"><details><summary style="cursor:pointer;font-size:16px">
<b>Where does everything come from?</b></summary>
<table class="src">
<tr><th>what</th><th>where it comes from</th></tr>
<tr><td><b>the eight test phases</b></td>
<td>reading the source code of all 12 active DSC workflows (public on the
SRCNet GitLab). The tests replay the I/O patterns those workflows perform
— without the astronomy software or its datasets.</td></tr>
<tr><td><b>file sizes and shapes in the tests</b></td>
<td>the real datasets, as registered in the SRCNet Rucio data lake and
listed on the
<a href="https://confluence.skatelescope.org/pages/viewpage.action?pageId=356758319">DSCs data Hub page</a>.
Example: the DSC-065 dataset is 83 files totalling 3.2 TB, two of which
hold 99% of the data — which is why the tests use two big files plus 81
small ones.</td></tr>
<tr><td><b>the 1&times; reference rates</b></td>
<td>measured on SWE-SRC where possible: most come from the DSC-065 imaging
run, one rate from the DSC-008 run, one from Rucio transfer measurements,
and four are estimates. The per-phase breakdown with links is in the
"What is the 1&times; reference?" panel at the top.</td></tr>
<tr><td><b>how much each phase counts in the score</b></td>
<td>byte shares measured in the same DSC-065 run: it read 1.04 TB and
wrote 808 GB over its 4.6 hours. The percentages shown next to each phase
name are these shares; together the eight phases cover 89% of the run's
I/O (the rest is in patterns not run in the score scenario).</td></tr>
<tr><td><b>the 4.6 h compute budget</b></td>
<td>the wall-clock time of that same DSC-065 run.</td></tr>
<tr><td><b>throughput, IOPS and latency in this report</b></td>
<td>no reference — measured on this storage by the benchmark itself. They
are properties of the storage and stay the same whichever DSC you care
about.</td></tr>
</table>
<p>If a different DSC family mattered more (pulsar timing, HI cubes…), the
per-phase measurements stay valid; the weights and reference rates would be
re-derived from that family's runs. Only the DSC-065 tables exist
today.</p>
</details></div>
<div class="footer">disco — synthetic I/O benchmarks representative of SRCNet DSC workloads.</div>
</div></body></html>"""
    with open(out, "w") as fh:
        fh.write(html)
    print(f"wrote {out}")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(here, "reports"))
    ap.add_argument("--out", default=os.path.join(here, "reports", "report.html"))
    args = ap.parse_args()
    classes = collect(args.results)
    if not classes:
        print("no results found", file=sys.stderr)
        sys.exit(1)
    render(classes, args.out, args.results)


if __name__ == "__main__":
    main()
