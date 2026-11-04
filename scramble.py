#!/usr/bin/env python3
"""SCRAMBLE mode (named after a scramble: launch every jet, now).

  scramble on [duration, default 8h] [reason...]   keep every agent slot filled: your orchestrating session refills each slot from the queue as agents finish
  scramble off [reason...]
  scramble status                                  state, time left, target, queue file

Scramble is a standing order to your orchestrating agent session, not a resource cap: Afterburner gives the Mac more muscle (heavy-lane caps);
Scramble keeps all agent slots busy (target = the Claude Code concurrent-subagent cap, 20). Memory floors and memguard's rules
are unchanged. It drops back to off by itself when the time is up. State: scramble.json {on, since, until, reason, target_agents, queue}.
"""
import json, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = int(os.environ.get("SCRAMBLE_TARGET", "20"))   # Claude Code concurrent-subagent cap by default
QUEUE_DIR = os.path.expanduser(os.environ.get("SCRAMBLE_QUEUE_DIR", "~/.config/memguard"))   # newest AGENT_QUEUE_*.md here
DEFAULT_SECS = 8 * 3600


def path(): return os.path.join(HERE, "scramble.json")

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

def left_min(st): return max(0, int((st.get("until", 0) - time.time()) / 60)) if st.get("on") else 0

def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    st = load()
    if cmd == "on":
        d = secs(argv[2]) if len(argv) > 2 and secs(argv[2]) else None
        rest = argv[3:] if d else argv[2:]
        d = d or DEFAULT_SECS
        st = {"on": True, "since": time.time(), "until": time.time() + d, "reason": " ".join(rest) or "owner",
              "target_agents": TARGET, "queue": queue_file()}
        save(st)
        print("scramble ON until %s (%dh %dm): keep %d agents running; queue %s" % (time.strftime("%-I:%M %p", time.localtime(st["until"])),
              d // 3600, d % 3600 // 60, TARGET, st["queue"] or "(none yet)"))
    elif cmd == "off":
        st.update(on=False, ended=" ".join(argv[2:]) or "owner"); save(st); print("scramble OFF")
    else:
        if st.get("on"): print("scramble ON, %d min left, target %d agents, queue %s" % (left_min(st), st.get("target_agents", TARGET), st.get("queue")))
        else: print("scramble off")
        print(json.dumps({"scramble": bool(st.get("on")), "scramble_left_min": left_min(st), "target_agents": st.get("target_agents", TARGET)}))
    return 0

if __name__ == "__main__": sys.exit(main(sys.argv))
