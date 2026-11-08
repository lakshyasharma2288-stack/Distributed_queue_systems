# Week 7 Final Performance Report

**Audit date:** 2026-11-06  
**Status:** Benchmark comparison and documentation complete; see the reliability-evidence caveat in Section 8.

## 1. Executive summary

Week 7 measured the job queue under nine workload/worker configurations, investigated bottlenecks, and implemented two approved database-clock-read reductions:

1. Heartbeat updates now compute the stale-worker cutoff in the update statement.
2. Worker claim and finalization paths now use database-time expressions instead of standalone clock reads.

The uninstrumented before-and-after reports cover the same `generate_pdf` workload and benchmark harness. Across the nine cells, post-change mean throughput increased in seven and decreased in two. Successful-completion mean and P95 latency improved in seven cells and regressed in two. Worker claim and result-update average durations each decreased in eight cells and increased in one.

These are measured before/after differences, not proof that the code changes alone caused the differences. Runs were collected at different times on a local machine, sample counts are small, and some cells have substantial run-to-run variation. The evidence supports a generally favorable direction, but not a statistically conclusive or production-scale performance claim.

## 2. Benchmark methodology

- **Environment:** Windows; Python 3.10.19 virtual environment. CPU model, memory, PostgreSQL version, and Redis version were not captured.
- **Matrix:** 100, 500, and 1,000 jobs, each with 1, 2, and 4 workers; three post-change repetitions per cell.
- **Task:** The real `generate_pdf` handler through the existing dispatcher, with the same task type and payload in every run.
- **Execution:** FastAPI `TestClient`, the existing outbox publisher and Redis queue, and worker instances running as threads in one Python process. Worker polling in the benchmark is 10 ms.
- **Isolation:** Each run uses a temporary PostgreSQL schema, unique Redis queue key, and temporary PDF output directory. The harness cleans up those run-scoped resources.
- **Reports:** JSON and Markdown pairs are written to `benchmark-results/` with unique names. The report writer opens files exclusively, so an existing report is not overwritten.
- **Throughput:** Completed jobs divided by the harness's measured end-to-end duration.
- **Completion latency:** Per-job successful completion from submission to completion observed by database polling. It includes the polling observation delay.
- **Worker and handler durations:** Same-process spans measured with `time.perf_counter()`. `worker_total_ms` spans worker processing; `worker_claim_ms` and `worker_result_update_ms` are separately timed spans.
- **Publisher duration:** One batch-level duration per benchmark report. The report-level percentile is unavailable for a one-sample series; the comparisons below average the per-run batch durations across repetitions.
- **PDF verification:** The harness checks that each completed job's output path is inside the temporary output directory and that the file begins with `%PDF-`. Those directories are removed after the run. The audit therefore verifies the recorded counts; the generated PDF bytes are not retained for a second, post-run opening.
- **Clock domains:** Same-process spans use `perf_counter()`. PostgreSQL lifecycle intervals use PostgreSQL timestamps only; no interval combines those timestamps with the application-clock `published_at`.
- **Excluded evidence:** The three instrumented Phase 3 diagnostic runs are not compared with these uninstrumented benchmark runs.

P95 values in the tables are the arithmetic mean of the per-run P95 summaries, computed from the unrounded JSON values. Change percentages use `(post - baseline) / baseline * 100`; positive changes are favorable for throughput, while negative changes are favorable for latency/duration.

## 3. Original pre-optimization baseline

The baseline is the 26 uninstrumented JSON/Markdown report pairs timestamped 2026-11-06 13:40:32 through 14:15:47 UTC. The worker implementation files were modified later, at 14:42:16 UTC. The benchmark harness was last modified before the first baseline report. Baseline sample counts were three per cell except 1,000 jobs / 2 workers, which had two.

| Jobs | Workers | Runs | Mean throughput (jobs/s) | Completion avg (ms) | Completion P95 (ms) | Worker avg (ms) | Worker P95 (ms) | Handler avg (ms) | Handler P95 (ms) | Publisher (ms) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 1 | 3 | 26.072 | 2460.468 | 3012.070 | 24.528 | 44.038 | 5.278 | 10.649 | 1694.845 |
| 100 | 2 | 3 | 29.612 | 2217.497 | 2676.042 | 42.071 | 68.686 | 10.082 | 20.531 | 1219.986 |
| 100 | 4 | 3 | 29.164 | 2308.481 | 2739.923 | 81.058 | 123.825 | 21.756 | 36.100 | 1549.437 |
| 500 | 1 | 3 | 29.646 | 9359.536 | 12791.135 | 24.220 | 40.395 | 4.999 | 10.251 | 3418.826 |
| 500 | 2 | 3 | 31.985 | 8548.062 | 11650.595 | 44.714 | 79.514 | 10.687 | 21.154 | 3891.450 |
| 500 | 4 | 3 | 30.130 | 11617.259 | 13023.346 | 99.153 | 153.041 | 25.971 | 42.461 | 9006.131 |
| 1,000 | 1 | 3 | 24.445 | 21685.930 | 32466.357 | 31.929 | 64.184 | 6.526 | 14.751 | 6740.335 |
| 1,000 | 2 | 2 | 30.691 | 17840.412 | 24570.717 | 48.241 | 92.896 | 11.970 | 25.733 | 8763.192 |
| 1,000 | 4 | 3 | 27.976 | 25319.837 | 28194.345 | 106.710 | 171.165 | 27.909 | 45.877 | 19783.209 |

All baseline reports recorded the expected completed count, zero failed jobs, and the same number of verified PDFs as jobs: 13,400 total.

## 4. Implemented optimizations

- [registry.py](../app/worker/registry.py): removed the standalone database-time read from `update_worker_heartbeat`; the cutoff is expressed in the update, with SQLite-compatible time arithmetic.
- [worker.py](../app/worker/worker.py): removed the standalone clock reads in job claim and finalization. PostgreSQL lease fencing continues to use `clock_timestamp()`; SQLite-compatible time expressions remain.
- [test_worker.py](../tests/test_worker.py): added a focused SQL-capture test covering heartbeat, claim, and successful finalization without standalone clock-select statements.

No outbox, Redis, PDF handler, schema, migration, connection-pool, or worker-concurrency optimization was made in this phase.

## 5. Before-and-after results

Each cell shows the mean across its report runs. Completion metrics are successful-job submission-to-observed-completion latency.

### Throughput and completion latency

| Jobs | Workers | Runs B/P | Throughput B -> P (jobs/s; change) | Completion avg B -> P (ms; change) | Completion P95 B -> P (ms; change) |
|---:|---:|---:|---:|---:|---:|
| 100 | 1 | 3 / 3 | 26.072 -> 24.611 (-5.60%) | 2460.468 -> 2999.834 (+21.92%) | 3012.070 -> 3732.828 (+23.93%) |
| 100 | 2 | 3 / 3 | 29.612 -> 33.531 (+13.24%) | 2217.497 -> 1896.333 (-14.48%) | 2676.042 -> 2242.329 (-16.21%) |
| 100 | 4 | 3 / 3 | 29.164 -> 42.186 (+44.65%) | 2308.481 -> 1348.492 (-41.59%) | 2739.923 -> 1643.117 (-40.03%) |
| 500 | 1 | 3 / 3 | 29.646 -> 32.191 (+8.58%) | 9359.536 -> 8536.054 (-8.80%) | 12791.135 -> 11896.811 (-6.99%) |
| 500 | 2 | 3 / 3 | 31.985 -> 37.839 (+18.30%) | 8548.062 -> 7446.941 (-12.88%) | 11650.595 -> 9234.964 (-20.73%) |
| 500 | 4 | 3 / 3 | 30.130 -> 39.231 (+30.21%) | 11617.259 -> 9283.220 (-20.09%) | 13023.346 -> 10500.522 (-19.37%) |
| 1,000 | 1 | 3 / 3 | 24.445 -> 29.924 (+22.41%) | 21685.930 -> 17486.297 (-19.37%) | 32466.357 -> 25582.168 (-21.20%) |
| 1,000 | 2 | 2 / 3 | 30.691 -> 36.791 (+19.88%) | 17840.412 -> 14738.378 (-17.39%) | 24570.717 -> 19574.640 (-20.33%) |
| 1,000 | 4 | 3 / 3 | 27.976 -> 27.496 (-1.72%) | 25319.837 -> 25649.599 (+1.30%) | 28194.345 -> 30399.826 (+7.82%) |

### Worker and handler duration

| Jobs | Workers | Worker avg B -> P (ms; change) | Worker P95 B -> P (ms; change) | Handler avg B -> P (ms; change) | Handler P95 B -> P (ms; change) |
|---:|---:|---:|---:|---:|---:|
| 100 | 1 | 24.528 -> 29.055 (+18.46%) | 44.038 -> 49.285 (+11.91%) | 5.278 -> 7.140 (+35.29%) | 10.649 -> 14.106 (+32.46%) |
| 100 | 2 | 42.071 -> 28.006 (-33.43%) | 68.686 -> 38.351 (-44.17%) | 10.082 -> 7.170 (-28.89%) | 20.531 -> 12.069 (-41.22%) |
| 100 | 4 | 81.058 -> 44.783 (-44.75%) | 123.825 -> 61.973 (-49.95%) | 21.756 -> 13.350 (-38.64%) | 36.100 -> 21.102 (-41.54%) |
| 500 | 1 | 24.220 -> 21.061 (-13.04%) | 40.395 -> 29.802 (-26.22%) | 4.999 -> 4.507 (-9.85%) | 10.251 -> 8.716 (-14.97%) |
| 500 | 2 | 44.714 -> 32.397 (-27.55%) | 79.514 -> 44.560 (-43.96%) | 10.687 -> 8.223 (-23.06%) | 21.154 -> 14.112 (-33.29%) |
| 500 | 4 | 99.153 -> 65.753 (-33.68%) | 153.041 -> 95.890 (-37.34%) | 25.971 -> 18.478 (-28.85%) | 42.461 -> 28.462 (-32.97%) |
| 1,000 | 1 | 31.929 -> 23.615 (-26.04%) | 64.184 -> 34.874 (-45.67%) | 6.526 -> 5.334 (-18.26%) | 14.751 -> 12.268 (-16.84%) |
| 1,000 | 2 | 48.241 -> 37.882 (-21.47%) | 92.896 -> 54.282 (-41.57%) | 11.970 -> 9.968 (-16.72%) | 25.733 -> 18.371 (-28.61%) |
| 1,000 | 4 | 106.710 -> 98.559 (-7.64%) | 171.165 -> 195.396 (+14.16%) | 27.909 -> 28.911 (+3.59%) | 45.877 -> 58.385 (+27.26%) |

### Claim, result-update, and publisher duration

`worker_result_update_ms` measures the worker result-finalization span. Publisher duration is one batch-level observation per report.

| Jobs | Workers | Claim avg B -> P (ms; change) | Result update avg B -> P (ms; change) | Publisher B -> P (ms; change) |
|---:|---:|---:|---:|---:|
| 100 | 1 | 4.858 -> 5.416 (+11.48%) | 7.762 -> 8.912 (+14.82%) | 1694.845 -> 1275.836 (-24.72%) |
| 100 | 2 | 8.386 -> 5.364 (-36.04%) | 11.478 -> 8.012 (-30.20%) | 1219.986 -> 774.461 (-36.52%) |
| 100 | 4 | 15.426 -> 8.297 (-46.22%) | 20.038 -> 11.268 (-43.77%) | 1549.437 -> 480.603 (-68.98%) |
| 500 | 1 | 4.737 -> 4.344 (-8.29%) | 7.768 -> 6.380 (-17.87%) | 3418.826 -> 3027.197 (-11.46%) |
| 500 | 2 | 8.540 -> 6.307 (-26.16%) | 12.688 -> 9.294 (-26.75%) | 3891.450 -> 3836.853 (-1.40%) |
| 500 | 4 | 18.694 -> 12.535 (-32.95%) | 24.854 -> 16.718 (-32.73%) | 9006.131 -> 7307.526 (-18.86%) |
| 1,000 | 1 | 6.341 -> 4.805 (-24.22%) | 9.789 -> 6.863 (-29.89%) | 6740.335 -> 5627.458 (-16.51%) |
| 1,000 | 2 | 9.144 -> 7.344 (-19.68%) | 13.259 -> 10.226 (-22.87%) | 8763.192 -> 7079.631 (-19.21%) |
| 1,000 | 4 | 20.386 -> 18.120 (-11.11%) | 26.554 -> 24.516 (-7.67%) | 19783.209 -> 19546.147 (-1.20%) |

The 27 post-change reports record 14,400 completed jobs, zero failures, and 14,400 verified PDFs. Per workload/worker cell, the three runs report 300, 1,500, or 3,000 verified PDFs, matching the corresponding 300, 1,500, or 3,000 jobs. The baseline reports record 13,400 completed jobs, zero failures, and 13,400 verified PDFs.

## 6. Interpretation and optimization assessment

### Measured evidence

- Mean throughput increased in 7 of 9 cells; it decreased for 100 jobs / 1 worker and 1,000 jobs / 4 workers.
- Mean and P95 completion latency improved in 7 of 9 cells and regressed in those same two cells.
- Mean worker duration improved in 8 of 9 cells; worker P95 improved in 7 of 9.
- Mean PDF handler duration improved in 7 of 9 cells and regressed for 100 jobs / 1 worker and 1,000 jobs / 4 workers.
- Mean claim and result-update spans each improved in 8 of 9 cells. Their 100-job / 1-worker averages regressed. Result-update P95 also regressed for 100 jobs / 1 worker and 1,000 jobs / 4 workers.
- Mean publisher duration was lower in all 9 cells, but publisher code was not changed; these measurements are not evidence that the worker clock changes directly optimized the publisher.

### Repeatability and uncertainty

All repetitions are retained; none was discarded. Throughput ranges show substantial variation in some cells:

| Jobs | Workers | Baseline throughput range | Post throughput range | Post range / post mean |
|---:|---:|---:|---:|---:|
| 100 | 1 | 22.28-28.04 | 12.45-33.16 | 84.15% |
| 100 | 2 | 19.71-34.80 | 27.77-37.29 | 28.40% |
| 100 | 4 | 23.15-34.08 | 41.50-42.82 | 3.12% |
| 500 | 1 | 27.84-31.74 | 30.57-33.63 | 9.50% |
| 500 | 2 | 27.12-36.42 | 35.71-39.35 | 9.63% |
| 500 | 4 | 24.07-35.65 | 34.84-42.15 | 18.64% |
| 1,000 | 1 | 20.38-27.24 | 28.93-30.46 | 5.12% |
| 1,000 | 2 | 30.59-30.79 | 32.84-39.56 | 18.26% |
| 1,000 | 4 | 21.66-34.20 | 20.20-35.48 | 55.56% |

The post-change 100-job / 1-worker cell includes a low 12.45 jobs/s run; the 1,000-job / 4-worker cell includes a 20.20 jobs/s run. Both remain included in the means. There are only two baseline runs for 1,000 jobs / 2 workers. No confidence intervals, randomized A/B ordering, or formal significance test were performed. Therefore the seven positive throughput changes are directional, not statistically conclusive. Four workers are not uniformly best: their throughput is strong for some cells but their worker/handler per-job durations are generally higher as workloads grow. Two or four workers should not be declared universally optimal from this local sample.

### Did the database-clock optimizations work?

- **Implementation-level result:** The approved code removes the standalone clock-read statements from the heartbeat update, job claim, and job finalization paths. The focused SQL-capture unit test verifies no standalone SQLite clock SELECT in those tested operations.
- **Measured proxy:** Claim and result-update average durations decreased in 8 of 9 workload/worker cells. Worker total duration decreased in 8 of 9.
- **Not measured:** The benchmark reports do not capture total SQL statement counts, database round-trip counts, or isolated PostgreSQL server execution time. No numeric reduction in database SELECT count can be claimed from these reports. The SQL-capture test is not a before/after PostgreSQL statement-count benchmark.
- **Conclusion:** The duration results are consistent with reduced per-operation overhead, but do not prove that the clock-read removal caused the observed end-to-end changes. Overall throughput and latency are mixed, and host/load variation remains a confounder.

## 7. Remaining evidence-backed concerns and limitations

- The benchmark is in-process and uses threads, not separate worker processes or real HTTP sockets. It is not a production-scale load test.
- Four-worker worker/handler averages remain higher than one-worker values for the 500- and 1,000-job cases. This is measured; CPU saturation, filesystem contention, or scheduling overhead are possible explanations but were not isolated.
- The 1,000-job / 4-worker post-change completion P95 and handler P95 are worse than baseline. This is a measured regression in that cell, though the throughput and duration ranges overlap substantially.
- CPU, memory, pool waits, SQL statement counts, transaction counts, and PostgreSQL server time were not sampled during these 27 runs.
- The reports do not separate ReportLab CPU work from filesystem cost.
- Outbox optimization (Proposal 3) remains unimplemented. Publisher duration comparisons alone do not justify changing it.

No additional optimization is recommended from this comparison alone. Preserve atomic claiming, leases, fencing, heartbeat behavior, retries, idempotency, and crash recovery.

## 8. Reliability verification and audit scope

Previously reported implementation validation:

- 26 focused worker/recovery tests passed.
- 37 broader worker/recovery/retry/database-safety tests passed.
- The supplied project handoff reports 11 PostgreSQL/Redis integration tests passed after the implementation.

This final audit did not rerun tests or benchmarks. In an earlier attempt from the current agent execution environment, three selected integration tests were skipped because the isolated URL variables were not visible to that process. Thus, the 11-pass integration result is recorded as supplied by the handoff, not independently reverified in this documentation audit.

The report audit parsed all 53 root JSON reports and all 53 paired Markdown reports: 26 baseline pairs and 27 post-change pairs. Every Markdown report matched its JSON workload, outcomes, and latency summaries; no pair mismatches were found. Every JSON report had completed jobs equal to the requested workload, zero failures, published count equal to workload, and verified PDF count equal to workload. The diagnostic reports remain under `benchmark-results/phase3-diagnostics/` and were not altered.

No production code, tests, schemas, migrations, `.env`, README, or benchmark methodology was changed for this audit. Only this final documentation file was added. No Git operations were performed.

## 9. Final conclusion and Week 7 status

The two approved optimizations have encouraging, but not conclusive, measured support: claim and result-update average spans improved in eight of nine cells, and seven cells improved in throughput and completion latency. Two cells regressed in the end-to-end metrics, and high variation in 100 jobs / 1 worker and 1,000 jobs / 4 workers limits confidence. The data do not justify claiming a guaranteed or statistically significant overall performance improvement.

**Week 7 performance comparison and documentation: complete.** The nine-cell post-change matrix is present with three runs per cell, all 27 reports record successful completion and verified PDFs, and all prior baseline/diagnostic files are preserved. Reliability results are recorded as previously reported, with the integration-evidence caveat above.

### Completion checklist

- [x] Original baseline identified from report contents and timestamps.
- [x] All 27 post-change JSON and Markdown report pairs audited.
- [x] All nine workload/worker combinations have three post-change runs.
- [x] Completed jobs, failure counts, and reported PDF verification counts checked.
- [x] Before/after averages, P95s, and percentage changes calculated.
- [x] Measured evidence distinguished from causal assumptions.
- [x] Existing benchmark and Phase 3 diagnostic reports preserved.
- [x] Final performance report created in the existing project.
- [x] Previously reported reliability evidence recorded with its verification limitation.

## Appendix A. Post-change report inventory

Each row links the JSON and Markdown report pair for one execution.

| Repetition | Jobs | Workers | Report pair |
|---:|---:|---:|---|
| 1 | 100 | 1 | [JSON](../benchmark-results/week7_20261106T151442967513Z_7b0b1432f7a34cb4ac71c01bfa4f45d1.json) / [Markdown](../benchmark-results/week7_20261106T151442967513Z_7b0b1432f7a34cb4ac71c01bfa4f45d1.md) |
| 1 | 100 | 2 | [JSON](../benchmark-results/week7_20261106T151453247901Z_714804f2fd104220a3986aae5b111b01.json) / [Markdown](../benchmark-results/week7_20261106T151453247901Z_714804f2fd104220a3986aae5b111b01.md) |
| 1 | 100 | 4 | [JSON](../benchmark-results/week7_20261106T151503190552Z_14a89ec693c54a3aa283471f246188a3.json) / [Markdown](../benchmark-results/week7_20261106T151503190552Z_14a89ec693c54a3aa283471f246188a3.md) |
| 1 | 500 | 1 | [JSON](../benchmark-results/week7_20261106T151513414596Z_108d9a68aa7f4b72858d325731964c69.json) / [Markdown](../benchmark-results/week7_20261106T151513414596Z_108d9a68aa7f4b72858d325731964c69.md) |
| 1 | 500 | 2 | [JSON](../benchmark-results/week7_20261106T151551684277Z_d9e89e566f064cf0b622b26ac3b4c442.json) / [Markdown](../benchmark-results/week7_20261106T151551684277Z_d9e89e566f064cf0b622b26ac3b4c442.md) |
| 1 | 500 | 4 | [JSON](../benchmark-results/week7_20261106T151627458068Z_ae58c71f9fa1466e967aa148680c4816.json) / [Markdown](../benchmark-results/week7_20261106T151627458068Z_ae58c71f9fa1466e967aa148680c4816.md) |
| 1 | 1,000 | 1 | [JSON](../benchmark-results/week7_20261106T151702381812Z_0238068a73d340b09e5679587416d3ab.json) / [Markdown](../benchmark-results/week7_20261106T151702381812Z_0238068a73d340b09e5679587416d3ab.md) |
| 1 | 1,000 | 2 | [JSON](../benchmark-results/week7_20261106T151817333601Z_12943bf249e04f78bc7cf0b2bcf0f36d.json) / [Markdown](../benchmark-results/week7_20261106T151817333601Z_12943bf249e04f78bc7cf0b2bcf0f36d.md) |
| 1 | 1,000 | 4 | [JSON](../benchmark-results/week7_20261106T151926151043Z_9ecbb7b8f2b943c0849e0ae158716b93.json) / [Markdown](../benchmark-results/week7_20261106T151926151043Z_9ecbb7b8f2b943c0849e0ae158716b93.md) |
| 2 | 100 | 1 | [JSON](../benchmark-results/week7_20261106T152036030078Z_4520ffd7ee6f4e98a7f4fede5a8d54cb.json) / [Markdown](../benchmark-results/week7_20261106T152036030078Z_4520ffd7ee6f4e98a7f4fede5a8d54cb.md) |
| 2 | 100 | 2 | [JSON](../benchmark-results/week7_20261106T152050930713Z_b827e8586b6046b8965b82d795db8134.json) / [Markdown](../benchmark-results/week7_20261106T152050930713Z_b827e8586b6046b8965b82d795db8134.md) |
| 2 | 100 | 4 | [JSON](../benchmark-results/week7_20261106T152101066416Z_89c9d4f72ea14615aa7793254193f719.json) / [Markdown](../benchmark-results/week7_20261106T152101066416Z_89c9d4f72ea14615aa7793254193f719.md) |
| 2 | 500 | 1 | [JSON](../benchmark-results/week7_20261106T152109384620Z_aaeb065c1b184bdc96f09ee0ad69df02.json) / [Markdown](../benchmark-results/week7_20261106T152109384620Z_aaeb065c1b184bdc96f09ee0ad69df02.md) |
| 2 | 500 | 2 | [JSON](../benchmark-results/week7_20261106T152153226221Z_6ca4b4d7960043b78faca498bfae78cb.json) / [Markdown](../benchmark-results/week7_20261106T152153226221Z_6ca4b4d7960043b78faca498bfae78cb.md) |
| 2 | 500 | 4 | [JSON](../benchmark-results/week7_20261106T152228091170Z_f0689290bba04abab1ab2a4409f66e59.json) / [Markdown](../benchmark-results/week7_20261106T152228091170Z_f0689290bba04abab1ab2a4409f66e59.md) |
| 2 | 1,000 | 1 | [JSON](../benchmark-results/week7_20261106T152303577587Z_8d87dc996be14b0dbf9e8cece6dc00f4.json) / [Markdown](../benchmark-results/week7_20261106T152303577587Z_8d87dc996be14b0dbf9e8cece6dc00f4.md) |
| 2 | 1,000 | 2 | [JSON](../benchmark-results/week7_20261106T152412681594Z_550777c320d94219bcaf69b757917cdb.json) / [Markdown](../benchmark-results/week7_20261106T152412681594Z_550777c320d94219bcaf69b757917cdb.md) |
| 2 | 1,000 | 4 | [JSON](../benchmark-results/week7_20261106T152508554803Z_53616b17b2e64ebfbc3f1eeb2c54b78b.json) / [Markdown](../benchmark-results/week7_20261106T152508554803Z_53616b17b2e64ebfbc3f1eeb2c54b78b.md) |
| 3 | 100 | 1 | [JSON](../benchmark-results/week7_20261106T152615209133Z_72447f0d7e9a4d758fb2805ec585cc03.json) / [Markdown](../benchmark-results/week7_20261106T152615209133Z_72447f0d7e9a4d758fb2805ec585cc03.md) |
| 3 | 100 | 2 | [JSON](../benchmark-results/week7_20261106T152627165050Z_ce31918f948849969d9a7554989c77d4.json) / [Markdown](../benchmark-results/week7_20261106T152627165050Z_ce31918f948849969d9a7554989c77d4.md) |
| 3 | 100 | 4 | [JSON](../benchmark-results/week7_20261106T152637114184Z_d20141114c7a4e9b9e7006b1f9a05ad7.json) / [Markdown](../benchmark-results/week7_20261106T152637114184Z_d20141114c7a4e9b9e7006b1f9a05ad7.md) |
| 3 | 500 | 1 | [JSON](../benchmark-results/week7_20261106T152647790808Z_434bd42e2941431b8163195b71bfd4f4.json) / [Markdown](../benchmark-results/week7_20261106T152647790808Z_434bd42e2941431b8163195b71bfd4f4.md) |
| 3 | 500 | 2 | [JSON](../benchmark-results/week7_20261106T152725559633Z_89e419d481454c8ba159e09e47cd90cc.json) / [Markdown](../benchmark-results/week7_20261106T152725559633Z_89e419d481454c8ba159e09e47cd90cc.md) |
| 3 | 500 | 4 | [JSON](../benchmark-results/week7_20261106T152803054502Z_1cb2f913f2ed44b2a1731b27295fc33e.json) / [Markdown](../benchmark-results/week7_20261106T152803054502Z_1cb2f913f2ed44b2a1731b27295fc33e.md) |
| 3 | 1,000 | 1 | [JSON](../benchmark-results/week7_20261106T152836011903Z_55fc6a3c64be4ee1b390212e9c8518a6.json) / [Markdown](../benchmark-results/week7_20261106T152836011903Z_55fc6a3c64be4ee1b390212e9c8518a6.md) |
| 3 | 1,000 | 2 | [JSON](../benchmark-results/week7_20261106T153019206272Z_ab0563bbe37445e78671721642531beb.json) / [Markdown](../benchmark-results/week7_20261106T153019206272Z_ab0563bbe37445e78671721642531beb.md) |
| 3 | 1,000 | 4 | [JSON](../benchmark-results/week7_20261106T153128913166Z_202e9ef03e9b4bae8718135273a38fd1.json) / [Markdown](../benchmark-results/week7_20261106T153128913166Z_202e9ef03e9b4bae8718135273a38fd1.md) |
