#!/usr/bin/env python3
"""Afterburner: a temporary higher-CPU mode for memguard's queue that drops back to normal by itself.

  afterburner on [duration, default 2h] [reason...]   e.g.  afterburner on 4h big batch tonight
  afterburner off [reason...]
  afterburner status                                  state, time left, caps, load, memory, why it would drop
  afterburner caps [--dir DIR]                        machine-readable "HEAVY_CAP LIGHT_SLOTS NICE" (heavyrun reads this)

Meant to be switched on by the person at the Mac, not by agents. While ON:
  - heavy lane: up to afterburner_max_heavy jobs (default cores - 2, at least 3), but only as many as the MEMORY TIERS allow
    (memguard.mem_slots: 2nd job at slot2_avail_gb, 3rd at slot3_avail_gb, each further job + afterburner_extra_slot_gb).
    The memory floors (pause / kill / resume, disk and swap hold, stuck rule) never change in any mode.
  - light lane: afterburner_light_slots slots (default 2); heavyrun starts commands with nice -n afterburner_nice (default 10) instead of 15.
  - agents may use up to N workers, N = (cores - 2) // running agent sessions, minimum 1 ("workers_per_agent" in afterburner.json).
It DROPS back to normal by itself (logged in memguard.log + a notification) on: memory pressure WARN or worse; 1-minute load above
cores x afterburner_load_factor for afterburner_load_seconds; an app matching afterburner_drop_apps frontmost or newly started (for
example your DAW: "Ableton Live|Logic Pro"); on battery below afterburner_battery_min_pct; when the time is up; or when memguard stops
checking (caps go back to normal after 2 minutes without a check).
Running jobs are never killed or paused by afterburner; memguard's normal memory rules still apply to them.
State: afterburner.json in the memguard home ($MEMGUARD_HOME, default ~/.config/memguard). memguard.py calls check() every pass.
Config: the afterburner_* keys of config.json (see config.example.json).
"""
import json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = os.path.expanduser(os.environ.get("MEMGUARD_HOME", "~/.config/memguard"))
CORES = os.cpu_count() or 8
DEFAULTS = {
    "afterburner_max_heavy": 0,          # 0 = cores - 2 (at least 3)
    "afterburner_light_slots": 2,
    "afterburner_nice": 10,
    "afterburner_default_hours": 2,
    "afterburner_max_hours": 24,
    "afterburner_load_factor": 1.5,      # drop when the 1-minute load stays above cores x this ...
    "afterburner_load_seconds": 120,     # ... for this long
    "afterburner_battery_min_pct": 30,   # drop on battery below this
    "afterburner_drop_apps": "",         # regex: drop when an app whose name or command line matches is frontmost or newly started
    "session_pattern": r"(claude-code/[^ ]*/claude|\.local/share/claude/versions/[^ ]+|/bin/claude)( |$)",
    "notify": True,
}

def config_file(here=None):
    return os.path.expanduser(os.environ.get("MEMGUARD_CONFIG") or os.path.join(here or HOME_DIR, "config.json"))

def load_config(here=None):
    cfg = dict(DEFAULTS)
    try:
        with open(config_file(here)) as f: cfg.update(json.load(f))
    except Exception: pass
    return cfg

CFG = load_config()
MAX_HEAVY = int(CFG["afterburner_max_heavy"]) or max(3, CORES - 2)
NORMAL = dict(heavy_cap=3, light_slots=1, nice=15)
ON_CAPS = dict(heavy_cap=MAX_HEAVY, light_slots=int(CFG["afterburner_light_slots"]), nice=int(CFG["afterburner_nice"]))
LOAD_LIMIT, LOAD_SECS = CORES * float(CFG["afterburner_load_factor"]), int(CFG["afterburner_load_seconds"])
BATT_MIN = int(CFG["afterburner_battery_min_pct"])
FRONT_EVERY = 60                       # frontmost-app check at most once a minute
DEFAULT_SECS = int(float(CFG["afterburner_default_hours"]) * 3600)
MAX_SECS = int(float(CFG["afterburner_max_hours"]) * 3600)
SESSION_RE = re.compile(CFG["session_pattern"])
def _re(p):
    try: return re.compile(p, re.I) if p else None
    except re.error: return None
DROP_APPS = _re(CFG["afterburner_drop_apps"])


def path(here=None): return os.path.join(here or HOME_DIR, "afterburner.json")

def load(here=None):
    try:
        with open(path(here)) as fh: return json.load(fh)
    except Exception: return {"on": False}

def save(st, here=None):
    p = path(here); tmp = p + ".tmp"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(tmp, "w") as fh: json.dump(st, fh, indent=1)
    os.replace(tmp, p)

def active(st=None, now=None, here=None):
    st = load(here) if st is None else st
    return bool(st.get("on")) and (now or time.time()) < st.get("until", 0)

STALE = 120   # memguard refreshes "checked" every pass while ON; if it stops (guard not running), nothing gets afterburner caps
def caps(st=None, now=None, here=None):
    st = load(here) if st is None else st; now = now or time.time()
    fresh = now - st.get("checked", 0) < STALE
    return dict(ON_CAPS) if active(st, now) and fresh else dict(NORMAL)

def _log(msg, here=None):
    with open(os.path.join(here or HOME_DIR, "memguard.log"), "a") as f: f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")

def _notify(msg):
    if not CFG.get("notify", True): return
    safe = msg.replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "memguard"'], capture_output=True)

# ---- readings (each can be stubbed in tests) ----
def ps_cmds():
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    res = {}
    for line in out.splitlines():
        p = line.strip().split(None, 1)
        if len(p) == 2 and p[0].isdigit(): res[int(p[0])] = p[1]
    return res

def agent_sessions(cmds):   # running agent sessions = processes matching session_pattern
    return sum(1 for c in cmds.values() if SESSION_RE.search(c))

def drop_app_pids(cmds): return sorted(p for p, c in cmds.items() if DROP_APPS and DROP_APPS.search(c))

def frontmost():
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=5).stdout.strip()
        out = subprocess.run(["lsappinfo", "info", "-only", "name", asn], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"LSDisplayName"="([^"]*)"', out); return m.group(1) if m else ""
    except Exception: return ""

def battery():   # (on_battery, percent or None)
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"(\d+)%", out); return ("Battery Power" in out, int(m.group(1)) if m else None)
    except Exception: return False, None

def pressure():   # (level 1 normal / 2 warn / 4 critical, free %)
    try: lvl = int(subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True).stdout.strip() or 1)
    except Exception: lvl = 1
    try:
        mp = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
        pct = int(re.search(r"free percentage:\s*(\d+)", mp).group(1))
    except Exception: pct = None
    return lvl, pct

def workers_per_agent(n_agents): return max(1, (CORES - 2) // max(1, n_agents))

LVL_NAME = {1: "normal", 2: "WARN", 4: "CRITICAL"}

def drop_reasons(st, lvl, load1, cmds, front, batt, now, mutate=True):
    """Why afterburner must drop now (empty list = stay on). With mutate, keeps the load timer in st."""
    why = []
    if lvl >= 2: why.append(f"memory pressure {LVL_NAME.get(lvl, lvl)}")
    if load1 > LOAD_LIMIT:
        since = st.get("load_high_since") or now
        if mutate: st["load_high_since"] = since
        if now - since >= LOAD_SECS: why.append(f"load {load1:.1f} above {LOAD_LIMIT:.0f} for {int(now - since)} s")
    elif mutate: st.pop("load_high_since", None)
    new_app = [p for p in drop_app_pids(cmds) if p not in st.get("drop_app_pids_at_on", [])]
    if new_app: why.append(f"{cmds[new_app[0]].split('/')[-1][:40]} started (pid {new_app[0]})")
    if front and DROP_APPS and DROP_APPS.search(front): why.append(f"{front} is frontmost")
    on_batt, pct = batt
    if on_batt and pct is not None and pct < BATT_MIN: why.append(f"on battery at {pct} % (< {BATT_MIN} %)")
    return why

def turn_off(st, why, kind, here=None, log=None, notify=None):
    log, notify = log or (lambda m: _log(m, here)), notify or _notify
    st.update(on=False, off_at=time.time(), off_reason=f"{kind}: {why}", workers_per_agent=1)
    st.pop("load_high_since", None)
    save(st, here)
    log(f"AFTERBURNER {kind.upper()} to normal mode: {why}")
    notify(f"Afterburner off: {why}"[:180])

def check(lvl, cmds=None, here=None, log=None, notify=None, now=None, load1=None, front=None, batt=None):
    """Called by memguard every pass. Auto-off at `until`, drop on any drop reason, else refresh N. Returns the caps in force."""
    st = load(here)
    if not st.get("on"): return caps(st)
    now = now or time.time()
    if now >= st.get("until", 0):
        turn_off(st, f"time up ({st.get('reason', '')})", "off", here, log, notify); return caps(st)
    cmds = ps_cmds() if cmds is None else cmds
    load1 = os.getloadavg()[0] if load1 is None else load1
    if front is None:
        if DROP_APPS and now - st.get("front_checked", 0) >= FRONT_EVERY:
            front = frontmost(); st["front_checked"] = now
        else: front = ""
    batt = battery() if batt is None else batt
    why = drop_reasons(st, lvl, load1, cmds, front, batt, now)
    if why:
        turn_off(st, "; ".join(why), "dropped", here, log, notify); return caps(st)
    n = agent_sessions(cmds); st["agents"] = n; st["workers_per_agent"] = workers_per_agent(n); st["checked"] = now
    save(st, here)
    return caps(st)

# ---- command line ----
def parse_dur(s):
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+)m)?", s.lower())
    if m and (m.group(1) or m.group(2)): return int(float(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60)
    if re.fullmatch(r"\d+(?:\.\d+)?", s): return int(float(s) * 3600)   # bare number = hours
    return None

def hm(secs):
    secs = max(0, int(secs)); return f"{secs // 3600} h {secs % 3600 // 60:02d} min"

def cmd_on(args, here=None):
    secs = parse_dur(args[0]) if args else None
    if secs is not None: args = args[1:]
    secs = secs or DEFAULT_SECS
    if secs > MAX_SECS: print(f"afterburner: max {hm(MAX_SECS)} at a time"); return 2
    reason = " ".join(args) or "by hand"
    lvl, _ = pressure(); cmds = ps_cmds(); now = time.time()
    why = drop_reasons({}, lvl, 0.0, {}, frontmost() if DROP_APPS else "", battery(), now, mutate=False)   # instant checks only
    if why: print("afterburner NOT turned on: " + "; ".join(why)); return 1
    st = load(here); was = active(st, now)
    n = agent_sessions(cmds)
    st = dict(on=True, since=st.get("since", now) if was else now, until=now + secs, reason=reason, cores=CORES,
              heavy_cap=ON_CAPS["heavy_cap"], light_slots=ON_CAPS["light_slots"], nice=ON_CAPS["nice"],
              agents=n, workers_per_agent=workers_per_agent(n), drop_app_pids_at_on=drop_app_pids(cmds), checked=now)
    save(st, here)
    until = time.strftime("%-I:%M %p", time.localtime(st["until"]))
    _log(f"AFTERBURNER {'EXTENDED' if was else 'ON'} for {hm(secs)} until {until} ({reason}): heavy cap {MAX_HEAVY} (memory tiers apply), "
         f"light {ON_CAPS['light_slots']}, nice {ON_CAPS['nice']}, {workers_per_agent(n)} worker(s) per agent ({n} sessions)", here)
    print(f"afterburner ON until {until} ({hm(secs)}): {reason}")
    return 0

def cmd_off(args, here=None):
    st = load(here)
    if not st.get("on"): print("afterburner already off"); return 0
    turn_off(st, " ".join(args) or "turned off by hand", "off", here, notify=lambda m: None)
    print("afterburner OFF (normal mode)"); return 0

def mem_slots_now(avail_gb, cap):
    try:
        sys.path.insert(0, HERE); import memguard
        return memguard.mem_slots(avail_gb * memguard.GB, cap)
    except Exception: return None

def cmd_status(here=None):
    st = load(here); now = time.time(); on = active(st, now); c = caps(st, now)
    lvl, pct = pressure(); load1 = os.getloadavg()[0]; cmds = ps_cmds(); front = frontmost(); batt = battery()
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip() or 0)
    avail_gb = total * (pct or 0) / 100 / (1 << 30)
    n = agent_sessions(cmds); N = workers_per_agent(n) if on else 1
    if on:
        print(f"AFTERBURNER: ON  since {time.strftime('%-I:%M %p', time.localtime(st['since']))}, until "
              f"{time.strftime('%-I:%M %p', time.localtime(st['until']))} ({hm(st['until'] - now)} left)")
        print(f"  reason: {st.get('reason', '')}")
    else:
        print("AFTERBURNER: OFF (normal mode)")
        if st.get("off_reason"): print(f"  last: {st['off_reason']} at {time.strftime('%b %-d %-I:%M %p', time.localtime(st.get('off_at', now)))}")
    ms = mem_slots_now(avail_gb, c["heavy_cap"])
    print(f"  caps: heavy lane up to {c['heavy_cap']} jobs (memory allows {ms if ms is not None else '?'} now at {avail_gb:.1f} GB available), "
          f"light lane {c['light_slots']} slot(s), heavyrun nice -n {c['nice']}")
    print(f"  agents: {n} sessions running -> WORKERS PER AGENT N = {N}" + ("" if on else " (one-worker rule)"))
    print(f"  load (1 min): {load1:.1f} (drop line {LOAD_LIMIT:.0f} for {LOAD_SECS} s, {CORES} cores)")
    print(f"  memory: {pct if pct is not None else '?'} % free, pressure {LVL_NAME.get(lvl, lvl)} (drop at WARN); floors unchanged in every mode")
    print(f"  battery: {'on battery' if batt[0] else 'on power'}, {batt[1] if batt[1] is not None else '?'} % (drop below {BATT_MIN} % on battery)")
    if DROP_APPS: print(f"  frontmost app: {front or '?'}; drop apps running: {'yes' if drop_app_pids(cmds) else 'no'}")
    why = drop_reasons(dict(st), lvl, load1, cmds, front, batt, now, mutate=False)
    if load1 > LOAD_LIMIT and not any(w.startswith("load") for w in why): why.append(f"load {load1:.1f} above {LOAD_LIMIT:.0f} (drops if it stays)")
    print("  would drop now: " + ("; ".join(why) if why else "no (all clear)"))
    return 0

def main(argv):
    cmd, args = (argv[0] if argv else "status"), argv[1:]
    if cmd == "on": return cmd_on(args)
    if cmd == "off": return cmd_off(args)
    if cmd == "status": return cmd_status()
    if cmd == "caps":
        here = args[1] if len(args) > 1 and args[0] == "--dir" else None
        c = caps(here=here); print(c["heavy_cap"], c["light_slots"], c["nice"]); return 0
    print(__doc__); return 2

if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
