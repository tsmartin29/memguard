# Changelog

## 2026-11-05 (queue tracking via hooks, racing names, no screen animations)
- New **agentq** (`agentq.py`, `agentq`, linked as `agentq` and `queue`): Claude Code hooks (`SubagentStart`, `SubagentStop`, `PostToolUse` on `Agent`/`Task`) append every agent start and finish to `queue/agents.jsonl` and regenerate a dated `AGENT_QUEUE_<date>.md` view; `queue add` / `queue done` keep a PENDING backlog; `queue backfill` rebuilds a day from subagent transcripts. The hook fails open (always exits 0, errors to `hook_errors.log`). README: "Queue tracking via hooks". Tests: `tests/test_agentq.py`.
- Hangar Queue tab reads the agentq log first (`HANGAR_QUEUE_JSONL`), shows running / finished today / pending, and a **STALE** banner when the newest entry is older than 2 h in business hours (`HANGAR_QUEUE_STALE_HOURS`, `HANGAR_BUSINESS_HOURS`). The markdown fallback now also reads `## PENDING` and `## Finished` sections.
- Racing names: DRS (afterburner), Box Box (clean), Lights Out (save + push), Telemetry (backup), Pit Stop (all three); agent modes Safety Car 12, Race Pace 20, Quali Mode 25, Full Send 30. `scramble safety-car | race-pace | quali | full-send` added; the old verbs stay as aliases and `scramble.json` keeps its mode keys. `drs` is linked next to `afterburner`.
- Hangar art: original vector side profiles (a straight-six sports coupe for Safety Car / Race Pace, a V8-era single-seater for Quali Mode / Full Send), as the menu-bar template image and in colour in the panel; `HANGAR_ART=<dir>` exports SVG, PDF and 1x/2x PNG.
- Removed all screen animations, the A-10 icon and the flyover sounds. The only effect left is a short synthesised engine blip (2 s or less) on a click, with an **Engine sound** switch (`HANGAR_SOUND=off`). `HANGAR_ENGINE_WAV=<file>` writes it to a WAV. Tests: `tests/test_hangar_engine.py` (replaces `test_hangar_animations.py`).

## 2026-11-04 (Close Air Support and screen animations)
- New Scramble mode **Close Air Support (CAS)**: `scramble cas` keeps 30 agents running (`scramble warthog` is kept as an alias; `scramble.json` mode `cas`). Same disk rule as Surge: refuses under 10 GB free. Custom stays 1 to 40. Tile menu item "Close Air Support · 30", tile label "CAS · 30".
- Hangar menu bar: an original A-10 silhouette (vector, monochrome template image) sits next to the plane while the mode is CAS or the target is 30 or more.
- Hangar screen animations, one per action, all drawn and synthesised in the app (no image files, no recorded audio): Afterburner (sonic boom), Jettison (drop tank), Wheels Up (carrier launch, gear up), Black Box (recorder and beacon pings), Alpha Strike (three-ship V), CAP / Scramble / Surge (2 / 4 / 6-ship passes) and CAS (A-10 dive, gun burst, fire and booms). Click-through, no focus, Reduce Motion skips them, once per action and at most one per 20 s. New panel switches **Animations** and **Flyover sound**.
- `HANGAR_SHOT=<dir>` now also renders every scene offscreen (frame PNGs plus a strip per scene); `HANGAR_FLYOVER=<scene>` plays one for real and `HANGAR_FLYOVER_WAV=<file>` writes its sound to a WAV.
- Tests: `tests/test_hangar_animations.py` (sound length, level and determinism per scene) and CAS cases in `tests/test_scramble.py`.

## 2026-11-04 (Hangar Queue tab)
- Hangar: optional **Queue** tab. Set `HANGAR_QUEUE_FILE` (a file or a glob such as `~/notes/AGENT_QUEUE_*.md`, newest name wins) and it shows the running count, the queued count, the next three queued jobs and the file's age, amber when older than 45 minutes. It parses the `## Running` and `## Queued` markdown tables (`R<id>` / `Q<id>` rows, struck-through rows skipped). Read-only, no network, re-read at most every 60 s while the tab is open. With the variable unset there is no Queue tab.
- `HANGAR_QUEUE_PARSE=<file>` prints the parser's result as JSON and exits (used by the tests).
- Tests: `tests/test_hangar_queue.py` (7, compiles `Hangar.swift` into a scratch folder; skipped without `swiftc`) and a sample file in `tests/fixtures/`.

## 2026-11-04 (Scramble modes)
- Scramble now has agent-count modes: `scramble cap` (CAP, 12), `scramble on` (Scramble, 20, unchanged), `scramble surge` (25) and `scramble set N` (Custom, 1 to 40), each with an optional `[duration] [reason]`. `scramble.json` gains `mode` next to `target_agents`; `status` prints `mode`, `disk_free_gb` and `disk_low` in its JSON line.
- Disk safety: under 10 GB free, `status` warns; Surge and Custom above 20 refuse to turn on and print why. CAP, Scramble and Custom up to 20 still start, with the warning.
- Hangar: the Scramble tile is now a mode picker (CAP 12 / Scramble 20 / Surge 25 / Custom with a stepper / Off) in the same tile footprint; green glow when on, the label shows the count, amber "disk low" under 10 GB. New screenshots.
- Tests: `tests/test_scramble.py` (15, on a scratch state dir with a faked free-disk figure).

## 2026-11-03
- Autosave: memguard can run your snapshot command every 5 minutes (MEMGUARD_AUTOSAVE_CMD, MEMGUARD_AUTOSAVE_SECS).
- Scramble mode: `scramble on/off/status` (keep every agent slot filled from a queue file), Hangar Scramble tile, install links it.

## 2026-10-30 (afterburner, tidy, Hangar)

- Afterburner: new `afterburner.py` and the `afterburner` command (`on [2h] [reason]`, `off`, `status`, `caps`). While on, the heavy lane may run up to `afterburner_max_heavy` jobs (default cores - 2), still only as many as memory allows (new `mem_slots()`: the usual tiers, then `afterburner_extra_slot_gb` more per job); the light lane gets `afterburner_light_slots` (2); `heavyrun` uses `nice -n` `afterburner_nice` (10). `afterburner.json` carries `workers_per_agent` for agents. It drops back to normal by itself on memory pressure WARN, sustained load, low battery, an `afterburner_drop_apps` app in front or newly started, time up, or a stopped guard. The memory floors never change and dropping never touches a running job.
- `heavyrun` reads the caps from `afterburner.py caps` every poll (fails safe to normal), uses `heavy.slot4` and up in afterburner, cleans stale slots left from an afterburner run, and logs the nice level. `timing_probe.py` counts jobs in every slot.
- Graceful stop: a kill (critical memory or the stuck rule) now sends SIGTERM and waits up to `stop_grace_seconds` (30; `MEMGUARD_STOP_GRACE` overrides) for the job to save and exit before SIGKILL; 3 s when memory was already under `kill_avail_gb`, cut short under `stop_hard_avail_gb` (1.5). The log says `saved` or `forced`.
- Tidy: new `tidy.py` and the `tidy` command: clean regenerable build output under the configured roots to one dated Trash folder with a manifest (never `rm`), push unpushed branches fast-forward only with a secrets check, back up verified git bundles plus an rsync of your folders to an attached drive as one light-lane `heavyrun` job, then one summary line. Everything it may touch comes from the new `tidy` block in `config.json`; the defaults push nothing and back up nothing.
- Hangar: new optional SwiftUI menu-bar app in `hangar/` (an F-22 button for afterburner, Jettison, Wheels Up, Black Box, Alpha Strike = all three) with `build.sh` and screenshots. Clicking the plane opens a window kept inside the visible screen area; opening the app again shows it centered. Jettison and Black Box show the dry-run plan and wait for Confirm.
- Hangar Tower tab: a Cockpit / Tower picker. Tower shows the heavy and light lanes (running slots with pid check and run time, queued and held tickets, priority tickets starred), the paused-job count and the recent `heavy_queue.log` lines, refreshed every 5 s. Drag queued jobs to reorder them: Hangar writes memguard's `manual_order` file (priority tickets stay first). The memguard directory comes from `HANGAR_MEMGUARD_DIR`, else `MEMGUARD_HOME`, else `~/.config/memguard`. New Tower screenshots (made-up queue).
- Hangar call-signs: Tidy is now **Jettison**, Save + push **Wheels Up**, Backup **Black Box**, and the all-three button **Alpha Strike** (a red-orange V of three jets, "all at once: jettison → wheels up → black box"). Tabs are Cockpit and Tower. Screenshots regenerated.
- Hangar icons: Jettison shows a drawn falling bomb with speed lines (`DropTank`), Wheels Up a landing-gear wheel with an up chevron (`GearUp`), Black Box an orange flight recorder with a carry handle and stripes (`FlightRecorder`); all vector, no image files. Screenshots regenerated.
- `install.sh` installs and links `afterburner` and `tidy`; `memguard.py --status` shows the afterburner state; `RULES.md` rules 13 (afterburner) and 14 (tidy).
- Tests: 165 (34 new: afterburner, graceful stop, tidy on a scratch home, scratch volumes and a scratch Trash).

## 2026-10-21 (pid guard)

- `sig()` never signals pid -1 (every process the user owns), 0 (memguard's own process group), 1 (launchd) or memguard itself, even if a job record were corrupted. Prompted by anthropics/claude-code#104156, where an agent script's `kill(-1, SIGKILL)` took down every session and the desktop.
- Tests: 131 (1 new).

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
