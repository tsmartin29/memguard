#!/usr/bin/env python3
"""Claude Code statusLine hook for memguard's usage-limit prompt.

Claude Code pipes JSON on stdin that includes rate_limits.five_hour / rate_limits.seven_day {used_percentage, resets_at (epoch s)}
for subscribers. This copies the weekly and 5-hour numbers into usage.json (atomic write) for usagegate.py. Fields it does not
know (extra_enabled, extra_spent, currency) are kept exactly as another script last wrote them, so a spend feed and this hook can
share the file. Prints nothing (set USAGE_STATUSLINE_SHOW=1 to print "wk 97%"). To keep an existing statusline, set
USAGE_STATUSLINE_NEXT=<command>: it receives the same JSON and its output is printed.
Never fails, never reads credentials, no network. usage.json lives in $MEMGUARD_USAGE_DIR, else $MEMGUARD_HOME (default ~/.config/memguard)."""
import datetime, json, os, subprocess, sys, time
D = os.environ.get("MEMGUARD_USAGE_DIR") or os.path.expanduser(os.environ.get("MEMGUARD_HOME", "~/.config/memguard"))
raw = sys.stdin.read()
try:
    j = json.loads(raw)
    rl = j.get("rate_limits") or {}
    wk, h5 = rl.get("seven_day") or {}, rl.get("five_hour") or {}
    iso = lambda t: datetime.datetime.fromtimestamp(float(t), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if wk.get("used_percentage") is not None and wk.get("resets_at"):
        f = os.path.join(D, "usage.json")
        try:
            with open(f) as fh: cur = json.load(fh)
        except Exception: cur = {}
        new = dict(cur, weekly_pct=float(wk["used_percentage"]), weekly_resets_at=iso(wk["resets_at"]), source="statusline",
                   updated=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if h5.get("used_percentage") is not None: new["five_hour_pct"] = float(h5["used_percentage"])
        changed = any(cur.get(k) != new.get(k) for k in ("weekly_pct", "weekly_resets_at", "five_hour_pct"))
        stale = (not os.path.exists(f)) or time.time() - os.path.getmtime(f) > 60   # refresh "updated" at most once a minute
        if changed or stale:
            os.makedirs(D, exist_ok=True)
            tmp = f"{f}.tmp{os.getpid()}"
            with open(tmp, "w") as fh: json.dump(new, fh, indent=1)
            os.replace(tmp, f)
        if os.environ.get("USAGE_STATUSLINE_SHOW") == "1": sys.stdout.write(f"wk {new['weekly_pct']:.0f}%")
except Exception:
    pass
nxt = os.environ.get("USAGE_STATUSLINE_NEXT")
if nxt:
    try: sys.stdout.write(subprocess.run(nxt, shell=True, input=raw, capture_output=True, text=True, timeout=5).stdout)
    except Exception: pass
