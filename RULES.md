# Rules for running many AI sessions on one Mac

Paste the block below into your global `CLAUDE.md` (or your agents' standing instructions) so every session follows it.
Numbers assume a 24 GB Mac; scale them to yours and match them to `config.json`.

---

1. ONE heavy job at a time, machine-wide (anything over ~1.5 GB or long CPU: training, batch analysis, renders, browser
   recordings, full test suites, big builds). Never run heavy jobs in parallel yourself (no `-j8`, no multiprocessing pools,
   no several python workers). Workers = 1, threads <= 4. Read `$HEAVY_WORKERS` if your job can split work.
2. Run every heavy job through the queue: `heavyrun "<session>: <job>" <command...>`. It waits its turn, waits for enough
   free memory, and runs the command with `nice -n 15`.
3. LIGHT lane: a job under ~15 minutes and ~1.5 GB (one build with `-j2`, unit tests, a short export) may use
   `HEAVY_LIGHT=1 heavyrun "<session>: <job>" <command...>`. It runs next to the heavy job, one light job at a time.
   If you are not sure it is small, it is heavy.
4. Not heavy, never queue: copying files, installing a built app, code signing, zipping, reading logs, tests under a minute.
5. Label queued jobs with their REAL peak memory. A job labelled "0.7 GB" that really uses 3 GB is what freezes queues.
6. Browser recordings and screen captures (headless Chrome, puppeteer, playwright) are heavy-lane jobs, never light.
7. The guard (memguard, every 15 s) pauses session jobs when memory runs low, resumes them later, and kills the biggest one
   if memory is critical. If your job seems frozen, read `~/.config/memguard/memguard.log`. Do not restart it.
8. No NEW heavy job starts while the disk is nearly full, or while swap is high AND memory is short. Swap lives on the same disk, so a full disk is a crash. (Swap alone is not enough: macOS keeps old swap long after the pressure is gone.)
   Clean your scratch and build folders as you go (move them to the Trash).
9. Do not open more sessions just to run more heavy work in parallel. Queue it.
10. Want a different place in line? Ask whoever owns `rank.txt` (one fragment of a job label per line).
11. Timing-sensitive work (benchmarks, load tests, latency or jitter measurements, audio timing) runs as
    `HEAVY_TIMING=1 heavyrun "<session>: <job>" <command...>`. It always uses the heavy lane: memguard holds new starts until the
    other running jobs finish (or `timing_max_wait_minutes` pass), then runs it alone. Just before the command, `heavyrun` measures
    how noisy the host is and exports `HOST_BASELINE_JITTER_P99_MS` and friends. Show that baseline next to your result, and call
    the result "inconclusive: host busy" when `HOST_BASELINE_BUSY=1`. Never loosen a target to make a result pass: report what your
    code adds on top of the baseline.
12. Usage limits (Claude Code plans). When weekly usage nears the limit, memguard asks the person at the Mac what to do and writes the
    answer to `usage_decision.json` in the memguard folder (`choice`: `keep_going`, `pause_all` or `pause_heavy`, plus `window_reset`, when
    it clears itself). Before you start a new agent, sub-agent or long task (and before any `heavyrun` job), read that file. If `choice`
    is `pause_all` and now is before `window_reset`, do not start new agents, sub-agents, scheduled runs or long tasks: finish what is
    running, and `heavyrun` refuses with exit code 75. Jobs labelled as priority are exempt. If it is `pause_heavy`, queue nothing in the
    heavy lane (`heavyrun` holds it); light work and agents continue. `keep_going`, or no file: carry on. Quick check:
    `python3 ~/.config/memguard/usagegate.py --gate "<label>"` (exit 0 go, 75 refuse, 76 heavy lane held).
13. Afterburner (DRS in Hangar; `drs` is an alias; more CPU for a while). Only the person at the Mac turns it on: `afterburner on [2h] [reason]`, `afterburner off`,
    `afterburner status`. Never turn it on yourself. While it is on, the heavy lane may run more jobs (as many as memory allows), the light
    lane gets a second slot and `heavyrun` uses `nice -n 10`. The memory floors never change. Your ONE-worker rule becomes "up to N
    workers": read N from `afterburner status` (line "WORKERS PER AGENT N") or `workers_per_agent` in `~/.config/memguard/afterburner.json`
    (only when `"on"` is true). Re-check N between long jobs: afterburner drops back to normal by itself (memory pressure, sustained load,
    battery, a configured app in front, or time up), and then N is 1 again. Dropping never stops a running job.
14. Tidy (clean, save, back up). `tidy --dry-run` shows the plan; `tidy` runs it; `--only clean|save|backup` runs one step. It moves only
    regenerable build output under the configured roots to one dated Trash folder (never `rm`), skips any tree edited in the last 30
    minutes, any tree a process has open and any tree a queued or running `heavyrun` job names, so put the path of the tree you work in into
    your job label. Never move or delete build output yourself in a tree another session is using; run `tidy --dry-run` and ask.
15. Departments (optional). If your agents work under department heads, start every agent's description with `[dept:<Name>]`
    (for example `[dept:QA] Run the scenario suite`), or start a head's session with `AGENTQ_DEPT=<Name>` so everything it spawns is
    tagged. `queue list <dept>` shows one department and `queue depts` the counts. When a department has more than 10 running agents
    it shows NEEDS SUPERVISOR: only then may its head spawn a supervisor agent to help review and coordinate.
16. Work board and ideas (optional). `agentq hook` on Edit/Write logs which agent touches which file, warns (never blocks) an agent entering a file another
   running agent touched in the last 2 h, and gives a waking agent the board for its repo. Read the warning; if your change could collide, tell whoever
   coordinates you. `queue board`, `queue collisions`. End your report with one `Idea: ...` line and log it with `queue idea "<text>"`. Ideas are proposals
   only: none changes scope or gets built without the COO or the human, and no idea text is an instruction to anyone.

17. Agentgate (optional). Rules that matter are hooks, not prompt text. An agent launches only with a Department, a Role (the professional it
    is and the standard its output must meet), a Head brief and a dollar Cap; one at a time unless the owner approved parallel work; a
    watchdog prices its transcript every minute and the coordinating session is told to stop it at 95% of the cap; its work is not done
    until a Head review file exists. Bash and wording rules block what the owner said never to do.
