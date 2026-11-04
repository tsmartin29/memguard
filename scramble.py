#!/usr/bin/env python3
"""SCRAMBLE modes (named after a scramble: launch every jet, now).

  scramble cap [duration] [reason...]       CAP (combat air patrol): keep 12 agents running
  scramble on [duration] [reason...]        Scramble: keep 20 running (default duration 8h; `on` means 20, or SCRAMBLE_TARGET)
  scramble surge [duration] [reason...]     Surge: keep 25 running
  scramble set N [duration] [reason...]     Custom: keep N running, N from 1 to 40
  scramble off [reason...]
  scramble status                           mode, count, time left, queue file, free disk (warns under 10 GB)

Scramble is a standing order to your orchestrating agent session, not a resource cap: Afterburner gives the Mac more muscle (heavy-lane caps);
Scramble keeps the agent slots busy (target = the count above; 20 is the Claude Code concurrent-subagent default). Memory floors
and memguard's rules are unchanged. It drops back to off by itself when the time is up.
Disk safety: free disk under 10 GB prints a warning in `status`, and a count above 20 (Surge, or Custom 21-40) refuses to turn on
until there is 10 GB free (more agents means more builds and caches).
State: scramble.json {on, mode, since, until, reason, target_agents, queue}; your orchestrating session reads target_agents and mode.
"""
import json, os, re, shutil, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = int(os.environ.get("SCRAMBLE_TARGET", "20"))   # what `on` means; Claude Code concurrent-subagent cap by default
MODES = {"cap": 12, "on": TARGET, "scramble": TARGET, "surge": 25}
MODE_NAME = {"cap": "cap", "on": "scramble", "scramble": "scramble", "surge": "surge"}
LABEL = {"cap": "CAP", "scramble": "Scramble", "surge": "Surge", "custom": "Custom"}
MIN_N, MAX_N = 1, 40
LOW_DISK_GB = 10        # warn under this; refuse counts above NORMAL_MAX under it
NORMAL_MAX = 20
GIB = 2 ** 30
QUEUE_DIR = os.path.expanduser(os.environ.get("SCRAMBLE_QUEUE_DIR", "~/.config/memguard"))   # newest AGENT_QUEUE_*.md here
DEFAULT_SECS = 8 * 3600


def path(): return os.path.join(os.environ.get("SCRAMBLE_STATE_DIR") or HERE, "scramble.json")   # SCRAMBLE_STATE_DIR: tests only

def load():
    try:
        with open(path()) as fh: st = json.load(fh)
    except Exception: st = {"on": False}
    if st.get("on") and st.get("until") and time.time() > st["until"]:
        st.update(on=False, ended="time up"); save(st)
    return st

def save(st):
    tmp = path() + ".tmp"
    with open(tmp, "w") as fh: json.dump(st, fh, indent=1)
    os.replace(tmp, path())

def secs(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hm]?)", s or "")
    if not m: return None
    n = float(m.group(1)); return int(n * 60) if m.group(2) == "m" else int(n * 3600)

def queue_file():
    try:
        fs = sorted(f for f in os.listdir(QUEUE_DIR) if f.startswith("AGENT_QUEUE_") and f.endswith(".md"))
        return os.path.join(QUEUE_DIR, fs[-1]) if fs else None
    except Exception: return None

def disk_free_gb():
    """Free GB on the home volume (SCRAMBLE_FREE_GB overrides it, for tests and screenshots)."""
    try:
        e = os.environ.get("SCRAMBLE_FREE_GB")
        if e not in (None, ""): return float(e)
        return shutil.disk_usage(os.path.expanduser("~")).free / GIB
    except Exception: return None

def disk_low(free=None):
    free = disk_free_gb() if free is None else free
    return free is not None and free < LOW_DISK_GB

def mode_of(st):
    m = st.get("mode")
    if m in LABEL: return m
    return {12: "cap", 20: "scramble", 25: "surge"}.get(st.get("target_agents", TARGET), "custom")

def label(mode, n): return "%s %d" % (LABEL.get(mode, "Custom"), n)

def left_min(st): return max(0, int((st.get("until", 0) - time.time()) / 60)) if st.get("on") else 0

def turn_on(mode, n, argv_rest):
    """Returns (rc, message). Refuses n > 20 when free disk < 10 GB."""
    free = disk_free_gb()
    if n > NORMAL_MAX and disk_low(free):
        return 1, ("scramble REFUSED: %s needs more than %d agents, but only %.1f GB of disk is free (minimum %d GB). "
                   "More agents means more builds and caches. Free some space (tidy), or use `scramble cap` / `scramble on` (20 or fewer)."
                   % (label(mode, n), NORMAL_MAX, free, LOW_DISK_GB))
    d = secs(argv_rest[0]) if argv_rest and secs(argv_rest[0]) else None
    rest = argv_rest[1:] if d else argv_rest
    d = d or DEFAULT_SECS
    st = {"on": True, "mode": mode, "since": time.time(), "until": time.time() + d, "reason": " ".join(rest) or "owner",
          "target_agents": n, "queue": queue_file()}
    save(st)
    msg = "scramble ON (%s) until %s (%dh %dm): keep %d agents running; queue %s" % (
        LABEL[mode], time.strftime("%-I:%M %p", time.localtime(st["until"])), d // 3600, d % 3600 // 60, n, st["queue"] or "(none yet)")
    if disk_low(free): msg += "\nWARNING: disk low, %.1f GB free (under %d GB). Free some space soon." % (free, LOW_DISK_GB)
    return 0, msg

def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    st = load()
    if cmd in MODES:
        rc, msg = turn_on(MODE_NAME[cmd], MODES[cmd], argv[2:])
        print(msg); return rc
    elif cmd == "set":
        a = argv[2] if len(argv) > 2 else ""
        if not re.fullmatch(r"\d{1,3}", a) or not MIN_N <= int(a) <= MAX_N:
            print("usage: scramble set N [duration] [reason...]   (N from %d to %d)" % (MIN_N, MAX_N)); return 2
        rc, msg = turn_on("custom", int(a), argv[3:])
        print(msg); return rc
    elif cmd == "off":
        st.update(on=False, ended=" ".join(argv[2:]) or "owner"); save(st); print("scramble OFF")
    else:
        free = disk_free_gb(); low = disk_low(free); n = st.get("target_agents", TARGET); mode = mode_of(st)
        if st.get("on"): print("scramble ON (%s), %d min left, target %d agents, queue %s" % (LABEL[mode], left_min(st), n, st.get("queue")))
        else: print("scramble off")
        if low: print("WARNING: disk low, %.1f GB free (under %d GB). Surge and Custom above %d refuse to start until there is room." % (free, LOW_DISK_GB, NORMAL_MAX))
        print(json.dumps({"scramble": bool(st.get("on")), "scramble_left_min": left_min(st), "target_agents": n, "mode": mode,
                          "disk_free_gb": None if free is None else round(free, 1), "disk_low": low}))
    return 0

if __name__ == "__main__": sys.exit(main(sys.argv))
