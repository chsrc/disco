# disco, Detailed Science Cases I/O benchmarks

This project contributes synthetic I/O benchmarks that behave like the SRCNet science workloads.
This enables **representative** performance benchmarking of storage systems in minutes
instead of running terabyte-scale astronomy pipelines for hours.

Built from the actual Detailed Science Case (DSC) workflows: their code,
their real datasets, and recordings of their real runs. The name is an
anagram of *DSC I/O*.

**Contents:** [how it was built](#how-these-benchmarks-were-built) ·
[the phases](#the-phases-and-what-they-represent) ·
[why they are representative](#what-makes-the-phases-representative) ·
[limitations](#what-we-deliberately-did-not-copy) ·
[running it](#running-the-benchmarks) ·
[reading results](#reading-the-results) ·
[detailed docs](#detailed-documentation)

---

## Background: the workloads

A **DSC (Detailed Science Case)** is a documented astronomy workflow that
the SKA Regional Centre network (SRCNet) must support. Each one is a series
of steps a scientist runs: stage the data, calibrate it, make images, find
sources in them, make catalogues. Twelve DSCs are actively implemented, as
code in public GitLab repositories
([ska-telescope/src/sd/workflows](https://gitlab.com/ska-telescope/src/sd/workflows)).

Their inputs are mostly **MeasurementSets**, radio-astronomy data
directories. A typical one is a folder of ~83 files where two files hold
99% of the data (e.g. the DSC-065 dataset: 3.2 TB in 83 files, two of them
2.1 TB and 1.1 TB). The rest of the traffic is FITS images (up to ~1 GB
each), catalogues (MBs), and many small plot files.

Running a DSC for real needs these datasets staged on a node and takes
hours to days. That is what this benchmark replaces.

## How were these benchmarks built?

**1. We read the source code of every DSC workflow.**
The 12 workflows are Python scripts and notebooks.
The code says exactly which files each step opens, which tools it runs and with which options.
We extracted, step by step: files read, files written, tool command
lines, and how many processes run at once. Nothing was executed.

**2. We read the code of the tools underneath.**
The workflows drive real astronomy tools: WSClean (imaging), DP3
(calibration), casacore (the library that reads MeasurementSets), PyBDSF
(source finding), CARTA (interactive image viewing), PSRCHIVE (pulsar
timing), and a few more. We studied how each one actually reads and writes
files. Example findings: WSClean always writes a temporary reordered copy
of the dataset and then re-reads it many times. PyBDSF loads whole images
into memory. the CARTA viewer reads images in small random tiles.

**3. We distilled the recurring I/O patterns.**
Every DSC turned out to be a composition of the same ~11 patterns (full
dataset scans, temporary-copy-and-reread, column read-modify-write,
whole-file loads into RAM, image read/write chains, random tile reads,
staging downloads, small-file fan-out, ...).
The DSCs mostly consist of a different mix of these patterns, which we call I/O phases.
That is why there is one benchmark suite with parameter sets, instead of one benchmark per DSC.

**4. We took the file shapes from the real datasets.**
Sizes, file counts and layouts come from the SRCNet Rucio data lake, where
the actual datasets are registered (e.g. "83 files, 3.2 TB, two files hold
99%").

**5. We took the speeds and byte counts from recordings of real runs.**
The Science Delivery team records every DSC run with a monitoring tool
(psrecord, one sample per second: CPU, memory, bytes read/written). From
those recordings we got the real per-step rates and volumes.
For instance, the DSC-065 imaging run reads 1.04 TB and writes 808 GB over 4.6 hours,
in ~68 million read requests averaging **~16 KB each**.
That last number matters: it is why the benchmark issues the same mixture of small and large
requests (a plain big-block benchmark would ask the storage a different
question than the real workload does).

**6. We turned each pattern into a small fio job.**
[fio](https://fio.readthedocs.io/) is the standard Linux I/O testing tool.
Each pattern became a short job file (`profiles/`): the file layout, the
read/write pattern, the request-size mixture, the concurrency, and the
rate caps (for the modes that replay real speeds).
The jobs intentionally contain no astronomy code and no real data,
since we do not care about producing a scientific result, but only benchmarking the storage system.

## I/O phases

| Phase | Real step it models | Which DSCs do it |
|---|---|---|
| Staging ingest | downloading the dataset onto the node (Rucio) | all |
| Reorder copy | WSClean's temporary reordered dataset copy | 065, 057, 007, 008, 043 |
| Major-cycle re-reads | deep imaging re-reading the dataset many times | 065, 057, 062. 008's self-cal loop |
| Column RMW | calibrating/subtracting: read a column, write a new one | 043, 008, 007 |
| Image chain | read/write image generations (blank → convolve → cube) | 065, 057, 007 |
| CARTA tiles | a scientist browsing images interactively | all |
| Sparse QA | QA plots reading one baseline out of thousands | 007, 037 |
| Small-file fan-out | per-source plots, catalogues, logs | 065, 057, 053, 035 |

## Why these eight phases

The score runs 8 of the 14 job files. Together they carry 89% of the bytes
the reference workload moves. Each of the other six is left out for a
clear reason:

- The **staging verify** step re-reads the file the ingest step just wrote.
  It would measure the same thing again.
- The **full dataset scan** is the same sequential read of the same files
  that the reorder step already performs.
- The **whole directory copy** and the **burst load** are again the same
  big sequential reads and writes, just arranged differently.
- The **image rewrite loop** repeats a small read and write. Its
  interesting part is the timing of the repeats, not the speed of the
  storage.
- Two jobs keep slow rates on purpose. The **viewer** phase models a
  person browsing images, so it reads slowly by design and is judged on
  how long a single read takes. The **monitoring log** writes a small
  line ten times per second, like the real monitoring tool.

The full catalog still exists. The capacity scenario runs all of it when
you want the ceiling on every pattern. The scoring only covers the eight
phases, because the other six have no measured reference to compare
against.

## What makes the phases representative?

A benchmark phase is representative when it asks the storage the same
questions the real workload asks. What we copied, per phase:

- **File layout and sizes**, from the real datasets (data lake inventory).
- **Read/write pattern**, sequential scan, random tiles, chunked
  read-modify-write, etc., from the tool behaviour.
- **Request sizes**, the measured mixture (~16 KB average for dataset
  reads), not just big blocks.
- **Concurrency**, e.g. the image chain runs 8 workers because the real
  workflow does (`xargs -P 8` in its code).
- **Byte volumes and rates**, from the recorded real runs (where
  available. the report marks which numbers are measurements and which are
  estimates, with links).

One check worth knowing about: the recorded speeds were themselves partly
limited by the storage they were measured on (the same run also reached
4to5× higher speeds in bursts). We verified this and treat the reference
speeds as "the experience on SWE-SRC", not as physical laws, the report
documents this per phase.

## What did we omit deliberately?

- **The compute.** The real runs are mostly CPU-bound.
  The benchmark produces no CPU load and no large memory footprint.
  Consequence: the benchmark's page cache is not disturbed by application memory the way
  the real workload's is. This is the main known divergence.
- **Duration, in the fast modes.** We aim to produce a score within minutes,
  and purposefully avoid taking as long as the DSCs.
- **Interleaving.** Phases run one after another, while real workflows overlap some steps.

## Running the benchmarks

Dependencies: bash, fio, python3, gettext (`envsubst`).

```bash
./run-scenario.sh score    <dir-on-storage-under-test>   # ~45 min, the measurement
./run-scenario.sh capacity <dir>                         # same method, all phases
./run-scenario.sh dsc-065  <dir> 1.0                    # ~3-4 h, replays the real run
JOBS=8    ./run-scenario.sh score <dir>                 # multi-stream check
UNCAPPED=0 RATE_MULT=1 ./run-scenario.sh score <dir>    # score phases at real rates
PHASES=...                                               # run only some phases
python3 score.py <run-dir>                                # metrics + score
```

On a cluster: `deploy/` has ready-made Job YAMLs for three storage
classes, a results fetch script, and a Dockerfile. The session images on
the SRCNet nodes do not ship fio. Install it in your user space with
`conda install -c conda-forge fio gettext`.

Each mode answers a different question:

| Question it answers | Command | Wall time |
|----------------------|---------|-----------|
| How fast can this storage serve the DSC patterns? | `./run-scenario.sh score <dir>` | ~45 min (three repetitions by default) |
| Does the storage sustain the real demand rates on the eight key phases? | `UNCAPPED=0 RATE_MULT=1 ./run-scenario.sh score <dir>` | ~45 min |
| Does the storage deliver the full recorded experience, structure and duration included? | `./run-scenario.sh dsc-065 <dir> 1.0` | hours |
| What is the ceiling on every pattern, including the ones the score does not weigh? | `./run-scenario.sh capacity <dir>` | minutes |

- **score** is the default measurement and what the report uses. It
  removes the rate caps, so the storage does the best it can, and runs
  each phase three times by default (`REPEATS=1` for quick iteration).
  Two jobs
  keep a slow rate on purpose, because the rate is part of what they
  model: the interactive viewer (a person browsing reads slowly, its
  metric is how long one read takes) and the monitoring log (a small line
  ten times per second).
- **paced score** runs the same eight phases at the real recorded demand
  rates. It checks that the storage keeps up with the real tempo over a
  sustained stretch. It cannot separate a storage that barely keeps up
  from one with ten times the headroom, which is why the uncapped score
  is the primary number and this is the check.
- **replay** reproduces the rates, the structure and the duration of the
  recorded runs: all patterns chained in pipeline order, several cycles,
  idle gaps, a background monitoring load. Durations range from about 30
  minutes for the small scenarios to about 20 hours for `dsc-065-deep`.
  Use it to validate a storage against the measured traces, whose
  reference numbers are in the report and in the provenance table inside
  `score.py`. Do not use it to compare storage classes, because the rate
  caps would hide the advantage of a faster storage.
- **capacity** uses the same uncapped method as score but runs every
  phase. Use it to probe patterns the score does not weigh.

## Reading the results

`python3 make_report.py` generates `reports/report.html`, a
self-contained comparison page with explanations built in ("What is the
1× reference?", "What is headroom?", "What is the score?"). Headline
metrics:

- **Score**: byte-weighted geometric mean of per-phase headroom
  ("how many times over could this storage handle what the workload
  needs")
- **I/O pressure**: I/O time as a share of the reference run's compute
  time (below 1 = storage keeps up)
- **Per-phase table**: where any phase falls below the workload's
  demand (red badges) and read-latency tails (p99)

Current measured results live in `reports/` (three storage classes on the
gornergrat cluster: Ceph RBD on NVMe, Ceph RBD on HDD, VAST NFS).

---

## Knobs

`JOBS=8` adds parallel readers over disjoint regions of the big files.
Use it for the multi-stream check, because rankings can flip between
single-stream and multi-stream on parallel-friendly filesystems.

`PHASES="a b c"` runs only the named phases.

`scale` (the third argument) shrinks the working set for quick tests. The
default `score` working set is about 190 GB, so on nodes with more than
95 GB of RAM, raise the scale (see the caveat below for why).

## Profiles (profiles/*.fio)

| # | File | Pattern | Models | Key params |
|---|------|---------|--------|------------|
| 01 | `01-ms-tile-scan.fio` | MS column scan, ~16 KB syscalls, rate-limited | WSClean/DP3/casacore MS reads | COLA, COLB (two TSM column files), 81 metadata files |
| 02 | `02-reorder-copy.fio` | Concurrent seq read + seq write of same size | wsclean `-reorder`, DDFacet `.ddfcache` | REORD (~0.5to1× data column) |
| 03 | `03-major-cycle-rereads.fio` | N repeated full sequential passes | CottontoSchwab major cycles (2 passes/cycle) | CYCLES (≈2× major iterations) |
| 04 | `04-column-rmw.fio` | 1 GB-chunk interleaved column read + write | DP3 applybeam, Crystalball MODEL_DATA, casacore subtract (100k-row chunks) | REORD |
| 04b | `04b-ms-copytree.fio` | Whole-MS copy | DSC-008 Step 5b copytree, DSC-035 `cp` chains | COLB |
| 05 | `05-burst-read.fio` | 2 GB/s full-file RAM load | pyuvdata/hera_pspec/PSRCHIVE/PyBDSF whole-file loads | COLA+COLB (runner adds idle gap) |
| 06 | `06-fits-image-chain.fio` | NIMG files read/written 8-way, per generation | blank→convolve→avg→cube chains, PyBDSF slice fan-out | NIMG_W, IMGSZ (runner runs 4 generations) |
| 07 | `07-inject-rmw-loop.fio` | Read-modify-write × 10 on one image | source-injection loop | chains onto 06 products |
| 08 | `08-carta-random-tiles.fio` | Random 256 KB reads, rate-limited, 4 sessions | CARTA interactive visualisation | 300 s fixed runtime |
| 09/09b | `09-staging-ingest.fio` | 250 MB/s network-rate write. verify re-read | Rucio download + checksum | COLA |
| 10 | `10-smallfile-monitor.fio` | PNG storm (2000×128 KB) + catalogues | per-source plots, catalogue writes | (n/a) |
| 10a | `10a-monitor-background.fio` | 10 Hz tiny writes, background | psrecord logging | started/killed by runner |
| 11 | `11-sparse-baseline-qa.fio` | Random 8 KB reads, <1% of volume | single-baseline TaQL QA, shadems | QA |

Two modes per profile: **fidelity** (rate-limited to measured per-process
rates, looks like the real trace) and **capacity** (remove `rate=`, larger
`bs`, measures the storage ceiling). Job-file headers document both.

## Reports

Results of runs on storage classes under test live in `reports/<class>/run-<ts>/`
(fio JSONs + rendered job files + job log). The comparison report is
`reports/report.html`. Regenerate it after adding new runs:

    python3 make_report.py            # reads reports/, writes reports/report.html


## Notes & caveats

- **The score is a short measurement, valid under three conditions.** Each
  phase should run past its transient regime. The working set should be
  at least twice the node RAM: at one times RAM much of the data can stay
  resident in the page cache, so a large share of reads still comes from
  memory. At two times, every pass through the data must miss the cache
  about half the time, so the measurement is dominated by real storage
  traffic. And repeat: the default three repetitions
  (`REPEATS=3`) cover short-term variance, but back-to-back repetitions
  are correlated. Interference from other tenants comes and goes over
  hours, so for a decision you care about, repeat whole runs spread over
  a day.
- For multi-client aggregate testing (the DP3-AA4 shared-storage failure
  mode), run several scenarios concurrently on different clients.
