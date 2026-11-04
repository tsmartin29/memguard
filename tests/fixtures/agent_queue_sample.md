# Agent queue (sample)

Status: RUNNING / QUEUED / HOLD / DONE. Lines outside the two tables below are ignored.

## Running

| # | Job | Started | ETA |
|---|---|---|---|
| R1 | Release notes draft for the 2.4 build | 9:10 AM | 10:30 AM |
| R2 | Benchmark the new decoder on the 4 GB test set | 9:40 AM | 11:00 AM |
| R3 | Migrate the docs site to the new theme | 9:55 AM | 11:30 AM |
| ~~R4~~ | (finished, struck through) Lint pass | 8:00 AM | 9:00 AM |
| — | Nightly model training (not an agent; checked hourly) | 1:00 AM | 1-hour check |

## Queued (next in line, top first)

| # | Job | Needs first |
|---|---|---|
| Q1 | **Crash triage**: group the last week of crash logs by signature and open one issue per group | none |
| Q2 | Upgrade the build image to the new toolchain and re-run the full test suite | R1 |
| ~~Q3~~ | (started as R4) Lint pass | none |
| Q4 | Write the migration guide for plug-in authors | R3 |
| Q5 | Dependency audit, licences and known advisories | R2 |
| QA | Quarterly cleanup of the issue tracker | owner |

## On hold

| # | Job | Why |
|---|---|---|
| Q9 | Hold item that must not be counted | waiting on a decision |
| R9 | Hold item that must not be counted either | waiting |
