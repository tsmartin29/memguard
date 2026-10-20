# Changelog

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
