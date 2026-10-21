#!/usr/bin/env python3
"""Usage-limit prompt for memguard (Claude Code plans): asks you what to do when weekly usage gets high or paid extra usage starts.

Reads ONLY a local file, usage.json (no credentials, keychains, cookies or network calls). usage.json can be written by anything:
  * usage_statusline.py, the Claude Code statusLine hook, fills in weekly_pct, weekly_resets_at and five_hour_pct from the
    rate_limits that Claude Code passes it locally, and
  * any script of yours may add the extra-usage fields (extra_enabled, extra_spent, currency) or write the whole file, from
    wherever you can read your spend (a billing page, an API, a number you type). Fields you do not write are simply unknown.
usage.json shape (all keys optional except what you want checked):
  {"updated": "2026-10-21T17:45:00Z", "source": "statusline",
   "weekly_pct": 100.0, "weekly_resets_at": "2026-10-22T12:00:00Z", "five_hour_pct": 0.0,
   "extra_enabled": true, "extra_spent": 12.50, "currency": "USD"}

Triggers (each at most once per weekly window; state in usage_state.json):
  "95"    weekly all-models percent >= usage_threshold_pct (default 95)
  "extra" extra usage starts: weekly >= 100, or extra_spent rose above the last value seen
Prompt: native macOS dialog (osascript) + the optional notify_command from config.json. Choice -> usage_decision.json:
  keep_going | pause_all (heavyrun refuses new jobs except priority_pattern ones) | pause_heavy (heavy lane holds, light lane continues).
Dismissing the dialog, or leaving it for 10 minutes, means NO action. Decisions expire at the weekly reset. The dialog runs in a
DETACHED child so memguard's memory checks never wait on it.
Env: MEMGUARD_HOME / MEMGUARD_CONFIG (as memguard), MEMGUARD_USAGE_DIR (dir holding all usage files),
  MEMGUARD_NO_DIALOG=1 (never show a dialog; the test suite sets this), USAGE_NOW (ISO time, tests), USAGE_DRYRUN=1,
  USAGE_DRYRUN_CHOICE (keep|all|heavy|none).
CLI: --check | --dry-run | --gate LABEL [--lane heavy|light] [--dir D] | --test-dialog | --ask-now | --status
"""
import datetime, json, os, re, subprocess, sys, time

HOME_DIR = os.path.expanduser(os.environ.get("MEMGUARD_HOME", "~/.config/memguard"))
OPEN_SECS = 600   # dialog gives up (no action) after 10 minutes
BUTTONS = {"Keep going on credits": "keep_going", "Pause new agent work until reset": "pause_all", "Pause heavy jobs only": "pause_heavy"}
DRY_CHOICES = {"keep": "Keep going on credits", "all": "Pause new agent work until reset", "heavy": "Pause heavy jobs only"}
THRESH = 95.0

def udir(d=None): return d or os.environ.get("MEMGUARD_USAGE_DIR") or HOME_DIR
def P(name, d=None): return os.path.join(udir(d), name)
def config(d=None):
    """The few memguard config.json settings used here (same file and defaults as memguard.py)."""
    c = {"priority_pattern": "priority", "usage_threshold_pct": THRESH, "notify_command": ""}
    try:
        with open(os.environ.get("MEMGUARD_CONFIG") or P("config.json", d)) as f: c.update({k: v for k, v in json.load(f).items() if k in c})
    except Exception: pass
    return c
def now_ts():
    v = os.environ.get("USAGE_NOW")
    if v: return parse_iso(v)
    return time.time()
def parse_iso(s):
    s = str(s).strip().replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(s).timestamp()
def iso(ts): return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def log(msg, d=None):
    try:
        with open(P("memguard.log", d), "a") as f: f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "USAGE " + msg + "\n")
    except OSError: pass
def jload(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default
def jwrite(path, obj):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f: json.dump(obj, f, indent=1)
    os.replace(tmp, path)
def window_key(reset_ts):   # reset times jitter by a few hundred ms between sources: round to 5 min
    return iso(round(reset_ts / 300) * 300)
def num(v):
    try: return None if v is None or v == "" else float(v)
    except (TypeError, ValueError): return None

# ---------- text ----------
def local(ts):
    """e.g. "Thursday 7:00 AM CDT" in the Mac's own time zone."""
    return datetime.datetime.fromtimestamp(ts).astimezone().strftime("%A %-I:%M %p %Z")
def money(v, cur="USD"):
    if v is None: return "unknown"
    return ("$" if cur in (None, "", "USD") else cur + " ") + f"{v:,.2f}"
def pct_s(v):
    return "unknown" if v is None else (f"{v:.0f}%" if abs(v - round(v)) < 0.05 else f"{v:.1f}%")

def prompt_text(trigger, u, rise_from=None):
    """Plain-English dialog text for a trigger and a usage dict."""
    wk, spent, cur = num(u.get("weekly_pct")), num(u.get("extra_spent")), u.get("currency") or "USD"
    reset = local(round(parse_iso(u["weekly_resets_at"]) / 300) * 300) if u.get("weekly_resets_at") else "the weekly reset (time unknown)"
    on = u.get("extra_enabled")
    if trigger == "extra":
        head = f"Your weekly Claude usage is at {pct_s(wk)} and work is now running on paid extra usage." if on is not False else \
               f"Your weekly Claude usage is at {pct_s(wk)}. Extra usage is switched off, so Claude will stop at the limit."
        if rise_from is not None and wk is not None and wk < 100:
            head = f"Paid extra usage has started (weekly limit {pct_s(wk)})."
    else:
        head = f"Your weekly Claude usage has reached {pct_s(wk)}. At 100% new work rolls onto paid extra usage."
    lines = [head, "", f"Weekly limit resets: {reset}.", f"Extra usage spent so far: {money(spent, cur)}."]
    if rise_from is not None and spent is not None and trigger == "extra":
        lines.append(f"It was {money(rise_from, cur)} at the last check.")
    lines += ["",
              "Keep going on credits: carry on and keep spending.",
              "Pause new agent work: no new agents, sub-agents or long tasks start until the reset (running ones finish; jobs labelled as priority still run).",
              "Pause heavy jobs only: the heavy queue waits until the reset; light jobs and agents continue.",
              "",
              "Dismiss this and nothing changes (it also closes itself after 10 minutes)."]
    return "\n".join(lines)
def push_text(text):
    first = text.split("\n\n")[0]
    rest = [l for l in text.split("\n") if l.startswith(("Weekly limit resets", "Extra usage spent"))]
    return "\n".join([first] + rest + ["Answer on the Mac."])

# ---------- decision ----------
def active_decision(d=None, now=None):
    """The pause decision in force, or None. Expired at its window_reset."""
    dec = jload(P("usage_decision.json", d), None)
    try:
        if dec and dec.get("choice") in ("pause_all", "pause_heavy") and (now or now_ts()) < parse_iso(dec["window_reset"]): return dec
    except Exception: pass
    return None
def is_priority(label, d=None):
    """Does the queue label match priority_pattern (memguard's setting)?"""
    pat = config(d)["priority_pattern"]
    try: return bool(pat) and bool(re.search(pat, label or "", re.I))
    except re.error: return False
def gate(label, lane="heavy", d=None, now=None):
    """Return (code, message). 0 run/continue, 75 refuse (pause_all), 76 hold (pause_heavy, heavy lane)."""
    dec = active_decision(d, now)
    if not dec or is_priority(label, d): return 0, ""
    until = local(parse_iso(dec["window_reset"]))
    if dec["choice"] == "pause_all":
        return 75, (f"heavyrun: REFUSED - new agent work is paused until {until} (\"Pause new agent work until reset\" was chosen "
                    f"at {dec.get('time', '?')}). Running jobs continue. Priority jobs are exempt. Do not start new agents, sub-agents or long tasks; "
                    f"ask the user if this is urgent. Decision file: {P('usage_decision.json', d)}")
    if dec["choice"] == "pause_heavy" and lane != "light":
        return 76, f"heavy lane is on hold until {until} (\"Pause heavy jobs only\" was chosen); light jobs still run"
    return 0, ""

# ---------- evaluation ----------
def evaluate(u, st, now, thresh=THRESH):
    """Pure: -> (trigger or None, new_state, rise_from). Does nothing if the usage data's weekly window has already ended."""
    wk, spent = num(u.get("weekly_pct")), num(u.get("extra_spent"))
    try: rts = parse_iso(u["weekly_resets_at"])
    except Exception: return None, st, None
    if rts <= now or wk is None: return None, st, None   # stale window: wait for a fresh feed
    key = window_key(rts)
    st = dict(st)
    if st.get("window") != key:
        st = {"window": key, "window_reset": key, "fired_95": False, "fired_extra": False, "last_extra_spent": st.get("last_extra_spent")}
    last = num(st.get("last_extra_spent"))
    rise = spent is not None and last is not None and spent > last + 0.004
    trig, rise_from = None, None
    if (wk >= 100 or rise) and not st["fired_extra"]:
        trig, rise_from = "extra", (last if rise else None); st["fired_extra"] = st["fired_95"] = True
    elif wk >= thresh and not st["fired_95"]:
        trig = "95"; st["fired_95"] = True
    if spent is not None: st["last_extra_spent"] = spent
    st["window_reset"] = key
    return trig, st, rise_from

# ---------- dialog ----------
def osa_q(s): return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
def show_dialog(text, secs=OPEN_SECS, title="Claude usage"):
    """Real macOS dialog. Returns the button label, or None (dismissed / gave up / error / MEMGUARD_NO_DIALOG=1)."""
    if os.environ.get("MEMGUARD_NO_DIALOG") == "1": return None   # the test suite sets this: nothing ever appears on screen
    btns = ", ".join(osa_q(b) for b in BUTTONS)
    scr = (f'with timeout of {secs + 60} seconds\n tell application "System Events"\n activate\n'
           f' set r to display dialog {osa_q(text)} with title {osa_q(title)} buttons {{{btns}}} default button 1 with icon caution giving up after {secs}\n'
           f' if gave up of r then return "GAVEUP"\n return button returned of r\n end tell\nend timeout')
    try:
        r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=secs + 90)
    except Exception: return None
    out = r.stdout.strip()
    return out if out in BUTTONS else None

def run_prompt(payload, d=None, dry=False, test=False):
    """Push + dialog + write the decision. Returns the choice string or None."""
    text = payload["text"]
    if test: text = "TEST: no action needed. Pressing any button here does nothing (it only prints what it WOULD write).\n\n" + text
    push = push_text(payload["text"])
    if dry or test:
        print("NOTIFY (not sent):", push.replace("\n", " | "))
    else:
        cmd = config(d)["notify_command"]   # optional, e.g. a curl to ntfy.sh; gets $MEMGUARD_MESSAGE and $MEMGUARD_TITLE
        if cmd:
            try: subprocess.run(cmd, shell=True, input=push, text=True, capture_output=True, timeout=30,
                                env=dict(os.environ, MEMGUARD_MESSAGE=push, MEMGUARD_TITLE="Claude usage"))
            except Exception as e: log(f"notify_command failed: {e!r}", d)
    if os.environ.get("MEMGUARD_NO_DIALOG") == "1" and not dry: print("(dialog suppressed by MEMGUARD_NO_DIALOG=1)")
    if dry:
        print("DIALOG TITLE: Claude usage\nDIALOG TEXT:\n" + text + "\nBUTTONS: " + " | ".join(BUTTONS))
        label = DRY_CHOICES.get(os.environ.get("USAGE_DRYRUN_CHOICE", "none"))
        print("CHOICE:", label or "(dismissed: no action)")
    else:
        label = show_dialog(text, secs=45 if test else OPEN_SECS)
    choice = BUTTONS.get(label)
    rec = {"choice": choice, "time": iso(time.time() if not os.environ.get("USAGE_NOW") else now_ts()), "window_reset": payload["window_reset"],
           "trigger": payload["trigger"], "window_reset_local": local(parse_iso(payload["window_reset"]))}
    if test:
        print("TEST: would write usage_decision.json =", json.dumps(rec) if choice else "nothing (dismissed or timed out)"); return choice
    if choice:
        jwrite(P("usage_decision.json", d), rec); log(f"DECISION {choice} (trigger {payload['trigger']}) until {payload['window_reset']}", d)
    else: log(f"DIALOG dismissed or timed out (trigger {payload['trigger']}): no action", d)
    if dry and choice: print("WROTE usage_decision.json:", json.dumps(rec))
    return choice

def alive(pid):
    try: os.kill(int(pid), 0); return True
    except PermissionError: return True
    except (OSError, ValueError): return False

def child_main(payload_file, d=None):
    lock = P("usage_prompt.lock", d)
    try:
        payload = jload(payload_file, None)
        if not payload: return
        jwrite(lock, {"pid": os.getpid(), "since": iso(time.time())})
        run_prompt(payload, d)
    finally:
        for f in (lock, payload_file):
            try: os.remove(f)   # own temp state files only
            except OSError: pass

def check(d=None, dry=False):
    """One cycle (memguard calls this every 15 s). Never raises into memguard; the dialog runs detached."""
    d = udir(d)
    dec_f = P("usage_decision.json", d)
    dec = jload(dec_f, None)
    if dec and dec.get("window_reset"):   # decisions clear automatically at the reset
        try:
            if now_ts() >= parse_iso(dec["window_reset"]):
                os.replace(dec_f, P("usage_decision.last.json", d)); log(f"decision {dec.get('choice')} expired at window reset: everything resumes", d)
        except Exception as e: log(f"decision expiry error: {e!r}", d)
    lock = jload(P("usage_prompt.lock", d), None)
    if lock and alive(lock.get("pid")): return None   # a dialog is open: evaluate after it closes
    u = jload(P("usage.json", d), None)
    if not isinstance(u, dict): return None
    st = jload(P("usage_state.json", d), {})
    trig, st2, rise_from = evaluate(u, st, now_ts(), float(config(d)['usage_threshold_pct']))
    if st2 != st: jwrite(P("usage_state.json", d), st2)
    if not trig: return None
    text = prompt_text(trig, u, rise_from)
    payload = {"trigger": trig, "text": text, "window_reset": st2["window_reset"], "created": iso(now_ts())}
    log(f"TRIGGER {trig}: weekly {u.get('weekly_pct')}% extra spent {u.get('extra_spent')} reset {st2['window_reset']}", d)
    if dry:
        jwrite(P("usage_prompt_pending.json", d), payload); run_prompt(payload, d, dry=True)
        try: os.remove(P("usage_prompt_pending.json", d))
        except OSError: pass
        return trig
    pf = P("usage_prompt_pending.json", d); jwrite(pf, payload)
    env = dict(os.environ); env["MEMGUARD_USAGE_DIR"] = d
    subprocess.Popen(["/usr/bin/python3", "-I", os.path.abspath(__file__), "--child", pf], env=env, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return trig

def main(argv):
    a = argv[1:]
    def opt(n, default=None): return a[a.index(n) + 1] if n in a and a.index(n) + 1 < len(a) else default
    d = opt("--dir")
    if "--gate" in a:   # heavyrun: exit 0 go, 75 refuse, 76 hold. Fails open.
        try:
            code, msg = gate(opt("--gate", ""), opt("--lane", "heavy"), d)
            if msg: print(msg)
            return code
        except Exception: return 0
    if "--child" in a: child_main(opt("--child"), d); return 0
    if "--check" in a: check(d, dry="--dry-run" in a or os.environ.get("USAGE_DRYRUN") == "1"); return 0
    if "--dry-run" in a: check(d, dry=True); return 0
    if "--status" in a:
        print(json.dumps({"usage": jload(P("usage.json", d), None), "state": jload(P("usage_state.json", d), None),
                          "decision": active_decision(d)}, indent=1)); return 0
    if "--test-dialog" in a:   # ONE real dialog, marked TEST, wired to dry-run
        u = {"weekly_pct": 95.0, "weekly_resets_at": "2026-10-22T12:00:00Z", "extra_enabled": True, "extra_spent": 1723.71, "currency": "USD"}
        run_prompt({"trigger": "test", "text": prompt_text("95", u), "window_reset": "2026-10-22T12:00:00Z"}, d, test=True); return 0
    if "--ask-now" in a:   # show the prompt now, from the current usage.json
        u = jload(P("usage.json", d), None)
        if not u: print("no usage.json"); return 1
        st = jload(P("usage_state.json", d), {}); rts = parse_iso(u["weekly_resets_at"])
        pl = {"trigger": "manual", "text": prompt_text("extra" if (num(u.get("weekly_pct")) or 0) >= 100 else "95", u), "window_reset": iso(round(rts / 300) * 300)}
        pf = P("usage_prompt_pending.json", d); jwrite(pf, pl)
        subprocess.Popen(["/usr/bin/python3", "-I", os.path.abspath(__file__), "--child", pf] + (["--dir", d] if d else []), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print("dialog opened"); return 0
    print(__doc__); return 2

if __name__ == "__main__":
    sys.exit(main(sys.argv))
