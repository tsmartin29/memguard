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
