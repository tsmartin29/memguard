#!/usr/bin/env python3
"""agentgate: agent oversight enforced by hooks, not by prompts.

Why: a cap written in an agent's prompt does nothing, because a subagent cannot see its own spend. On one day three test agents each told
"hard stop at $3.50" spent $8.10, $6.34 and $5.18 and wrote no report. Rules only hold when a mechanism enforces them.

One script, four hook roles (first argument):
  gate      PreToolUse  Agent|Task : refuse a launch unless the prompt has a Department, a Role (the professional the agent is and the standard
                                     its output must meet), a Head brief (scope, setup, done-criteria) and a Cap ($), no other watched agent is
                                     running (max_running, default 1; a prompt line "Parallel: owner-approved" lifts it) and none is over its cap.
  register  PostToolUse Agent|Task : record the agent (id, transcript, department, cap) and start its spend watchdog (watchdog.py).
  stop      SubagentStop           : mark it finished; its work now needs a Head review.
  alerts    PostToolUse (all) and UserPromptSubmit : tell the coordinating session on every step about an agent over its cap (stop it) or a
                                     finished agent with no review file yet (<reviews_dir>/<agent id>.md).
Exit 2 + stderr = block. Internal errors allow (fail open). Config: ~/.config/agentgate/config.json (see config.example.json)."""
import glob, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {"departments": ["Firmware", "Apps", "QA", "Security", "Research", "Product", "Growth"], "max_running": 1,
            "reviews_dir": "~/.claude/agent_reviews", "state_dir": "~/.claude/agent_watch", "warn_at": 0.95, "default_cap": 5.0,
            "push_cmd": "", "exempt_types": ["web-fetch", "claude-code-guide", "statusline-setup"], "require_file": ""}

def cfg():
    c = dict(DEFAULTS)
    try:
        with open(os.path.expanduser(os.environ.get("AGENTGATE_CONFIG", "~/.config/agentgate/config.json"))) as f: c.update(json.load(f))
    except Exception: pass
    for k in ("reviews_dir", "state_dir"): c[k] = os.path.expanduser(c[k])
    os.makedirs(c["state_dir"], exist_ok=True)
    return c

def load(p):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return None

def watched(c): return [d for d in (load(p) for p in glob.glob(os.path.join(c["state_dir"], "*.json"))) if d]
def running(c): return [d for d in watched(c) if d.get("state") == "running"]
def emit(event, text): print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))

def problems(prompt, c, live):
    """What a launch prompt is missing (empty list = allowed). Pure, for tests."""
    p, errs = prompt or "", []
    dm = re.search(r"Department:\s*([^\n]+)", p)
    if not dm or not any(d.lower() in dm.group(1).lower() for d in c["departments"]):
        errs.append("a 'Department: <one of %s>' line" % ", ".join(c["departments"]))
    ro = re.search(r"Role:\s*(.+)", p)
    if not ro or len(ro.group(1).strip()) < 30:
        errs.append("a 'Role: <the professional this agent is, and the standard its output must meet>' line (30+ characters)")
    hb = re.search(r"Head brief:\s*(.+)", p)
    if not hb or len(hb.group(1).strip()) < 40:
        errs.append("a 'Head brief: <scope, setup, done-criteria>' line (40+ characters)")
    if not re.search(r"Cap:\s*\$\s*\d+(\.\d+)?", p):
        errs.append("a 'Cap: $N' line (the watchdog enforces it)")
    if c.get("require_file") and c["require_file"] not in p:
        errs.append("a pointer to %s" % c["require_file"])
    over = [d for d in live if d.get("over")]
    if over: errs.append("no agent over its cap: stop %s first" % ", ".join(d["id"] for d in over))
    if len(live) >= int(c["max_running"]) and "Parallel: owner-approved" not in p:
        errs.append("fewer than %d agents running (running: %s); add 'Parallel: owner-approved' only if the owner said so"
                    % (int(c["max_running"]), ", ".join(d["id"] for d in live)))
    return errs

def gate(inp, c):
    ti = inp.get("tool_input") or {}
    if (ti.get("subagent_type") or "") in c["exempt_types"]: return 0
    errs = problems(ti.get("prompt"), c, running(c))
    if errs:
        sys.stderr.write("AGENT LAUNCH REFUSED (agentgate). The prompt needs:\n- " + "\n- ".join(errs) + "\n"); return 2
    return 0

def register(inp, c):
    ti = inp.get("tool_input") or {}
    if (ti.get("subagent_type") or "") in c["exempt_types"]: return 0
    resp = json.dumps(inp.get("tool_response", ""))
    aid, out = re.search(r"\\b(a[0-9a-f]{16})\\b", resp) or re.search(r"agentId\\W+([0-9a-f]{8,})", resp), re.search(r"output_?[Ff]ile\\W+(/[^\\s\"\\\\]+?\\.output)", resp)
    if not aid or not out: return 0
    p = ti.get("prompt") or ""
    capm, dm = re.search(r"Cap:\s*\$\s*(\d+(?:\.\d+)?)", p), re.search(r"Department:\s*([^\n]+)", p)
    d = dict(id=aid.group(1), out=out.group(1), cap=float(capm.group(1)) if capm else float(c["default_cap"]), dept=dm.group(1).strip() if dm else "?",
             desc=ti.get("description", ""), state="running", started=time.time(), cost=0.0, over=False)
    with open(os.path.join(c["state_dir"], d["id"] + ".json"), "w") as f: json.dump(d, f)
    subprocess.Popen([sys.executable, os.path.join(HERE, "watchdog.py"), d["id"]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    emit("PostToolUse", "Agent %s registered: %s, cap $%.2f, watchdog on. Not done until its Head writes %s/%s.md."
         % (d["id"], d["dept"], d["cap"], c["reviews_dir"], d["id"]))
    return 0

def stop(inp, c):
    blob = json.dumps(inp)
    for d in running(c):
        if d["id"] in blob:
            d["state"], d["finished"] = "finished", time.time()
            with open(os.path.join(c["state_dir"], d["id"] + ".json"), "w") as f: json.dump(d, f)
    return 0

def alerts(inp, c):
    msgs = []
    for d in watched(c):
        if d.get("over") and d.get("state") == "running":
            msgs.append("STOP NOW: agent %s (%s) has spent $%.2f of its $%.2f cap." % (d["id"], d.get("desc", ""), d.get("cost", 0), d["cap"]))
        if d.get("state") == "finished" and not os.path.exists(os.path.join(c["reviews_dir"], d["id"] + ".md")):
            msgs.append("HEAD REVIEW MISSING: agent %s (%s, %s) finished at $%.2f; review it (%s/%s.md) before calling it done."
                        % (d["id"], d.get("desc", ""), d.get("dept", ""), d.get("cost", 0), c["reviews_dir"], d["id"]))
    if msgs: emit("UserPromptSubmit" if inp.get("hook_event_name") == "UserPromptSubmit" else "PostToolUse", "\n".join(msgs))
    return 0

if __name__ == "__main__":
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    try: inp = json.load(sys.stdin)
    except Exception: inp = {}
    try:
        c = cfg()
        sys.exit({"gate": gate, "register": register, "stop": stop, "alerts": alerts}.get(role, lambda i, c: 0)(inp, c))
    except SystemExit: raise
    except Exception: sys.exit(0)
