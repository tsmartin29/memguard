# Changelog

## 2026-10-21 (usage-limit prompt)

- Usage-limit prompt for Claude Code plans. New `usagegate.py`: once per weekly window, when weekly usage reaches `usage_threshold_pct` (default 95) or paid extra usage starts (weekly at 100 %, or the `extra_spent` figure rises), it shows a macOS dialog and runs the optional `notify_command`. Three choices: keep going, pause new agent work until the weekly reset, pause heavy jobs only. Dismissing it, or 10 minutes of silence, means no action.
- The choice is written to `usage_decision.json` and clears itself at the reset. `heavyrun` refuses new jobs with exit code 75 ("pause new agent work", both lanes, queued waiters included) or holds the heavy lane ("pause heavy jobs only"); the dispatcher starts nothing meanwhile. Jobs matching `priority_pattern` are exempt. Running jobs are never touched. The stall alert names "usage pause" as the reason.
- New `usage_statusline.py`, a Claude Code `statusLine` hook that fills `usage.json` with the weekly and 5-hour numbers from the `rate_limits` Claude Code passes it. `usage.json` is the only interface, so any script can add `extra_enabled`, `extra_spent` and `currency`.
- New options: `usage_prompt` (true), `usage_threshold_pct` (95), `notify_command` (empty; shell command with `$MEMGUARD_MESSAGE` and `$MEMGUARD_TITLE`, for example an ntfy push). `MEMGUARD_NO_DIALOG=1` makes sure no dialog is ever shown.
- Tests: 130 (61 new) plus one opt-in end-to-end test. The suite sets `MEMGUARD_NO_DIALOG=1` and a test proves `osascript` is never started, including by `--test-dialog`.

## 2026-10-21 (timing lane)

- Timing lane: `HEAVY_TIMING=1 heavyrun "<label>" <command...>` for benchmarks, load tests and latency or jitter measurements. A quiet window holds new heavy and light starts until the other running jobs finish (at most `HEAVY_TIMING_MAX_WAIT` minutes, default new option `timing_max_wait_minutes` = 20), then holds new starts while the timing job runs. Running jobs are never paused or killed for it; jobs matching `priority_pattern` are never held.
- Host baseline: new `timing_probe.py` (standard library only) runs about 10 s before and after the command and exports `HOST_BASELINE_JITTER_P99_MS`, `HOST_BASELINE_JITTER_P50_MS`, `HOST_TIMER_SLACK_P99_MS`, `HOST_TIMER_SLACK_P50_MS`, `HOST_LOADAVG_1M`, `HOST_MEM_FREE_PCT`, `HOST_MEM_PRESSURE_LEVEL`, `HOST_OTHER_JOBS`, `HOST_PRIORITY_RUNNING` and `HOST_BASELINE_BUSY` (1 when baseline p99 is above 2 ms or a priority job is running). Summaries go to `heavy_queue.log`.
- `heavyrun --selftest-timing`: end-to-end dry run of the lane in a scratch queue with dummy jobs. `HEAVYRUN_POLL` sets the seconds between queue checks (default 15; the tests use 1).
- `heavyrun` finds `timing_probe.py` through the `~/.local/bin` symlink; `install.sh` installs it. The stall alert names the quiet window as the reason when that is what a job waits for.
- Fixed: the stall alert read every ticket name as a nanosecond timestamp, but `heavyrun` writes milliseconds, so a waiting job could be reported as stalled the moment it queued. It now accepts seconds, milliseconds or nanoseconds.
- Tests: 69 (30 new) plus one opt-in end-to-end test (`MEMGUARD_RUN_SELFTEST=1`). The tests now clear inherited `HEAVY_*` variables so they pass when run through `heavyrun` itself.

## 2026-10-21

- Level 2 can run two jobs: new option `level2_two_jobs_gb` (default 8). With `level2_one_job`, a second job also keeps running at pressure level 2 while available memory is at least this much. `0` restores one job.
- Priority jobs are protected: new option `protect_priority_jobs` (default true). A job started by a `heavyrun` ticket whose label matches `priority_pattern` is not paused by pressure level 2; it is still paused under `pause_avail_gb` and killed under `kill_avail_gb`. Written after a long training job sat paused for hours at level 2 with 8 to 10 GB free.
- Tests: 39 (3 new).

## 2026-10-20

- Pressure level 2 no longer deadlocks the queue. New options `level2_one_job` (default true), `level2_run_margin_gb` (1) and `level2_resume_margin_gb` (2): one job may keep running at level 2 while available memory is above `pause_avail_gb` + the run margin, and when nothing is running the smallest paused job is resumed (deadlock breaker). `level2_one_job: false` restores the old "pause on any level 2".
- `manual_order` file: drag-and-drop re-ranking of the queue by a UI. Listed tickets swap among the positions they already hold; unlisted, unknown, duplicate and priority tickets keep their place.
- New `priority_pattern` option (regex on the queue label, default `priority`) for the stuck rule and `manual_order`.
- The light lane now follows the dispatcher: `memguard.py` writes `visible_light.txt` (rank.txt and `manual_order` apply) and `heavyrun` reads it. A light job that is alone starts at once; with no fresh list `heavyrun` falls back to first come, first served.
- Tests: 36 (22 new): level 2, deadlock breaker, manual order, priority pattern, stuck rule, light lane.

## 2026-10-19

- Swap no longer stalls the queue on its own: new jobs are held only above `swap_hard_gb`, or above `swap_hold_gb` while available memory is under `swap_low_avail_gb`.
- Stall alert: one notification (and a line in `stalled.txt`) when a queued job has waited `stall_alert_minutes`.

## 2026-10-19 (initial release)

- memguard RAM, swap and disk guard and the `heavyrun` job queue.
