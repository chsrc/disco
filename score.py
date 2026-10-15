#!/usr/bin/env python3
"""score.py — turn a `score`-mode run into a perf metric + full-run prediction.

Usage:
    ./score.py <run-dir> [--family dsc-065]

Reads the per-phase fio JSON (<run-dir>/logs/<phase>.json) produced by
run-scenario.sh in `score` mode, prints per-phase steady-state metrics
(throughput, IOPS, p99 latency) and:

  1. a bytes-weighted composite throughput (weights = each pattern's share
     of total I/O in the measured reference run), and
  2. a predicted wall time for the chosen DSC family's full run, computed as
     sum(known byte volume per phase / measured throughput per phase).

Caveats (why this is a screen, not a substitute for a full run):
  - predicts storage-bound behaviour; the real runs are compute-bound, so
    also check headroom (measured capability vs the required 1x rates)
  - misses time-varying interference (repeat runs across the day)
  - validity assumes working set > node RAM during measurement
"""
import argparse
import glob
import json
import math
import os
import sys

# Byte weights: share of total I/O per phase, from the measured DSC-065
# 100k-iteration run (1.04 TB read + 0.81 TB written, ~1.85 TB total) and the
# DSC-008 e2e run. Edit per family as better measurements arrive.
WEIGHTS = {
    "dsc-065": {  # DEEP_FIELD family
        "00-ingest-write": 0.14,   # staging write (+ checksum read)
        "02-reorder":      0.07,   # MS scan + reordered-copy write
        "03-major-cycles": 0.42,   # repeated passes (dominant)
        "04-column-rmw":   0.02,   # column read-modify-write (bigger in DSC-008)
        "06-imagechain-gen1": 0.20,  # many-file parallel read/write chain
        "08-carta-tiles":  0.02,   # interactive random reads
        "09-qa-sparse":    0.01,   # sparse QA reads
        "10-smallfiles":   0.01,   # plot/catalogue fan-out
        # remainder (~0.11) is phases not in the score subset (copytree, burst)
        # — normalized out below
    },
}

# Full-run byte volumes per phase (GiB, moved at least once), calibrated to
# the measured DSC-065 100k-iteration run, for the runtime prediction.
# The reference run's compute budget: the DSC-065 100k-iteration run on
# SWE-SRC took 4.6 h wall time, compute-dominated (CPUs were the pace-setter).
REF_COMPUTE_H = 4.6

VOLUMES = {
    "dsc-065": {
        "00-ingest-write": 263,    # staging of the averaged MS
        "02-reorder":      383,    # read 197 + write ~186 reordered
        "03-major-cycles": 860,    # reorder-scan + inversion + cycle passes
        "04-column-rmw":    40,    # minor for DEEP_FIELD; ~66 GB in DSC-008
        "06-imagechain-gen1": 84,  # one generation of the 4-gen chain
        "08-carta-tiles":  20,     # an interactive session's tile traffic
        "09-qa-sparse":    2,
        "10-smallfiles":    12,    # 12k plots + catalogues + logs
    },
}

# The real workload's DEMAND per phase (MB/s) — as MEASURED on SWE-SRC for
# the reference runs (see DEMAND_SOURCES for provenance). POLICY: measured
# values are used UNROUNDED (no rounding in either direction — rounding
# shifts near-1x verdicts); estimates are explicit and marked as such. Caveat: the
# reference run was partly I/O-wait-bound (CPU ~60% while reading; the same
# storage sustained 261-574 MB/s in bursts), so these rates are an interaction
# outcome of pattern x that storage, NOT a purely intrinsic demand. Headroom
# therefore means "capability relative to the SWE-SRC reference experience".
DEMAND_1X = {
    "00-ingest-write": 250,  # MB/s (network-limited staging)
    "02-reorder":      500,  # MB/s combined
    "03-major-cycles": 62.6,  # MB/s — measured, unrounded
    "04-column-rmw":  780,  # MB/s — measured, unrounded
    "06-imagechain-gen1": 100,  # MB/s, 8-way parallel
    "08-carta-tiles":   4,   # MB/s per session (latency matters more)
    "09-qa-sparse":     10,  # MB/s
    "10-smallfiles":     5,  # MB/s
}

# Provenance of each reference rate: what it is, where it came from, and a
# link where one exists (SRCSC Confluence is private; links work for SRCNet
# team members with access).
DEMAND_SOURCES = {
    "00-ingest-write": dict(
        kind="measured (network, not storage)",
        source="Rucio single-stream staging rate, ~250 MiB/s for a ~10 TB file (RSE transfer campaign)",
        url="https://confluence.skatelescope.org/pages/viewpage.action?pageId=409720961"),
    "02-reorder": dict(
        kind="estimate",
        source="SWE-SRC peak sustained write burst 574 MB/s in the same run (reorder is read+write; ~500 set as combined demand)",
        url="https://confluence.skatelescope.org/pages/viewpage.action?pageId=386754533"),
    "03-major-cycles": dict(
        kind="measured",
        source="DSC-065 100k-iteration WSClean run on SWE-SRC: mean read rate 62.6 MB/s over 4.6 h (1.04 TB / 68.4M read syscalls, psrecord trace)",
        url="https://confluence.skatelescope.org/pages/viewpage.action?pageId=386754533"),
    "04-column-rmw": dict(
        kind="measured",
        source="DSC-008 e2e casacore subtract step: ~65 GB moved in 84 s ≈ 780 MB/s",
        url="https://confluence.skatelescope.org/pages/viewpage.action?pageId=409720206"),
    "06-imagechain-gen1": dict(
        kind="estimate",
        source="8-way parallel image chain; no direct measurement",
        url=None),
    "08-carta-tiles": dict(
        kind="measured",
        source="CARTA backend session on SWE-SRC: near-idle (~3% CPU, 178 MB RAM), latency-bound interactive traffic",
        url="https://confluence.skatelescope.org/pages/viewpage.action?pageId=402431698"),
    "09-qa-sparse": dict(
        kind="estimate",
        source="QA notebooks read decimated single-baseline columns; no direct measurement",
        url=None),
    "10-smallfiles": dict(
        kind="estimate",
        source="Serial plot/catalogue writes; no direct measurement",
        url=None),
}


def phase_files(logs, phase):
    """All result files for a phase: {phase}-N.json (repetitions), with
    fallback to the single {phase}.json from runs before REPEATS existed."""
    files = sorted(glob.glob(os.path.join(logs, f"{phase}-*.json")))
    if not files:
        single = os.path.join(logs, f"{phase}.json")
        files = [single] if os.path.exists(single) else []
    return files


def phase_metrics_aggregate(files):
    """Pool repetitions: throughput from summed bytes over summed runtime,
    IOPS pooled, latency tail is the worst across repetitions."""
    rd = wr = iops = rt = 0.0
    p99s = []
    for f in files:
        r, w, io, runt, p99 = phase_metrics(f)
        rd += r
        wr += w
        iops += io * (runt or 1)  # time-weighted pooling
        rt += runt or 0
        if p99:
            p99s.append(p99)
    iops = iops / rt if rt else 0
    return rd, wr, iops, rt, (max(p99s) if p99s else None), len(files)


def phase_metrics(path):
    d = json.load(open(path))
    rd = wr = iops = 0
    runtime = 0.0
    p99 = []
    for job in d.get("jobs", []):
        for op in ("read", "write"):
            j = job[op]
            rd += j.get("io_bytes", 0) if op == "read" else 0
            wr += j.get("io_bytes", 0) if op == "write" else 0
            iops += j.get("iops", 0) or 0
            if j.get("io_bytes"):
                runtime = max(runtime, j.get("runtime", 0) / 1000)
            lat = j.get("lat_ns", {})
            if lat.get("N") and op == "read":
                # fio 3.x stores percentiles as lat_ns.percentile["99.000000"]
                pct = lat.get("percentile", {})
                v99 = pct.get("99.000000") or pct.get("99.00000") or lat.get("99_percentile")
                if v99:
                    p99.append(float(v99) / 1e6)
    return rd, wr, iops, runtime, (max(p99) if p99 else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--family", default="dsc-065", choices=sorted(WEIGHTS))
    args = ap.parse_args()

    logs = os.path.join(args.run_dir, "logs")
    weights = WEIGHTS[args.family]
    volumes = VOLUMES[args.family]
    total_w = sum(weights.values())

    print(f"{'phase':<18}{'GB moved':>10}{'throughput':>14}{'IOPS':>10}{'p99 ms':>9}{'headroom':>10}")
    print("-" * 71)
    print("(headroom = measured capability / SWE-SRC reference-run rate;")
    print(" the reference was partly I/O-bound, so headroom > 1x means your")
    print(" storage beats the reference experience; provenance: DEMAND_SOURCES)")
    print("-" * 71)
    scores, predicted_s, heads_geo = [], 0.0, []
    for phase, w in sorted(weights.items()):
        files = phase_files(logs, phase)
        if not files:
            print(f"{phase:<18} (missing)")
            continue
        rd, wr, iops, rt, p99, nreps = phase_metrics_aggregate(files)
        gb = (rd + wr) / 1e9
        thr = (rd + wr) / rt / 1e6 if rt else 0  # MB/s
        # predicted full-run contribution: volume / measured throughput
        vol = volumes.get(phase, 0)
        predicted_s += (vol * 1024 / thr) if thr else float("inf")
        head = f"{thr / DEMAND_1X[phase]:.1f}x" if thr else "n/a"
        print(f"{phase:<18}{gb:>10.2f}{thr:>12.0f} M/s{iops:>10.0f}"
              f"{(p99 if p99 else 0):>9.1f}{head:>10}")
        scores.append((w / total_w, thr))
        # score input: capability-meaningful ratios only. CARTA is excluded:
        # it runs rate-capped, so its throughput ratio measures the cap, not
        # capability (its metric is the p99 latency, shown separately).
        # The score is the BYTE-WEIGHTED geometric mean: phases are slices of
        # one workload, so importance follows byte share (WEIGHTS).
        if thr and phase != "08-carta-tiles":
            heads_geo.append((w, thr / DEMAND_1X[phase]))

    composite = sum(w * t for w, t in scores) if scores else 0
    wsum = sum(w for w, _ in heads_geo)
    geomean = (math.exp(sum(w * math.log(h) for w, h in heads_geo) / wsum)
               if heads_geo else 0)
    print("-" * 71)
    reps_seen = max((phase_files(logs, p) and len(phase_files(logs, p))
                     for p in weights), default=0)
    if reps_seen > 1:
        print(f"({reps_seen} repetitions per phase, pooled)")
    print(f"score (byte-weighted geomean headroom): {geomean:.1f}x  "
          f"(capability phases, carta excluded, byte-weighted)")
    print(f"bytes-weighted composite throughput: {composite:.0f} MB/s "
          f"(weights: {args.family} measured trace)")
    if predicted_s and predicted_s != float("inf"):
        print(f"predicted full-run wall time (storage-bound, {args.family}): "
              f"{predicted_s / 3600:.1f} h")
        print(f"I/O pressure: {predicted_s / 3600 / REF_COMPUTE_H:.2f} "
              f"(I/O time as a share of the {REF_COMPUTE_H} h compute time "
              f"of the reference run; <1 means storage keeps up)")
        print("NOTE: the real run took 4.6 h compute-bound; if storage keeps up "
              "(all headroom > 1x), expect ~the real duration, not this bound.")


if __name__ == "__main__":
    sys.exit(main())
