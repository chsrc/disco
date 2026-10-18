#!/usr/bin/env bash
# run-scenario.sh — compose fio profiles into a DSC-representative I/O benchmark scenario
#
# Usage:
#   ./run-scenario.sh score [workdir] [scale]
#
#   workdir:  directory on the storage under test (default: ./disco-data)
#   scale:    omitted (default) = read working set of 2x the node's RAM — the
#             minimum for cold-cache readings; "full" = 1.0 (real dataset
#             sizes); a number = that fraction of the full DSC size
#
# Env vars:
#   JOBS=N           reader concurrency for the major-cycle phase
#                   (default 1 = the traced run's effective depth). Set 8 or
#                   16 to test the multi-stream regime: rankings can flip on
#                   parallel-friendly filesystems (CephFS scales with streams).
#   PHASES="a b c"   run only the named phases (default: all 8)
#   REPEATS=N        run each phase N times (default 3). Per-repetition
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
[ "$SCENARIO" = "score" ] || { echo "unknown scenario '$SCENARIO' — only 'score' exists" >&2; exit 1; }
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
# (envsubst with an explicit variable list), then strip the rate= caps so the
# storage does the best it can (capability measurement). The interactive
# viewer keeps its rate, because the rate is part of what it models, not a
# demand cap: a person browsing reads slowly, its metric is read latency.
render() {
  local jobfile="$1"
  local strip=1
  case "$jobfile" in
    *08-carta*) strip=0 ;;
  esac
  DIR="$RUN" COLA="$COLA" COLB="$COLB" REORD="$REORD" \
  REORD_PJ="$(pjsz "$REORD")" \
  JOBS="${JOBS:-1}" \
  CYCLES="$CYCLES" NIMG_W="$NIMG_W" IMGSZ="$IMGSZ" QA="$QA" \
  envsubst '${DIR} ${COLA} ${COLB} ${REORD} ${REORD_PJ} ${JOBS} ${CYCLES} ${NIMG_W} ${IMGSZ} ${QA}' < "$jobfile" \
  | python3 -c '
import re, sys
strip = sys.argv[1] == "1"
data = sys.stdin.read()
if strip:
    data = re.sub(r"^\s*rate=.*\n", "", data, flags=re.M)
sys.stdout.write(data)' "$strip"
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

# SCALE=auto (the default): target a read working set of 2x node RAM — the
# minimum for cold-cache readings (see README). Reads host MemTotal from
# /proc/meminfo, so it works in a tenant pod without host knob access; no
# drop_caches/O_DIRECT needed.
if [ "$SCALE" = "auto" ]; then
  FULL_READ_GIB=190   # ~96+48+72 GiB files + image/QA
  [ -r /proc/meminfo ] || { echo "SCALE=auto requires /proc/meminfo (Linux)" >&2; exit 1; }
  MEM_KIB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
  SCALE=$(python3 -c "import sys; print(round(max(float(sys.argv[1])/1048576*2/float(sys.argv[2]),0.15),3))" "$MEM_KIB" "$FULL_READ_GIB")
  echo "=== SCALE=auto: node MemTotal $((MEM_KIB/1024)) MiB -> scale $SCALE (read set = 2x RAM)"
fi

# the reference parameter set (sizes in GiB, times in s)
COLA=$(sz 96); COLB=$(sz 48); REORD=$(sz 72); CYCLES=1
NIMG=16; IMGSZ=$(sz 1) QA=$(sz 2)
PHASES=${PHASES:-"00-ingest-write 02-reorder 03-major-cycles 04-column-rmw 06-imagechain-gen1 08-carta-tiles 09-qa-sparse 10-smallfiles"}
REPEATS=${REPEATS:-3}

NIMG_W=$(python3 -c "import sys; print(max(int(int(sys.argv[1])/8),1))" "$NIMG")

# ---- Phase 0: staging/ingest (Rucio-like sequential write) ----
phase 00-ingest-write 09-staging-ingest.fio

# ---- Phase 2: wsclean reorder (concurrent read MS + write temp copy) ----
phase 02-reorder 02-reorder-copy.fio

# ---- Phase 3: deep-imaging major cycles (repeated passes over reordered data) ----
phase 03-major-cycles 03-major-cycle-rereads.fio

# ---- Phase 4: column read-modify-write (DP3 applybeam / crystalball / casacore subtract) ----
phase 04-column-rmw 04-column-rmw.fio

# ---- Phase 6: FITS image chain (read gen N, write gen N+1, 8-way) ----
phase 06-imagechain-gen1 06-fits-image-chain.fio

# ---- Phase 8: interactive visualisation (CARTA random tile reads, latency-bound) ----
phase 08-carta-tiles 08-carta-random-tiles.fio

# ---- Phase 9: sparse QA / single-baseline column reads ----
phase 09-qa-sparse 11-sparse-baseline-qa.fio

# ---- Phase 10: small-file / metadata storm (PNG fan-out, catalogues) ----
phase 10-smallfiles 10-smallfile-monitor.fio

echo "=== scenario $SCENARIO complete; results in $RUN"
echo "per-phase fio JSON: $RUN/logs/<phase>.json"
