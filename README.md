# disco, Detailed Science Cases I/O benchmarks

This project contributes synthetic I/O benchmarks that behave like the SRCNet science workloads.
This enables **representative** performance benchmarking of storage systems in minutes
instead of running terabyte-scale astronomy pipelines for hours.

Built from the actual Detailed Science Case (DSC) workflows: their code,
their real datasets, and recordings of their real runs. The name is an
anagram of *DSC I/O*.

**Contents:** [how it was built](#how-were-these-benchmarks-built) ·
[the phases](#io-phases) ·
[why they are representative](#what-makes-the-phases-representative) ·
[limitations](#what-did-we-omit-deliberately) ·
[running it](#running-the-benchmarks) ·
[reading results](#reading-the-results)

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
read/write pattern, the request-size mixture, the concurrency, and — where
the rate itself is part of what is modelled — a rate cap.
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

The suite distils to 8 job files. Together they carry 89% of the bytes
the reference workload moves. Patterns that did not make the cut:

- The **staging verify** step re-reads the file the ingest step just wrote.
  It would measure the same thing again.
- The **full dataset scan** is the same sequential read of the same files
  that the reorder step already performs.
- The **whole directory copy** and the **burst load** are again the same
  big sequential reads and writes, just arranged differently.
- The **image rewrite loop** repeats a small read and write. Its
  interesting part is the timing of the repeats, not the speed of the
  storage.

One kept job runs slowly on purpose. The **viewer** phase models a
person browsing images, so it reads slowly by design and is judged on
how long a single read takes.

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

- **The compute.** The real runs are not purely compute-bound — CPU
  averaged ~60% during heavy reads, a mixed regime of serial software
  phases and I/O wait. The benchmark reproduces only the I/O side: no CPU
  load and no large memory footprint.
  Consequence: the benchmark's page cache is not disturbed by application memory the way
  the real workload's is. This is the main known divergence.
- **Duration.** We aim to produce a score within minutes,
  and purposefully avoid taking as long as the DSCs.
- **Interleaving.** Phases run one after another, while real workflows overlap some steps.

## Running the benchmarks

Dependencies: bash, fio, python3, gettext (`envsubst`); coreutils
`timeout` when using the `MAXTIME` watchdog.

```bash
./run-scenario.sh score <dir-on-storage-under-test>   # the measurement
JOBS=8   ./run-scenario.sh score <dir>                # multi-stream check
PHASES=...                                            # run only some phases
python3 score.py <run-dir>                            # metrics + score
```

On a cluster: `deploy/` has ready-made Job YAMLs for three storage
classes and a Dockerfile. The session images on
the SRCNet nodes do not ship fio. Install it in your user space with
`conda install -c conda-forge fio gettext`. Results accumulate under
`/data/bench/run-<ts>/` inside the container (on the volume under test);
the Job's TTL cleans the container up an hour after it finishes, so copy
results out before that.

The score answers one question:

| Question it answers | Command | Wall time |
|----------------------|---------|-----------|
| How fast can this storage serve the DSC patterns? | `./run-scenario.sh score <dir>` | ~20 min on fast classes, up to ~1.5 h on slow ones (three pooled repetitions) |

**score** is the measurement and what the report uses. It removes the
rate caps, so the storage does the best it can, and runs each phase three
times by default (`REPEATS=1` for quick iteration). One job
keeps a slow rate on purpose, because the rate is part of what it
models: the interactive viewer (a person browsing reads slowly, its
metric is how long one read takes).

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

`PHASES="a b c"` runs only the named phases. The names (as in
`run-scenario.sh`): `00-ingest-write`, `02-reorder`, `03-major-cycles`,
`04-column-rmw`, `06-imagechain-gen1`, `08-carta-tiles`, `09-qa-sparse`,
`10-smallfiles` — these are all 8, so `PHASES` only ever narrows the run.

`scale` (the third argument) is omitted by default: the read working set
is sized to 2× the node's RAM automatically — read from `/proc/meminfo`,
floored at 0.15 — so the benchmark is always cold-cache without manual
guessing. On the 16 GiB gornergrat nodes that resolves to ~0.16 (a
~30 GB set). Pass `full` for the real dataset sizes (1.0), or a number
for that fraction of the full DSC size.

`MAXTIME=SECONDS` puts a watchdog on every phase: one that runs longer
than this is killed and reported as failed, instead of hanging the whole
benchmark for hours. It is a guard, not a measurement method — a healthy
phase is never affected, because size-based phases stop on their own when
the workload is done. `MAXTIME=0` (default) disables it; the cluster Job
manifests set it to 3600.

## Profiles (profiles/*.fio)

| # | File | Pattern | Models | Key params |
|---|------|---------|--------|------------|
| 02 | `02-reorder-copy.fio` | Concurrent seq read + seq write of same size | wsclean `-reorder`, DDFacet `.ddfcache` | REORD (~0.5to1× data column) |
| 03 | `03-major-cycle-rereads.fio` | N repeated full sequential passes | Cotton–Schwab major cycles (2 passes/cycle) | CYCLES, JOBS |
| 04 | `04-column-rmw.fio` | 1 GB-chunk interleaved column read + write | DP3 applybeam, Crystalball MODEL_DATA, casacore subtract (100k-row chunks) | REORD |
| 06 | `06-fits-image-chain.fio` | NIMG files read/written 8-way, per generation | blank→convolve→avg→cube chains, PyBDSF slice fan-out | NIMG_W, IMGSZ (one generation) |
| 08 | `08-carta-random-tiles.fio` | Random 256 KB reads, rate-limited, 4 sessions | CARTA interactive visualisation | 300 s fixed runtime |
| 09 | `09-staging-ingest.fio` | 250 MB/s network-rate write | Rucio download | COLA |
| 10 | `10-smallfile-monitor.fio` | PNG storm (2000×128 KB) + catalogues | per-source plots, catalogue writes | (n/a) |
| 11 | `11-sparse-baseline-qa.fio` | Random 8 KB reads, <1% of volume | single-baseline TaQL QA, shadems | QA |

The runner strips the `rate=` caps, so the storage does the best it can.
The one exception is the phase where the rate is part of what is
modelled: the interactive viewer (a person browsing reads slowly, judged
on single-read latency). Job-file headers document the profiles.

## Reports

