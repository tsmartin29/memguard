# memguard

A small launchd guard for Macs that run many AI-agent sessions at once: it pauses or stops runaway background jobs before the Mac runs out of RAM, swap or disk, and gives agents a one-at-a-time queue for heavy work.

Made by Tommy Martin. Built while running [ClubPlugins](https://clubplugins.com), where it guards the Mac that runs all our Claude Code sessions.

## Why it exists

Run 20-30 Claude Code sessions on a 24 GB laptop and sooner or later two of them start a training run, a big render or a test suite at the same time. macOS swaps, the disk fills, and the whole machine crashes. memguard is the cheap fix: it watches memory every 15 seconds, only touches jobs that were started by an agent session, and tells the agents (through a rules file) to line up their heavy jobs in a queue.

## What is in the box

| File | What it does |
|---|---|
| `memguard.py` | The guard. One pass per run (launchd runs it every 15 s). Pauses (`SIGSTOP`) the biggest session job when available memory is low, kills it when memory is critical, resumes jobs one by one when memory recovers, stops the lowest-ranked paused job if memory stays low too long, holds new heavy jobs when the disk is nearly full or swap is high while memory is short, alerts you once when a queued job has waited too long, copes with macOS sitting at pressure level 2 for hours, and dispatches the `heavyrun` queue (both lanes, with an optional drag-and-drop `manual_order`, and the timing lane's quiet window). |
| `heavyrun` | Wrapper agents use to run heavy work: `heavyrun "<session>: <job>" <command...>`. Up to 3 jobs at once (memory permitting) plus a light lane for short small jobs. Runs commands with `nice -n 15`. `HEAVY_TIMING=1` adds the timing lane (below); `heavyrun --selftest-timing` is a dry run of it. |
| `usagegate.py` | The usage-limit prompt (Claude Code plans): asks what to do when weekly usage gets high, and holds the queue if you say so. Called by `memguard.py` every pass and by `heavyrun`. |
| `usage_statusline.py` | Claude Code `statusLine` hook that feeds `usage.json` for `usagegate.py`. |
| `timing_probe.py` | Host-noise probe that `heavyrun` runs before and after a timing job (~10 s each). |
| `config.example.json` | Every threshold and pattern, with defaults for a 24 GB Mac. |
| `rank.example.txt` | Optional job order, plus a `PARK` section for jobs that should always go last. |
| `RULES.md` | A rules block to paste into your agents' standing instructions. |
| `install.sh` | Copies files to `~/.config/memguard`, writes the launchd plist, loads it. |
| `launchd/com.example.memguard.plist.template` | The launchd job (every 15 s). |
| `tests/` | Unit tests (dispatcher ordering, rank file, manual order, priority, disk/swap hold, level-2 handling, stuck rule, job detection, light lane, heavyrun, timing lane, usage-limit prompt). No test ever shows a dialog. |
| `CHANGELOG.md` | What changed in each update. |

## Requirements

macOS (uses `vm_stat`, `memory_pressure`, `sysctl`, `top`, `launchctl`, BSD `stat`), the `/usr/bin/python3` that ships with macOS (3.9+), `perl` (ships with macOS; used for a millisecond timestamp).

## Install

```bash
git clone <this repo> memguard && cd memguard
python3 -m unittest discover -s tests          # optional: run the tests first
./install.sh                                   # installs to ~/.config/memguard and loads the launchd job
python3 ~/.config/memguard/memguard.py --status
```

`./install.sh --print` shows the launchd plist without installing anything. `./install.sh --uninstall` unloads the job and moves the plist to the Trash. Make sure `~/.local/bin` is on your `PATH` (the installer links `heavyrun` there).

Edit `~/.config/memguard/config.json` to change thresholds. Set `MEMGUARD_HOME` (or `MEMGUARD_CONFIG`) if you want everything somewhere other than `~/.config/memguard`; export it for sessions that call `heavyrun` too.

## Use

```bash
heavyrun "docs-session: render all" python3 render.py          # heavy lane
HEAVY_LIGHT=1 heavyrun "app-session: build" make -j2           # light lane
HEAVY_PRIORITY=1 heavyrun "release: package" ./package.sh      # front of the queue, use sparingly
tail ~/.config/memguard/heavy_queue.log                        # who is running, who is waiting
tail ~/.config/memguard/memguard.log                           # what the guard did and why
python3 ~/.config/memguard/memguard.py --dry-run               # log what it WOULD do, send no signals
```

Then paste `RULES.md` into your global `CLAUDE.md` so the agents actually use the queue.

### What it will and will not touch

It only acts on processes that have a Claude Code session (matched by `session_pattern`) or `heavyrun` as an ancestor. Browsers, the Claude app, Finder, system processes and anything in `protect_pattern` / `protect_extra` are skipped (add your DAW, IDE, etc. to `protect_extra`). Headless browsers started by a session do count as jobs. Killing is a last resort: the biggest session job gets SIGTERM, then SIGKILL, and its queue waiter exits so the session can re-queue it.

### Pressure level 2 and manual order

macOS can report memory pressure "warn" (level 2) for hours while several GB are still free. If every level 2 paused the last running job, a paused pile would never restart. So with `level2_one_job` (default on) one job keeps running at level 2 while available memory is above `pause_avail_gb` + `level2_run_margin_gb`, and when nothing is running the smallest paused job is resumed once available memory is above `pause_avail_gb` + `level2_resume_margin_gb`. Set `level2_one_job` to `false` to pause on any level 2.

`manual_order` (optional, in the memguard home folder) lets a UI re-rank the queue: ticket file names from `queue*/` and `queue*_hold/`, one per line (text after whitespace is just a label). Listed tickets swap among the places they already hold in the `rank.txt` order; everything else keeps its place, and new tickets still follow `rank.txt`. Tickets whose label matches `priority_pattern` (default `priority`) and `0-` tickets never move; the stuck rule also stops them last. Set `priority_pattern` to your own label convention, or to `""` for none.

### Timing lane

For honest benchmark numbers on a shared machine. A latency or jitter figure measured while other agent jobs load the Mac can miss its target by a millisecond or more, and the same test passes on a quiet Mac. `HEAVY_TIMING=1 heavyrun ...` runs the job in the heavy lane behind a quiet window and tells the job how noisy the host was.

- **Quiet window.** When a timing job is next in line, no new heavy or light job starts until the other running jobs finish, or until `timing_max_wait_minutes` (default 20) passes; then it starts, and new starts stay held until it ends. Running jobs are never paused or killed for it, and jobs whose label matches `priority_pattern` are never held (the timing job runs beside them). Memory, disk and swap holds still apply.
- **Host baseline.** Just before your command, `timing_probe.py` runs for about 10 s: timer loops at 2.5 ms and 23 ms, measured two ways, plus load average, memory and the other running queue jobs. It runs again after the command. Both summaries go to `heavy_queue.log` (`BASELINE-start`, `BASELINE-end`).
- **What your command sees:**

| Variable | Meaning |
|---|---|
| `HOST_BASELINE_JITTER_P99_MS`, `HOST_BASELINE_JITTER_P50_MS` | How late a no-slack timer (kqueue `NOTE_CRITICAL`) plus 0.2 ms of CPU work finishes. This is host noise: about 0.1 to 0.3 ms p99 on a quiet Mac. |
| `HOST_TIMER_SLACK_P99_MS`, `HOST_TIMER_SLACK_P50_MS` | What a plain `sleep()` gets. macOS coalesces timers for processes started by an agent session, up to 5 ms, even on an idle Mac. That floor is the OS, not load. |
| `HOST_LOADAVG_1M`, `HOST_MEM_FREE_PCT`, `HOST_MEM_PRESSURE_LEVEL` | Load average and memory when the probe ran. |
| `HOST_OTHER_JOBS` | How many other queue jobs were running. |
| `HOST_PRIORITY_RUNNING` | 1 if a job matching `priority_pattern` was running (it is never held, so it can load the host). |
| `HOST_BASELINE_BUSY` | 1 if baseline p99 was above 2 ms, a priority job was running, or the probe failed. Treat the result as inconclusive. |

```bash
HEAVY_TIMING=1 heavyrun "bench: frame loop" python3 bench.py                         # wait up to 20 min for a quiet machine
HEAVY_TIMING=1 HEAVY_TIMING_MAX_WAIT=5 heavyrun "bench: frame loop" python3 bench.py # wait at most 5 min
```

Inside `bench.py`, report your own figure next to the baseline, and call it inconclusive when the host was busy:

```python
import os
p99 = float(os.environ["HOST_BASELINE_JITTER_P99_MS"])
busy = os.environ["HOST_BASELINE_BUSY"] == "1"
print(f"frame jitter p99 {measured:.2f} ms (host noise p99 {p99:.2f} ms)" + ("  INCONCLUSIVE: host busy" if busy else ""))
```

Never loosen a target to make a result pass: report what your code adds on top of the baseline. `heavyrun --selftest-timing` runs the lane end to end in a scratch queue with dummy jobs (about 40 s) and touches no real job.

### Usage-limit prompt (Claude Code plans)

On a Claude subscription the weekly limit can run out in the middle of a busy week, and work then rolls onto paid extra usage without anyone noticing. memguard can ask you first.

**What it does.** `memguard.py` calls `usagegate.py` on every pass. It reads one local file, `usage.json` in the memguard folder (no credentials, no cookies, no network calls). Once per weekly window it shows a macOS dialog, and runs your optional `notify_command` (a phone push, for example), when either:

- weekly usage reaches `usage_threshold_pct` (default 95), or
- paid extra usage starts: weekly usage is at 100 %, or the `extra_spent` figure in `usage.json` rises.

The dialog has three choices and runs in a detached child process, so memguard's memory checks never wait on it:

| Choice | Effect until the weekly reset |
|---|---|
| Keep going on credits | Nothing changes. |
| Pause new agent work until reset | `heavyrun` refuses new jobs (exit code 75 and a clear message), in both lanes. Jobs already waiting in the queue exit 75 too; running jobs finish. |
| Pause heavy jobs only | The heavy lane waits (`heavyrun` holds, the dispatcher starts nothing); the light lane and agents continue. |

Jobs whose queue label matches `priority_pattern` are exempt from both pauses. Your choice is written to `usage_decision.json` and clears itself at the weekly reset. Dismissing the dialog does nothing, and so does leaving it alone: after 10 minutes it closes itself and counts as no action.

**Where `usage.json` comes from.** Claude Code passes `rate_limits.seven_day` and `rate_limits.five_hour` to a `statusLine` command, so the weekly numbers are free. Add the hook to `~/.claude/settings.json` (the hook prints nothing; set `USAGE_STATUSLINE_NEXT` to a command if you already have a status line and want to keep it):

```json
{
  "statusLine": { "type": "command", "command": "/usr/bin/python3 -I ~/.config/memguard/usage_statusline.py" }
}
```

The status line JSON has no extra-usage spend. If you want the "extra usage started" trigger on the spend figure, any script of yours can write it: `usage.json` is the whole interface, and the hook keeps the fields it does not know. All keys are optional:

```json
{"updated": "2026-10-21T17:45:00Z", "weekly_pct": 100.0, "weekly_resets_at": "2026-10-22T12:00:00Z", "five_hour_pct": 0.0,
 "extra_enabled": true, "extra_spent": 12.50, "currency": "USD"}
```

Without a spend feed the prompt still fires at the threshold and at 100 %.

**Config** (`config.json`): `usage_prompt` (true; false turns the whole thing off), `usage_threshold_pct` (95) and `notify_command` (empty). `notify_command` runs through the shell with the message in `$MEMGUARD_MESSAGE`, the title in `$MEMGUARD_TITLE`, and the message on stdin, for example `"curl -s -H \"Title: $MEMGUARD_TITLE\" -d \"$MEMGUARD_MESSAGE\" https://ntfy.sh/your-topic"`. It is used only for this prompt.

```bash
python3 ~/.config/memguard/usagegate.py --status      # usage.json, state and the decision in force
python3 ~/.config/memguard/usagegate.py --dry-run     # print what it would ask, show nothing, send nothing (it does record the trigger)
python3 ~/.config/memguard/usagegate.py --ask-now     # show the dialog now from the current usage.json
python3 ~/.config/memguard/usagegate.py --gate "my-session: job"   # exit 0 go, 75 refuse, 76 heavy lane held
```

Tell your agents about it with `RULES.md` rule 12. For automated tests set `MEMGUARD_NO_DIALOG=1`: no dialog can then be shown, including `--test-dialog`, and the test suite sets it itself.

### Tuning for your Mac

The defaults suit a 24 GB machine: pause under 6 GB available, kill under 3 GB, resume over 9 GB. On 16 GB try `pause_avail_gb 4`, `kill_avail_gb 2`, `resume_avail_gb 6`, `slot2_avail_gb 8`, `slot3_avail_gb 12`. To force a strict one-at-a-time queue set `max_heavy_jobs` to 1 and `slot2_avail_gb` very high. Run `--dry-run` for a day to see what it would have done.

## Known limits

- Pausing a job keeps its memory; macOS can swap it out, but it is not freed. That is what the "stuck" rule is for.
- Memory numbers come from macOS tools whose output could change between OS versions; `--status` is a quick check.
- It is a safety net, not a scheduler. Agents still need to follow `RULES.md`.

## Licence

MIT. See `LICENSE`.
