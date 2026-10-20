# memguard

A small launchd guard for Macs that run many AI-agent sessions at once: it pauses or stops runaway background jobs before the Mac runs out of RAM, swap or disk, and gives agents a one-at-a-time queue for heavy work.

Made by Tommy Martin. Built while running [ClubPlugins](https://clubplugins.com), where it guards the Mac that runs all our Claude Code sessions.

## Why it exists

Run 20-30 Claude Code sessions on a 24 GB laptop and sooner or later two of them start a training run, a big render or a test suite at the same time. macOS swaps, the disk fills, and the whole machine crashes. memguard is the cheap fix: it watches memory every 15 seconds, only touches jobs that were started by an agent session, and tells the agents (through a rules file) to line up their heavy jobs in a queue.

## What is in the box

| File | What it does |
|---|---|
| `memguard.py` | The guard. One pass per run (launchd runs it every 15 s). Pauses (`SIGSTOP`) the biggest session job when available memory is low, kills it when memory is critical, resumes jobs one by one when memory recovers, stops the lowest-ranked paused job if memory stays low too long, holds new heavy jobs when the disk is nearly full or swap is high while memory is short, alerts you once when a queued job has waited too long, copes with macOS sitting at pressure level 2 for hours, and dispatches the `heavyrun` queue (both lanes, with an optional drag-and-drop `manual_order`). |
| `heavyrun` | Wrapper agents use to run heavy work: `heavyrun "<session>: <job>" <command...>`. Up to 3 jobs at once (memory permitting) plus a light lane for short small jobs. Runs commands with `nice -n 15`. |
| `config.example.json` | Every threshold and pattern, with defaults for a 24 GB Mac. |
| `rank.example.txt` | Optional job order, plus a `PARK` section for jobs that should always go last. |
| `RULES.md` | A rules block to paste into your agents' standing instructions. |
| `install.sh` | Copies files to `~/.config/memguard`, writes the launchd plist, loads it. |
| `launchd/com.example.memguard.plist.template` | The launchd job (every 15 s). |
| `tests/` | Unit tests (dispatcher ordering, rank file, manual order, priority, disk/swap hold, level-2 handling, stuck rule, job detection, light lane, heavyrun). |
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

### Tuning for your Mac

The defaults suit a 24 GB machine: pause under 6 GB available, kill under 3 GB, resume over 9 GB. On 16 GB try `pause_avail_gb 4`, `kill_avail_gb 2`, `resume_avail_gb 6`, `slot2_avail_gb 8`, `slot3_avail_gb 12`. To force a strict one-at-a-time queue set `max_heavy_jobs` to 1 and `slot2_avail_gb` very high. Run `--dry-run` for a day to see what it would have done.

## Known limits

- Pausing a job keeps its memory; macOS can swap it out, but it is not freed. That is what the "stuck" rule is for.
- Memory numbers come from macOS tools whose output could change between OS versions; `--status` is a quick check.
- It is a safety net, not a scheduler. Agents still need to follow `RULES.md`.

## Licence

MIT. See `LICENSE`.
