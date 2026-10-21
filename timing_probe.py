#!/usr/bin/env python3
"""Host-noise baseline for timing-sensitive jobs. Called by heavyrun for HEAVY_TIMING=1 jobs.
Why: a latency or jitter figure measured while other jobs load the Mac can miss its target by a millisecond or more; the same
test on a quiet Mac passes. A timing result means nothing without a measure of how noisy the host was while it ran.

Runs timer loops at 2.5 ms and 23 ms periods for --secs seconds, each in its own process (no GIL interplay), and measures how late
each wake-up is (p50/p99, ms):
  * HOST NOISE (exported as HOST_BASELINE_JITTER_*): kqueue timers flagged NOTE_CRITICAL, which macOS fires without coalescing
    slack; each wake-up then does 0.2 ms of CPU work. Lateness of the finish = the CPU scheduler being late = load from other work.
    Quiet host: p99 ~0.1-0.3 ms.
  * TIMER SLACK (HOST_TIMER_SLACK_*): plain sleep() to the deadline, as a naive timing loop does. In our measurements processes
    started from an agent session got macOS latency tier 1 = up to 25 % of the sleep, max 5 ms of slack (p50 ~3.5-4 ms at 23 ms) even
    on an idle Mac; an app the user launches gets tier 0 (max 1 ms). That floor is the OS, not load.
Also records load average, memory pressure and the other running queue jobs. Prints NAME=value lines (numbers only) and one SUMMARY=
line. Standard library only; never touches other processes.
"""
import argparse, json, os, re, select, subprocess, sys, time

PERIODS_MS = (2.5, 23.0)
WORK_S = 0.0002     # CPU work per noise tick
BUSY_P99_MS = 2.0   # RULES.md: host-noise p99 above this = "inconclusive: host busy"
SLOTS = ("heavy.lock", "heavy.slot2", "heavy.slot3", "light.lock", "light.slot2")

def worker(mode, period_s, secs):
    kq = select.kqueue(); t0 = time.perf_counter(); n = 1; late = []
    while True:
        target = t0 + n * period_s
        if target - t0 > secs: break
        rem = target - time.perf_counter()
        if mode == "plain":
            if rem > 0: time.sleep(rem)
        elif rem > 0:   # kqueue timer with NOTE_CRITICAL (0x20) + NOTE_NSECONDS (0x4): macOS applies no coalescing slack
            kq.control([select.kevent(1, filter=select.KQ_FILTER_TIMER, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                      fflags=0x20 | 0x4, data=int(rem * 1e9))], 1, None)
        work = 0.0
        if mode != "plain":   # then 0.2 ms of CPU work, like a frame being built: a busy CPU shows up as late finishes
            c = time.process_time(); work = WORK_S
            while time.process_time() - c < WORK_S: pass
        now = time.perf_counter(); late.append(max(0.0, now - target - work))
        n = max(n + 1, int((now - t0) / period_s) + 1)   # a missed tick is skipped, not replayed as a burst
    late.sort()
    q = lambda p: late[min(len(late) - 1, int(round(p / 100 * (len(late) - 1))))] * 1000
    print(f"{q(50):.4f} {q(99):.4f} {late[-1] * 1000:.4f} {len(late)}")

def other_jobs(dirs, self_pid):
    seen, out = set(), []
    for d in dirs:
        for s in SLOTS:
            p = os.path.join(d, s)
            if not os.path.isdir(p): continue
            try: pid = int(open(os.path.join(p, "pid")).read().strip())
            except (OSError, ValueError): continue
            if pid == self_pid or pid in seen: continue
            try: os.kill(pid, 0)
            except ProcessLookupError: continue
            except PermissionError: pass
            try: lab = open(os.path.join(p, "label")).read().strip()
            except OSError: lab = "?"
            seen.add(pid); out.append(lab)
    return out

def priority_regex(dirpath):
    """priority_pattern from config.json (memguard's setting; default "priority"); None when it is empty."""
    cfg = os.environ.get("MEMGUARD_CONFIG") or os.path.join(dirpath, "config.json")
    pat = "priority"
    try:
        with open(cfg) as f: pat = json.load(f).get("priority_pattern", pat)
    except (OSError, ValueError, AttributeError): pass
    try: return re.compile(pat, re.I) if pat else None
    except re.error: return None

def mem():
    try:
        mp = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
        free = int(re.search(r"free percentage:\s*(\d+)", mp).group(1))
    except Exception: free = -1
    try: lvl = int(subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True).stdout.strip())
    except Exception: lvl = -1
    return free, lvl

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=10.0)
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--self-pid", type=int, default=os.getppid())
    ap.add_argument("--also-dir", action="append", default=[], help="another memguard home whose running jobs should be counted too")
    ap.add_argument("--worker", nargs=2, metavar=("MODE", "PERIOD_MS"))
    a = ap.parse_args()
    if a.worker: return worker(a.worker[0], float(a.worker[1]) / 1000, a.secs)
    dirs = [a.dir] + [d for d in a.also_dir if os.path.realpath(d) != os.path.realpath(a.dir)]   # the selftest sandbox also reports the real queue
    prio_re = priority_regex(a.dir)
    runs = {(m, p): subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--secs", str(a.secs), "--worker", m, str(p)],
                                     stdout=subprocess.PIPE, text=True)
            for m in ("noise", "plain") for p in PERIODS_MS}
    R = {}
    for k, pr in runs.items():
        out, _ = pr.communicate(timeout=a.secs + 30)
        try: R[k] = [float(x) for x in out.split()[:3]]
        except ValueError: R[k] = [float("nan")] * 3
    p50 = max(R["noise", p][0] for p in PERIODS_MS); p99 = max(R["noise", p][1] for p in PERIODS_MS)
    s50, s99 = R["plain", 23.0][0], R["plain", 23.0][1]
    load1 = os.getloadavg()[0]; free, lvl = mem()
    jobs = other_jobs(dirs, a.self_pid)
    prio = int(bool(prio_re) and any(prio_re.search(j) for j in jobs))
    busy = int(not p99 <= BUSY_P99_MS or prio == 1)   # NaN (probe failed) counts as busy
    kv = dict(HOST_BASELINE_JITTER_P99_MS=f"{p99:.3f}", HOST_BASELINE_JITTER_P50_MS=f"{p50:.3f}",
              HOST_TIMER_SLACK_P99_MS=f"{s99:.3f}", HOST_TIMER_SLACK_P50_MS=f"{s50:.3f}",
              HOST_LOADAVG_1M=f"{load1:.2f}", HOST_MEM_FREE_PCT=str(free), HOST_MEM_PRESSURE_LEVEL=str(lvl),
              HOST_OTHER_JOBS=str(len(jobs)), HOST_PRIORITY_RUNNING=str(prio), HOST_BASELINE_BUSY=str(busy))
    for k, v in kv.items():
        if re.fullmatch(r"-?[\d.]+", v): print(f"{k}={v}")
    why = []
    if not p99 <= BUSY_P99_MS: why.append(f"noise p99 {p99:.2f} ms > {BUSY_P99_MS:g} ms")
    if prio: why.append("a priority job is running")
    verdict = f"INCONCLUSIVE: host busy ({', '.join(why)})" if busy else "quiet host"
    f = lambda k: f"{R[k][0]:.2f}/{R[k][1]:.2f}"
    lst = "; ".join(j.replace("\n", " ")[:80] for j in jobs) or "none"
    print(f"SUMMARY={verdict} | host noise p50/p99 ms: 2.5ms {f(('noise', 2.5))}, 23ms {f(('noise', 23.0))} | "
          f"sleep() slack p50/p99 ms: 2.5ms {f(('plain', 2.5))}, 23ms {f(('plain', 23.0))} ({a.secs:g} s) | "
          f"load1 {load1:.2f} | mem free {free}% level {lvl} | other jobs {len(jobs)}: {lst}")

if __name__ == "__main__": main()
