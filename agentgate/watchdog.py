#!/usr/bin/env python3
"""Spend watchdog for one agent (started by agentgate.py register). Every 60 s: agentcost.py on its transcript -> cost in the state file.
At warn_at (default 95%) of the cap: over=true (agentgate alerts tell the coordinating session to stop it) and one push_cmd call, if set.
Ends when the agent is finished, or its transcript has not changed for 15 minutes."""
import json, os, re, shlex, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agentgate import cfg
c = cfg()
p = os.path.join(c["state_dir"], sys.argv[1] + ".json")
COST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentcost.py")
pushed = False
while True:
    try:
        d = json.load(open(p))
        if d.get("state") != "running": break
        r = subprocess.run([sys.executable, COST, d["out"]], capture_output=True, text=True, timeout=60).stdout.strip()
        m = re.search(r"^\$([0-9.]+)", r.splitlines()[-1] if r else "")
        if m: d["cost"] = float(m.group(1))
        if d["cost"] >= float(c["warn_at"]) * d["cap"]:
            d["over"] = True
            if not pushed and c.get("push_cmd"):
                subprocess.run(shlex.split(c["push_cmd"]) + ["Agent %s at $%.2f of $%.2f cap" % (d["id"], d["cost"], d["cap"])], timeout=20)
                pushed = True
        if time.time() - os.path.getmtime(d["out"]) > 900: d["state"], d["finished"] = "finished", time.time()
        json.dump(d, open(p, "w"))
        if d["state"] != "running": break
    except Exception:
        pass
    time.sleep(60)
