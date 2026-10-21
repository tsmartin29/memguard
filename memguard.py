#!/usr/bin/env python3
"""memguard: a small RAM / swap / disk guard for Macs that run many AI-agent sessions at once.

Runs every ~15 s from launchd. It stops background jobs started by a Claude Code session
(python scripts, renders, test suites, builds...) BEFORE the Mac runs out of memory.

It only touches processes STARTED BY an agent session (some ancestor matches `session_pattern`) or by the
`heavyrun` queue wrapper. It never touches protected apps (browsers, the Claude app, Finder, system processes).

  available memory < pause_avail_gb   or pressure WARN      -> pause (SIGSTOP) the biggest session job
  available memory < kill_avail_gb    or pressure CRITICAL  -> kill the biggest session job (SIGTERM, then SIGKILL)
  more heavy jobs than the memory budget allows              -> pause the newer ones
  paused jobs resume one at a time once memory is back above resume_avail_gb
  stuck rule: low memory for stuck_seconds with jobs paused  -> stop the lowest-ranked paused job
  pressure level 2 (macOS "warn") can last for hours with plenty of memory free, so by default (level2_one_job) one job
    may keep running at level 2 while available memory is above pause_avail_gb + level2_run_margin_gb, and when nothing
    is running the smallest paused job is resumed (deadlock breaker) once available is above pause_avail_gb + level2_resume_margin_gb

It also acts as the dispatcher for the `heavyrun` queue: it decides which queued jobs may start next
(priority tickets, then rank.txt order, then first come first served) and holds all new heavy jobs while
swap is high or the disk is nearly full. An optional manual_order file lets a UI drag-and-drop re-rank the queue.

Config: $MEMGUARD_CONFIG, else $MEMGUARD_HOME/config.json (default home: ~/.config/memguard). See config.example.json.
Usage:  memguard.py            one pass (this is what launchd runs)
        memguard.py --dry-run  one pass, log what it WOULD do, send no signals
        memguard.py --status   print memory, thresholds and the jobs it can see
"""
import json, os, re, signal, subprocess, sys, time

HOME_DIR = os.path.expanduser(os.environ.get("MEMGUARD_HOME", "~/.config/memguard"))
CONFIG_FILE = os.path.expanduser(os.environ.get("MEMGUARD_CONFIG", os.path.join(HOME_DIR, "config.json")))
GB = 1 << 30

DEFAULTS = {
    # thresholds, in GB of memory macOS says it can hand out (tuned for a 24 GB Mac; lower them for 16 GB)
    "pause_avail_gb": 6, "kill_avail_gb": 3, "resume_avail_gb": 9,
    "heavy_job_gb": 1.5,          # a job above this counts as "heavy"
    "min_job_mb": 300,            # ignore session jobs smaller than this
    "second_ok_gb": 10, "second_pause_gb": 7,    # extra heavy jobs resume above / are paused below
    "slot2_avail_gb": 10, "slot3_avail_gb": 14,  # the 2nd / 3rd heavy job may start at this much available memory
    "max_heavy_jobs": 3,          # at most this many heavy jobs keep running (heavyrun has 3 slots)
    "disk_hold_gb": 6.5,          # no NEW heavy job while disk free is below this
    # Swap: macOS keeps old swap long after memory pressure is gone, so swap alone would hold jobs for hours.
    # Hold only when swap is above swap_hard_gb, or above swap_hold_gb while available memory is under swap_low_avail_gb.
    "swap_hold_gb": 6, "swap_low_avail_gb": 8, "swap_hard_gb": 10,
    # macOS can sit at pressure level 2 for hours with several GB free. With level2_one_job, level 2 alone does not pause the
    # last running job (needs available > pause_avail_gb + level2_run_margin_gb), and if everything is paused at level 2 the
    # smallest paused job is resumed once available > pause_avail_gb + level2_resume_margin_gb. False = pause on any level 2.
    "level2_one_job": True, "level2_run_margin_gb": 1, "level2_resume_margin_gb": 2,
    # With level2_one_job, a SECOND job may also keep running at level 2 while available memory is at least this (0 = off).
    "level2_two_jobs_gb": 8,
    # Jobs started under a heavyrun ticket whose label matches priority_pattern are not paused by pressure level 2;
    # they are paused only when available memory drops under pause_avail_gb (and killed under kill_avail_gb).
    "protect_priority_jobs": True,
    # Jobs whose queue label matches this regex (case-insensitive) are stopped last by the stuck rule and are never moved by manual_order.
    # Empty string = no priority labels.
    "priority_pattern": "priority",
    "stall_alert_minutes": 45,    # one notification (and a line in stalled.txt) when a queued job waits this long; 0 = off
    "disk_path": "~",             # which disk to watch
    "stuck_seconds": 300, "stuck_interval_seconds": 60,
    # regexes matched against the full command line
    "session_pattern": r"(claude-code/[^ ]*/claude|\.local/share/claude/versions/[^ ]+|/bin/claude)( |$)",
    "protect_pattern": r"(Claude\.app|WindowServer|Google Chrome|Safari|Firefox|Finder|loginwindow|launchd|kernel_task|Dock|SystemUIServer|memguard)",
    "protect_extra": "",          # your own additions, e.g. "Ableton|Logic Pro|Xcode"
    "headless_pattern": r"--headless|for Testing|puppeteer|playwright",   # browsers started by a session ARE jobs
    "notify": True,               # macOS notification when a job is paused or killed
}

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f: cfg.update(json.load(f))
    except FileNotFoundError: pass
    except Exception as e: print(f"memguard: bad config {CONFIG_FILE}: {e!r}; using defaults", file=sys.stderr)
    return cfg

CFG = load_config()
HERE = HOME_DIR
MANUAL = os.path.join(HERE, "manual_order")
os.makedirs(HERE, exist_ok=True)
LOG, STATE = os.path.join(HERE, "memguard.log"), os.path.join(HERE, "paused.json")
PAUSE_AVAIL, KILL_AVAIL, RESUME_AVAIL = (int(CFG[k] * GB) for k in ("pause_avail_gb", "kill_avail_gb", "resume_avail_gb"))
HEAVY, MIN_JOB = int(CFG["heavy_job_gb"] * GB), int(CFG["min_job_mb"]) << 20
SECOND_OK, SECOND_PAUSE = int(CFG["second_ok_gb"] * GB), int(CFG["second_pause_gb"] * GB)
SLOT2_AVAIL, SLOT3_AVAIL = int(CFG["slot2_avail_gb"] * GB), int(CFG["slot3_avail_gb"] * GB)
MAX_HEAVY = int(CFG["max_heavy_jobs"])
L2_RUN, L2_RESUME = int(CFG["level2_run_margin_gb"] * GB), int(CFG["level2_resume_margin_gb"] * GB)
PRIORITY_RE = re.compile(CFG["priority_pattern"], re.I) if CFG["priority_pattern"] else None
SESSION_RE = re.compile(CFG["session_pattern"])
PROTECT = re.compile(CFG["protect_pattern"] + (("|" + CFG["protect_extra"]) if CFG["protect_extra"] else ""), re.I)
HEADLESS = re.compile(CFG["headless_pattern"], re.I)
DRY = False
SELF_PID = os.getpid()

def g(b): return f"{b / GB:.1f} GB"

def log(msg):
    with open(LOG, "a") as f: f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + ("[dry-run] " if DRY else "") + msg + "\n")
    try:
        if os.path.getsize(LOG) > 2 << 20: os.replace(LOG, LOG + ".1")
    except OSError: pass

def notify(msg):
    if DRY or not CFG["notify"]: return
    safe = msg.replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "memguard"'], capture_output=True)

def mem():
    """(available bytes, pressure level 1 normal / 2 warn / 4 critical)"""
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    ps = int(re.search(r"page size of (\d+)", out).group(1))
    pg = lambda k: int(re.search(rf"{k}:\s+(\d+)", out).group(1)) if re.search(rf"{k}:\s+(\d+)", out) else 0
    avail = (pg("Pages free") + pg("Pages inactive") + pg("Pages speculative") + pg("Pages purgeable")) * ps
    lvl = int(subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True).stdout.strip() or 1)
    # vm_stat's free+inactive undercounts what macOS can really hand out (reclaimable cache, compression), so also
    # use macOS's own "memory free percentage" (what `memory_pressure` reports) and take the larger of the two.
    try:
        mp = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
        pct = int(re.search(r"free percentage:\s*(\d+)", mp).group(1))
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip())
        avail = max(avail, total * pct // 100)
    except Exception as e: log(f"memory_pressure read failed: {e!r}")
    return avail, lvl

def procs():
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,state=,etime=,command="], capture_output=True, text=True).stdout
    P = {}
    for line in out.splitlines():
        p = line.split(None, 5)
        if len(p) < 6: continue
        P[int(p[0])] = dict(pid=int(p[0]), ppid=int(p[1]), rss=int(p[2]) * 1024, state=p[3], etime=p[4], cmd=p[5])
    # RSS misses GPU/Metal memory (a training job can show 0.75 GB RSS but a 7 GB real footprint). Use top's MEM (footprint).
    try:
        t = subprocess.run(["top", "-l", "1", "-stats", "pid,mem"], capture_output=True, text=True, timeout=10).stdout
        mult = {"B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
        for line in t.splitlines():
            m = re.match(r"\s*(\d+)\s+([\d.]+)([BKMG])", line)
            if m and int(m.group(1)) in P:
                c = P[int(m.group(1))]; c["rss"] = max(c["rss"], int(float(m.group(2)) * mult[m.group(3)]))
    except Exception as e: log(f"footprint read failed: {e!r}")
    return P

def is_session(c): return bool(SESSION_RE.search(c["cmd"]))
def is_heavyrun(c): return "/heavyrun " in c["cmd"] or c["cmd"].endswith("/heavyrun")

def session_jobs(P):
    jobs = []
    for c in P.values():
        headless = HEADLESS.search(c["cmd"])
        if c["pid"] == SELF_PID or (PROTECT.search(c["cmd"]) and not headless) or is_session(c): continue
        a, seen = P.get(c["ppid"]), 0
        while a and seen < 40:
            if is_session(a) or is_heavyrun(a): jobs.append(c); break   # heavyrun jobs count even when detached (nohup)
            a, seen = P.get(a["ppid"]), seen + 1
    # group: a job = the top process under the session plus its children's memory
    by = {c["pid"]: c for c in jobs}
    roots = [c for c in jobs if c["ppid"] not in by]
    kids = {}
    for c in jobs: kids.setdefault(c["ppid"], []).append(c["pid"])
    def tree(r):
        tot, stack, pids = 0, [r["pid"]], []
        while stack:
            x = stack.pop(); pids.append(x); tot += by[x]["rss"]; stack += kids.get(x, [])
        return tot, pids
    out = []
    for r in roots:
        tot, pids = tree(r)
        if tot >= MIN_JOB: out.append(dict(root=r["pid"], pids=pids, rss=tot, cmd=r["cmd"][:140], etime=r["etime"]))
    return out

def secs(et):
    d, _, hms = et.rpartition("-"); parts = [int(x) for x in hms.split(":")]
    while len(parts) < 3: parts.insert(0, 0)
    return int(d or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]

def sig(job, s):
    if DRY: return
    for p in job["pids"]:
        try: os.kill(p, s)
        except ProcessLookupError: pass

def read_rank():
    """rank.txt: one label fragment per line, '#' comments, a line 'PARK' sends everything below it to the back."""
    rank, park = [], False
    try:
        for line in open(os.path.join(HERE, "rank.txt")):
            line = line.strip()
            if not line or line.startswith("#"): continue
            if line == "PARK": park = True; continue
            rank.append((line.lower(), park))
    except OSError: pass
    return rank

def should_pause_at_level(avail, lvl, n_live):
    """Pressure level 2 pauses a job, except (level2_one_job) a single running job while available is comfortably above the pause line."""
    if lvl < 2: return False
    if not CFG["level2_one_job"]: return True
    two = CFG.get("level2_two_jobs_gb", 0)
    limit = 2 if two and avail >= two * GB else 1
    return avail < PAUSE_AVAIL + L2_RUN or n_live > limit

def is_priority(label): return bool(PRIORITY_RE and PRIORITY_RE.search(label))

def under_priority_ticket(j, P):
    """True when the job was started by a heavyrun ticket whose command line (label) matches priority_pattern."""
    a, n = P.get(j["root"]), 0
    while a and n < 40:
        if is_heavyrun(a): return is_priority(a["cmd"])
        a, n = P.get(a["ppid"]), n + 1
    return False

def read_manual_order(path=None):
    """manual_order: ticket file names (as in queue*/ and queue*_hold/), one per line; text after whitespace is just a label for humans."""
    out = []
    try:
        for line in open(path or MANUAL):
            line = line.strip()
            if line and not line.startswith("#"): out.append(line.split()[0])
    except OSError: pass
    return out

def apply_manual_order(normal, T, manual):
    """Listed tickets swap among the positions they already hold in the rank order. Unlisted, unknown, duplicate, "0-" priority
    and priority-pattern tickets keep their places, and new tickets still follow rank.txt."""
    seen, movable = set(), []
    for f in manual:
        if f in seen or f not in normal or f.startswith("0-") or is_priority(T[f][1]): continue
        seen.add(f); movable.append(f)
    slots = sorted(normal.index(f) for f in movable)
    out = list(normal)
    for pos, f in zip(slots, movable): out[pos] = f
    return out

def pausable_jobs(live, P, avail):
    """Jobs the pause rules may stop: everything below pause_avail_gb, otherwise all but protected priority jobs."""
    if avail < PAUSE_AVAIL or not CFG.get("protect_priority_jobs", True): return live
    return [j for j in live if not under_priority_ticket(j, P)]

def main():
    try: paused = json.load(open(STATE))
    except Exception: paused = []
    P = procs(); paused = [j for j in paused if j["root"] in P]
    avail, lvl = mem(); jobs = session_jobs(P)
    pz = {j["root"] for j in paused}
    live = sorted([j for j in jobs if j["root"] not in pz], key=lambda j: -j["rss"])
    if (avail < KILL_AVAIL or lvl >= 4) and jobs:
        j = max(jobs, key=lambda j: j["rss"]); sig(j, signal.SIGCONT); sig(j, signal.SIGTERM)
        if not DRY: time.sleep(3)
        sig(j, signal.SIGKILL)
        paused = [p for p in paused if p["root"] != j["root"]]
        log(f"KILLED (available {g(avail)}, level {lvl}) pid {j['root']} {g(j['rss'])}: {j['cmd']}")
        notify(f"Stopped a {g(j['rss'])} background job to keep the Mac running")
    elif (avail < PAUSE_AVAIL or should_pause_at_level(avail, lvl, len(live))) and pausable_jobs(live, P, avail):
        j = pausable_jobs(live, P, avail)[0]; sig(j, signal.SIGSTOP); paused.append(j)
        log(f"PAUSED (available {g(avail)}, level {lvl}) pid {j['root']} {g(j['rss'])}: {j['cmd']}")
        notify(f"Paused a {g(j['rss'])} background job: memory low")
    else:
        heavy = sorted([j for j in live if j["rss"] >= HEAVY], key=lambda j: secs(j["etime"]), reverse=True)
        # keep the oldest heavy job(s) running; pause the rest. With plenty of memory more than one may run.
        keep = 1 if avail < SECOND_PAUSE else MAX_HEAVY
        for j in heavy[keep:]:
            sig(j, signal.SIGSTOP); paused.append(j)
            log(f"QUEUED (too many heavy jobs) pid {j['root']} {g(j['rss'])}: {j['cmd']}")
        if paused and lvl == 2 and not live and CFG["level2_one_job"] and avail > PAUSE_AVAIL + L2_RESUME:
            # deadlock breaker: level 2 with nothing running would keep every job paused for good; smallest job first
            j = min(paused, key=lambda j: j["rss"]); paused.remove(j); sig(j, signal.SIGCONT)
            log(f"RESUMED one job at level 2 (available {g(avail)}) pid {j['root']}: {j['cmd']}")
        elif paused and lvl == 1 and ((not heavy and avail > RESUME_AVAIL) or (len(heavy) < MAX_HEAVY and avail > SECOND_OK)):
            j = paused.pop(0); sig(j, signal.SIGCONT)
            log(f"RESUMED (available {g(avail)}) pid {j['root']}: {j['cmd']}")
    # STUCK rule: pausing keeps a job's memory, so a pile of paused jobs can hold the Mac below the resume line forever.
    # If memory has stayed low for stuck_seconds while jobs are paused, stop (TERM) the lowest-ranked paused job,
    # one per stuck_interval. Jobs whose queue label matches priority_pattern are stopped last.
    stuck_f = os.path.join(HERE, "stuck.json")
    try: stuck = json.load(open(stuck_f))
    except Exception: stuck = {}
    now = time.time()
    if paused and avail < RESUME_AVAIL:
        stuck.setdefault("since", now)
        if now - stuck["since"] > CFG["stuck_seconds"] and now - stuck.get("last_kill", 0) > CFG["stuck_interval_seconds"]:
            def label_of(j):
                a, n = P.get(j["root"]), 0
                while a and n < 40:
                    if is_heavyrun(a): return a["cmd"].split("heavyrun", 1)[1].strip()
                    a, n = P.get(a["ppid"]), n + 1
                return ""
            rank = [f for f, _ in read_rank()]
            def badness(j):
                lab = label_of(j).lower()
                if is_priority(lab): return (0, 0)
                pos = next((i for i, f in enumerate(rank) if f in lab), len(rank))
                return (1, pos)
            j = max(paused, key=lambda j: (badness(j), j["rss"]))
            sig(j, signal.SIGCONT); sig(j, signal.SIGTERM)
            if not DRY: time.sleep(3)
            sig(j, signal.SIGKILL)
            paused = [p for p in paused if p["root"] != j["root"]]
            stuck["last_kill"] = now
            log(f"STOPPED (stuck: memory low {int(now - stuck['since'])} s with jobs paused) pid {j['root']} {g(j['rss'])} [{label_of(j) or 'no queue label'}]: {j['cmd']}")
            notify(f"Stopped a paused {g(j['rss'])} job to free memory")
    else:
        stuck = {}
    if not DRY:
        json.dump(stuck, open(stuck_f, "w"))
        json.dump(paused, open(STATE, "w"))

# ---- dispatcher for the heavyrun queue ----
# heavyrun waiters start when their ticket is listed in visible.txt (heavy lane) or visible_light.txt (light lane).
# We keep only the next job(s) visible and park the rest in a "hold" folder (waiters keep waiting; nothing is killed).
# Order: priority tickets (name starts "0-"), then rank.txt order (adjusted by manual_order), then first come first served.
# Dead tickets (waiter gone) are removed so they can't block the queue.

def disk_and_swap():
    try:
        st = os.statvfs(os.path.expanduser(CFG["disk_path"])); disk_free = st.f_bavail * st.f_frsize
        sw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
        used = re.search(r"used = ([\d.]+)([MG])", sw)
        swap_used = float(used.group(1)) * ((1 << 20) if used.group(2) == "M" else GB)
        return disk_free, swap_used
    except Exception: return 99 * GB, 0

def stall_alert(T, want_q, heavy_lane):
    """One notification per ticket that has waited longer than stall_alert_minutes, with the reason; also appended to stalled.txt."""
    mins = CFG.get("stall_alert_minutes", 45)
    if not mins: return
    try:
        seen_p = os.path.join(HERE, "stalled_seen.txt")
        seen = set(open(seen_p).read().split()) if os.path.exists(seen_p) else set()
        held = heavy_lane and os.path.exists(os.path.join(HERE, "hold_disk_swap"))
        for f, (d, label) in T.items():
            if f in want_q or f in seen: continue
            try: waited = time.time() - int(f.split("-")[-2]) / 1e9   # ticket names start with a nanosecond timestamp
            except (ValueError, IndexError): continue
            if waited < mins * 60: continue
            why = "disk/swap hold" if held else "waiting for a free slot"
            with open(os.path.join(HERE, "stalled.txt"), "a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M')} STALLED {int(waited // 60)} min ({why}): {label}\n")
            notify(f"Job waiting {int(waited // 60)} min ({why}): {label[:60]}")
            seen.add(f)
        with open(seen_p, "w") as fh: fh.write("\n".join(sorted(seen)))
    except Exception as e: log(f"stall alert error: {e!r}")

def dispatch(lane="queue", hold="queue_hold", heavy_lane=True):
    Q, H = os.path.join(HERE, lane), os.path.join(HERE, hold); os.makedirs(Q, exist_ok=True); os.makedirs(H, exist_ok=True)
    rank = read_rank()
    T = {}
    for d in (Q, H):
        for f in os.listdir(d):
            path = os.path.join(d, f)
            try: pid = int(f.rsplit("-", 1)[-1]); os.kill(pid, 0)
            except (ValueError, ProcessLookupError):
                if not DRY:
                    try: os.remove(path); log(f"DISPATCH removed dead ticket {f}")
                    except OSError: pass
                continue
            except PermissionError: pass
            try: label = open(path).read().strip()
            except OSError: continue
            T[f] = (d, label)
    def score(f):
        lab = T[f][1].lower()
        for i, (frag, pk) in enumerate(rank):
            if frag in lab: return (2 if pk else 0, i, f)
        return (1, 0, f)
    normal = sorted([f for f in T if not f.startswith("0-")], key=score)
    normal = apply_manual_order(normal, T, read_manual_order())
    prio = sorted(f for f in T if f.startswith("0-"))
    order = prio + normal
    if heavy_lane:   # memory-budget slots
        avail, _ = mem()
        allowed = 1 + (avail >= SLOT2_AVAIL) + (avail >= SLOT3_AVAIL)
        # Swap lives on the same disk as everything else, so a full disk means no room to swap, which means a crash.
        # No NEW heavy job starts while the disk is nearly full or swap use is high.
        disk_free, swap_used = disk_and_swap()
        hold_f = os.path.join(HERE, "hold_disk_swap")
        swap_bad = swap_used > CFG["swap_hard_gb"] * GB or (swap_used > CFG["swap_hold_gb"] * GB and avail < CFG["swap_low_avail_gb"] * GB)
        if disk_free < CFG["disk_hold_gb"] * GB or swap_bad:
            allowed = 0
            if not os.path.exists(hold_f) and not DRY:
                open(hold_f, "w").write(time.ctime())
                log(f"HOLD new heavy jobs: disk free {g(disk_free)}, swap used {g(swap_used)}")
                notify(f"Holding new big jobs: disk {g(disk_free)} free, swap {g(swap_used)}")
        elif os.path.exists(hold_f) and not DRY:
            os.remove(hold_f); log("HOLD lifted (disk and swap OK)")
        running = sum(1 for s in ("heavy.lock", "heavy.slot2", "heavy.slot3") if os.path.isdir(os.path.join(HERE, s)))
        free = max(0, allowed - running)
        want_q = set(order[:free])
        if not DRY:
            tmp = os.path.join(HERE, "visible.txt.tmp")
            with open(tmp, "w") as fh: fh.write("".join(f + "\n" for f in order[:free]))
            os.replace(tmp, os.path.join(HERE, "visible.txt"))
    else:
        want_q = set(order[:1])
        # heavyrun re-creates its own ticket when the dispatcher parks it, so "first name in the folder" would ignore rank.txt and
        # manual_order. Light-lane waiters start only if named in visible_light.txt (heavyrun falls back to first-come if it is stale).
        if not DRY:
            tmp = os.path.join(HERE, "visible_light.txt.tmp")
            with open(tmp, "w") as fh: fh.write("".join(f + "\n" for f in order[:1]))
            os.replace(tmp, os.path.join(HERE, "visible_light.txt"))
    if DRY: return order
    stall_alert(T, want_q, heavy_lane)
    for f, (d, label) in T.items():
        dest = Q if f in want_q else H
        if d != dest:
            try: os.replace(os.path.join(d, f), os.path.join(dest, f))
            except OSError: pass
    return order

def status():
    avail, lvl = mem(); P = procs()
    print(f"config:      {CONFIG_FILE} ({'found' if os.path.exists(CONFIG_FILE) else 'not found, using defaults'})")
    print(f"available:   {g(avail)}   pressure level {lvl} (1 normal, 2 warn, 4 critical)")
    print(f"thresholds:  pause < {g(PAUSE_AVAIL)}, kill < {g(KILL_AVAIL)}, resume > {g(RESUME_AVAIL)}")
    d, s = disk_and_swap(); print(f"disk free:   {g(d)}   swap used {g(s)}")
    for j in sorted(session_jobs(P), key=lambda j: -j["rss"]):
        print(f"  job pid {j['root']:>6}  {g(j['rss']):>8}  {j['cmd'][:90]}")

if __name__ == "__main__":
    if "--status" in sys.argv: status(); sys.exit(0)
    DRY = "--dry-run" in sys.argv
    try: dispatch(); dispatch("queue_light", "queue_light_hold", heavy_lane=False)
    except Exception as e: log(f"dispatch error: {e!r}")
    try: main()
    except Exception as e: log(f"error: {e!r}")
