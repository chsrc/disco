#!/usr/bin/env bash
# run-scenario.sh — compose fio profiles into a DSC-representative I/O benchmark scenario
#
# Usage:
#   ./run-scenario.sh <scenario> [workdir] [scale]
#
#   scenario: dsc-065 | dsc-065-raw | dsc-065-deep | dsc-008 | dsc-007 | dsc-037 |
#             dsc-043 | dsc-035 | dsc-053 | score | capacity
#   workdir:  directory on the storage under test (default: ./disco-data)
#   scale:    omitted (default) = read working set of 2x the node's RAM — the
#             minimum for cold-cache readings; "full" = 1.0 (real dataset
#             sizes); a number = that fraction of the full DSC size
#
# Env vars:
#   UNCAPPED=0/1     1 = strip the rate= caps for a capability measurement
#                   (default 1 in score and capacity). Set 0 to keep the
#                   caps, e.g. the paced score variant:
#                     UNCAPPED=0 RATE_MULT=1 ./run-scenario.sh score ...
#                   runs the score phases at the real recorded demand rates.
#   RATE_MULT=N      multiplies every rate= cap (only meaningful with
#                   UNCAPPED=0, e.g. accelerating a replay run)
#   JOBS=N           reader concurrency for the MS-scan and major-cycle phases
#                   (default 1 = the traced run's effective depth). Set 8 or
#                   16 to test the multi-stream regime: rankings can flip on
#                   parallel-friendly filesystems (CephFS scales with streams).
#   PHASES="a b c"   run only the named phases (default: all)
#   REPEATS=N        run each phase N times (default 3 in score, 1
#                   elsewhere). Per-repetition
#                   results are written as separate JSON files and the
#                   scoring pools them (throughput) and takes the worst
#                   latency tail. Note: back-to-back repetitions capture
#                   short-term variance only. For time-of-day variance,
#                   repeat whole runs spread over a day.
#   MAXTIME=SECONDS  per-phase watchdog (default 0 = off). Phases stay
#                   size-based and stop on their own when the workload is
#                   done; one that exceeds MAXTIME is killed and reported
#                   instead of hanging the whole run for hours. A guard, not
#                   a measurement method: a healthy phase is never affected.
#
# Per-phase results are written as fio JSON to <run>/logs/<phase>.json.
set -euo pipefail

SCENARIO=${1:?scenario required}
WORKDIR=${2:-./disco-data}
SCALE=${3:-auto}
if [ "$SCALE" = "full" ]; then SCALE=1; fi
HERE="$(cd "$(dirname "$0")" && pwd)"
PROFILES="$HERE/profiles"
RUN="$WORKDIR/run-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN/logs"

# helper: scale a size in GiB by $SCALE, floor at 1 GiB
sz() { python3 -c "import sys; print(max(int(float(sys.argv[1])*float(sys.argv[2])),1))" "$1" "$SCALE"; }

# helper: divide a size in GiB by JOBS, floor at 1 GiB (per-reader region)
pjsz() { python3 -c "import sys; print(max(int(float(sys.argv[1])/int(sys.argv[2])),1))" "$1" "${JOBS:-1}"; }

# render: substitute ${VAR} tokens in a profile with the current parameters
# (envsubst with an explicit variable list), then either strip the rate= caps
# (UNCAPPED=1, used by score/capacity for capability measurement) or apply
# the RATE_MULT factor to them (replay modes).
# The interactive viewer keeps its rate even when uncapped, because the
# rate is part of what it models, not a demand cap: a person browsing reads
# slowly, its metric is read latency.
render() {
  local jobfile="$1"
  local strip=0
  if [ "${UNCAPPED:-0}" = "1" ]; then
    case "$jobfile" in
      *08-carta*|*10-smallfile*) strip=0 ;;
      *) strip=1 ;;
    esac
  fi
  DIR="$RUN" COLA="$COLA" COLB="$COLB" REORD="$REORD" \
  COLA_PJ="$(pjsz "$COLA")" COLB_PJ="$(pjsz "$COLB")" REORD_PJ="$(pjsz "$REORD")" \
  JOBS="${JOBS:-1}" \
  CYCLES="$CYCLES" NIMG_W="$NIMG_W" IMGSZ="$IMGSZ" QA="$QA" \
  envsubst '${DIR} ${COLA} ${COLB} ${REORD} ${COLA_PJ} ${COLB_PJ} ${REORD_PJ} ${JOBS} ${CYCLES} ${NIMG_W} ${IMGSZ} ${QA}' < "$jobfile" \
  | python3 -c '
import re, sys
mult = float(sys.argv[1])
strip = sys.argv[2] == "1"
data = sys.stdin.read()
if strip:
    data = re.sub(r"^\s*rate=.*\n", "", data, flags=re.M)
elif mult != 1.0:
    data = re.sub(r"^(\s*rate=)([\d.]+)([kmg])",
                  lambda m: "%s%.4g%s" % (m.group(1), float(m.group(2)) * mult, m.group(3)),
                  data, flags=re.M)
sys.stdout.write(data)' "${RATE_MULT:-1}" "$strip"
}

# helper: run a fio job with variable substitution; emits fio JSON per phase
phase() {
  local name=$1 job=$2
  if [ -n "${PHASES:-}" ] && ! grep -qw "$name" <<< "${PHASES}"; then
    echo "=== phase $name skipped (not in PHASES subset)"
    return 0
  fi
  local rendered="$RUN/${name}.fio"
  render "$PROFILES/$job" > "$rendered"
  echo "=== phase $name ($(date +%T)) -> $job"
  local rep out
  for rep in $(seq 1 "${REPEATS:-1}"); do
    out="$RUN/logs/${name}-${rep}.json"
    if [ "${MAXTIME:-0}" != "0" ]; then
      if ! timeout "$MAXTIME" fio "$rendered" --output-format=json --output="$out"; then
        echo "    WARNING: phase $name rep $rep exceeded MAXTIME=${MAXTIME}s — killed, no results" >&2
        rm -f "$out"
      fi
    else
      fio "$rendered" --output-format=json --output="$out"
    fi
  done
}

# SCALE=auto (score/capacity): target a read working set of 2x
# node RAM — the minimum for cold-cache readings (see README). Reads host
# MemTotal from /proc/meminfo, so it works in a tenant pod without host knob
# access; no drop_caches/O_DIRECT needed. Other scenarios: pass a number.
if [ "$SCALE" = "auto" ]; then
  case $SCENARIO in
    score) FULL_READ_GIB=190 ;;   # ~96+48+72 GiB files + image/QA
    capacity)          FULL_READ_GIB=300 ;;   # ~100+100+100 GiB files
    *) echo "SCALE=auto supports score/capacity only" >&2; exit 1 ;;
  esac
  [ -r /proc/meminfo ] || { echo "SCALE=auto requires /proc/meminfo (Linux)" >&2; exit 1; }
  MEM_KIB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
  SCALE=$(python3 -c "import sys; print(round(max(float(sys.argv[1])/1048576*2/float(sys.argv[2]),0.15),3))" "$MEM_KIB" "$FULL_READ_GIB")
  echo "=== SCALE=auto: node MemTotal $((MEM_KIB/1024)) MiB -> scale $SCALE (read set = 2x RAM)"
fi

# default parameter set per scenario (sizes in GiB, times in s)
case $SCENARIO in
dsc-065)  # DEEP_FIELD: wsclean deep imaging of 3.2 TB AA2 MS + image chain + pybdsf + injections
  COLA=$(sz 131); COLB=$(sz 66); REORD=$(sz 120); CYCLES=4
  NIMG=40; IMGSZ=$(sz 1.05) QA=$(sz 2) ;;
dsc-065-raw)  # DEEP_FIELD raw: 3.2 TB staging + full raw MS scan (DP3-averaging analog)
  COLA=$(sz 2084); COLB=$(sz 1042); REORD=$(sz 263); CYCLES=4
  NIMG=40; IMGSZ=$(sz 1.05) QA=$(sz 2) ;;
dsc-065-deep)  # DEEP_FIELD 1M-iter: 22 h, 1.75 TB R / 1.45 TB W
  COLA=$(sz 131); COLB=$(sz 66); REORD=$(sz 120); CYCLES=12
  NIMG=40; IMGSZ=$(sz 1.05) QA=$(sz 2) ;;
dsc-008)  # DIFFUSE_CLUSTER: 10x selfcal cycles on 154 GB MS + column RMW + reimaging
  COLA=$(sz 100); COLB=$(sz 50); REORD=$(sz 75); CYCLES=20
  NIMG=6; IMGSZ=$(sz 0.008) QA=$(sz 1) ;;
dsc-007)  # 21cm forest: DP3 streaming pass + wsclean reorder + 400-file image chain + cube analytics
  COLA=$(sz 52); COLB=$(sz 26); REORD=$(sz 39); CYCLES=20
  NIMG=400; IMGSZ=$(sz 0.017) QA=$(sz 1) ;;
dsc-037)  # cable reflections: 3x whole-MS RAM loads with CPU-idle gaps + DP3 2-pass
  COLA=$(sz 26); COLB=$(sz 13); REORD=$(sz 4); CYCLES=6
  NIMG=100; IMGSZ=$(sz 0.004) QA=$(sz 1) ;;
dsc-043)  # HI absorption: applybeam column add + chunked no-reorder imaging + cube assembly
          # (chunked wsclean re-reads each channel range ONCE -> CYCLES=2, not 30)
  COLA=$(sz 1496); COLB=$(sz 748); REORD=$(sz 1122); CYCLES=2
  NIMG=4001; IMGSZ=$(sz 0.067) QA=$(sz 4) ;;
dsc-035)  # pulsar timing: 8x 171 MB whole-file loads, copy chains, small-file storm
  COLA=$(sz 0.17); COLB=$(sz 0.17); REORD=$(sz 0.17); CYCLES=8
  NIMG=8; IMGSZ=$(sz 0.0002) QA=$(sz 0.01) ;;
dsc-053)  # solar dynaspec: 12x ~5.8 GB serial whole-MS reads with long CPU-idle gaps
  COLA=$(sz 4); COLB=$(sz 2); REORD=$(sz 5.8); CYCLES=12
  NIMG=12; IMGSZ=$(sz 0.0001) QA=$(sz 0.01) ;;
score)    # ~15 min per repetition, 3 repetitions by default (~45 min total):
          # the 8 byte-dominant phases, uncapped capability measurement.
          # Repetitions are pooled by the scoring (worst latency tail kept).
          # REPEATS=1 for quick iteration.
  COLA=$(sz 96); COLB=$(sz 48); REORD=$(sz 72); CYCLES=1
  NIMG=16; IMGSZ=$(sz 1) QA=$(sz 2)
  PHASES=${PHASES:-"00-ingest-write 02-reorder 03-major-cycles 04-column-rmw 06-imagechain-gen1 08-carta-tiles 09-qa-sparse 10-smallfiles"}
  UNCAPPED=${UNCAPPED:-1}
  REPEATS=${REPEATS:-3} ;;
capacity) # pure storage capability probe: all phases, rate caps multiplied
          # 1000x = effectively uncapped — the storage does the best it can
  COLA=$(sz 100); COLB=$(sz 100); REORD=$(sz 100); CYCLES=5
  NIMG=16; IMGSZ=$(sz 1) QA=$(sz 4)
  RATE_MULT=${RATE_MULT:-1000} ;;
*) echo "unknown scenario $SCENARIO" >&2; exit 1 ;;
esac

NIMG_W=$(python3 -c "import sys; print(max(int(int(sys.argv[1])/8),1))" "$NIMG")

# ---- Phase 0: staging/ingest (Rucio-like sequential write + checksum re-read) ----
phase 00-ingest-write 09-staging-ingest.fio
phase 00-ingest-verify 09b-staging-verify.fio

# ---- Phase 1: MS tile scan (single casacore-column-style pass) ----
phase 01-ms-tile-scan 01-ms-tile-scan.fio

# ---- Phase 2: wsclean reorder (concurrent read MS + write temp copy) ----
phase 02-reorder 02-reorder-copy.fio

# ---- Phase 3: deep-imaging major cycles (repeated passes over reordered data) ----
phase 03-major-cycles 03-major-cycle-rereads.fio

# ---- Phase 4: column read-modify-write (DP3 applybeam / crystalball / casacore subtract) ----
phase 04-column-rmw 04-column-rmw.fio
phase 04b-ms-copy 04b-ms-copytree.fio

# ---- Phase 5: burst-read + CPU-idle (pyuvdata/hera_pspec whole-file RAM load) ----
phase 05-burst-load 05-burst-read.fio
echo "    (simulating CPU-bound processing with idle gap)"
sleep 120

# ---- Phase 6: FITS image chain (read gen N, write gen N+1, x4 generations, 8-way) ----
for gen in 1 2 3 4; do
  phase 06-imagechain-gen$gen 06-fits-image-chain.fio
done

# ---- Phase 7: injection loop (read-modify-write 1 GB image x10) ----
phase 07-injection-loop 07-inject-rmw-loop.fio

# ---- Phase 8: interactive visualisation (CARTA random tile reads, latency-bound) ----
phase 08-carta-tiles 08-carta-random-tiles.fio

# ---- Phase 9: sparse QA / single-baseline column reads ----
phase 09-qa-sparse 11-sparse-baseline-qa.fio

# ---- Phase 10: small-file / metadata storm (PNG fan-out, catalogues) ----
phase 10-smallfiles 10-smallfile-monitor.fio

echo "=== scenario $SCENARIO complete; results in $RUN"
echo "per-phase fio JSON: $RUN/logs/<phase>.json"
