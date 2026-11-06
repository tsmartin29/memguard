#!/usr/bin/env python3
"""agentq: an agent queue that keeps itself up to date.

The old queue was a hand-edited markdown file; when the orchestrating session forgot to edit it, it went stale. Now Claude Code hooks
write every agent start and finish to an append-only log, and the markdown view is regenerated from it. Nobody has to remember anything.

  agentq hook                       hook mode: reads one Claude Code hook payload (JSON) on stdin. Never blocks, always exits 0.
                                    PostToolUse (Agent/Task) and SubagentStart -> STARTED, SubagentStop -> FINISHED.
  agentq add [--top] "<task>"       add a PENDING item (the backlog); prints its id (Q1, Q2, ...); --top makes it priority
  agentq top <id> [id ...]          promote pending items to priority (Q3 or 3)
  agentq done <id> [id ...] [note]  mark PENDING items done (Q3 or 3); several ids at once, the first non-id word starts the note
  agentq status                     running / finished today / pending counts, newest entry age, STALE flag
  agentq list [dept]                status plus the running agents (grouped by department) and the pending items; a dept name filters
  agentq depts                      running agents per department; "needs supervisor" when one has more than 10
  agentq tag <R#|agent id> <dept>   put an agent that is already running into a department (R# as numbered by `list`)
  agentq render [YYYY-MM-DD]        regenerate the dated markdown view (default today)
  agentq backfill <subagents_dir> [YYYY-MM-DD]
                                    reconstruct agents from Claude Code subagent transcripts (agent-*.meta.json + .jsonl) for one day
                                    (default today); skips agents already in the log

Keeping "Next up" current: nobody has to close items by hand any more.
  order     priority items first, then NEWEST first (never oldest first), everywhere the backlog is shown.
  [Q14]     an agent whose description or prompt contains [Q14] marks pending item Q14 "in progress" when it starts (it leaves the
            pending list) and done when it finishes. A lost agent (no finish after AGENTQ_LOST_HOURS) releases the item again.
  maybe     a finished agent whose description closely matches a pending item flags it "maybe done" in `list` (never auto-closed).
  age       pending items older than AGENTQ_AGE_HOURS (24) show their age; `status` reports how many.

Files (AGENTQ_DIR, default ~/.config/memguard/queue):
  agents.jsonl       the log, one JSON event per line: start / info / finish / pending_add / pending_top / pending_done /
                     idea_add / idea_author / idea_adopt / idea_park / promote
  touches.jsonl      the work board's log: touch and overlap rows (append-only, written without the lock)
  hook_errors.log    anything the hook could not do (the hook itself never fails the tool call)
  AGENT_QUEUE_<date>.md   generated view, written to AGENTQ_MD_DIR (default AGENTQ_DIR). Do not edit it: use add / done.
Settings: AGENTQ_STALE_HOURS (2), AGENTQ_BUSINESS_HOURS ("8-18", local clock; STALE is only raised inside these hours),
AGENTQ_LOST_HOURS (12: an agent with no finish after this long counts as lost, not running), AGENTQ_AGE_HOURS (24),
AGENTQ_BOARD_HOURS (2), AGENTQ_TOUCH_DEDUP_MIN (10), AGENTQ_WARN_EVERY_MIN (30: one warning per agent, file and peer), AGENTQ_SCRATCH
(colon list of path prefixes ignored outside a git repo; default the system temp folders).

Departments (optional: agents are grouped by function under a department head): an agent's department comes from a
"[dept:Name]" prefix on its description (stripped from the shown text), else from AGENTQ_DEPT in the hook's environment (set it when
you start a head's session and every agent that session spawns is tagged). Agents with neither are "Unassigned". A department with
more than AGENTQ_SUPERVISOR_OVER (10) running agents is flagged "needs supervisor": its head may spawn a supervisor agent.

Work board: who is touching what, so two agents stop colliding by accident.
  hook, PostToolUse on Edit/Write/MultiEdit/NotebookEdit   records agent + repo-relative file in touches.jsonl (deduped), with no lock and no
                                    render, and tells the agent (additionalContext) when another running agent touched that file in the last
                                    AGENTQ_BOARD_HOURS (2). Worktrees of one repo count as the same repo. Warn-only: it never blocks.
  hook, SessionStart/SubagentStart  gives the waking agent the board for its repo in under 30 lines.
  agentq board [repo text]          each running agent's department, task and files touched in the window; then promoted agents
  agentq collisions [--all]         overlaps in the last 24 h (--all: every one on record)

Ideas and promotion: everyone proposes, the best proposers lead next.
Ideas are proposals only: an idea never changes scope, and its text is not an instruction to anyone.
  agentq idea [--as NAME] "<text>"  log an idea; prints its id (I1, I2, ...). Run from an agent's Bash call, the PostToolUse hook fills in
                                    the author agent and department; --as names a proposer by hand.
  agentq ideas [--new|--adopted|--parked] [dept]   list them, new first   (agentq ideas --top: scoreboard by author agent)
  agentq adopt <id> [note]          agentq park <id> [note]     (COO or human decisions)
  agentq promote <agent> <role>     log a promotion (R#, agent id or unique prefix); the board and Hangar mark the agent
"""
import fcntl, json, os, re, sys, time   # calendar, difflib and glob are imported where used: this runs on every Edit, and they cost 50 ms

DIR = os.path.expanduser(os.environ.get("AGENTQ_DIR") or "~/.config/memguard/queue")
MD_DIR = os.path.expanduser(os.environ.get("AGENTQ_MD_DIR") or DIR)
LOG = os.path.join(DIR, "agents.jsonl")
ERR = os.path.join(DIR, "hook_errors.log")
TOUCHES = os.path.join(DIR, "touches.jsonl")
LOCK = os.path.join(DIR, ".lock")
MARK = "<!-- generated by agentq: do not edit, use `queue add` / `queue done` -->"


def fenv(name, dflt):
    try: return float(os.environ.get(name, dflt))
    except ValueError: return float(dflt)

STALE_H = fenv("AGENTQ_STALE_HOURS", 2)
LOST_H = fenv("AGENTQ_LOST_HOURS", 12)
AGE_H = fenv("AGENTQ_AGE_HOURS", 24)
SUP_OVER = int(fenv("AGENTQ_SUPERVISOR_OVER", 10))
BOARD_H = fenv("AGENTQ_BOARD_HOURS", 2)
DEDUP_S = fenv("AGENTQ_TOUCH_DEDUP_MIN", 10) * 60
WARN_EVERY_S = fenv("AGENTQ_WARN_EVERY_MIN", 30) * 60
TAIL_BYTES = 4 << 20       # the hook reads only the end of touches.jsonl; 4 MB is far more than a 2 h window
BOARD_MAX_LINES = 29       # the session-start board stays under 30 lines
NO_REPO = "(no repo)"
SCRATCH = tuple(x for x in os.environ.get("AGENTQ_SCRATCH", "/tmp/:/private/tmp/:/var/folders/:/private/var/folders/").split(":") if x)
NO_DEPT = "Unassigned"
DEPT_RE = re.compile(r"^\s*\[\s*dept\s*:\s*([^\]\n]{1,60})\]\s*", re.I)

def clean_dept(name):
    name = re.sub(r"\s+", " ", re.sub(r"[^\w &/+.-]", "", str(name or ""))).strip()
    return name[:40]

def split_dept(text):
    """'[dept:QA] Run the suite' -> ('QA', 'Run the suite'); no prefix -> ('', text)."""
    text = str(text or "")
    m = DEPT_RE.match(text)
    if not m: return "", text
    return clean_dept(m.group(1)), text[m.end():]

QTAG_RE = re.compile(r"\[\s*Q(\d+)\s*\]", re.I)

def find_qid(*texts):
    """First '[Q14]' tag in the given texts -> 'Q14' ('' when none)."""
    for t in texts:
        m = QTAG_RE.search(str(t or ""))
        if m: return "Q%d" % int(m.group(1))
    return ""

def age_text(secs):
    h = int(secs // 3600)
    return "%dh" % h if h < 48 else "%dd" % (h // 24)

def env_dept(): return clean_dept(os.environ.get("AGENTQ_DEPT", ""))

def business_hours():
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", os.environ.get("AGENTQ_BUSINESS_HOURS", "8-18"))
    return (int(m.group(1)), int(m.group(2))) if m else (8, 18)


def one_line(s, n=140):
    s = re.sub(r"[*`#|>]+", "", str(s or "")).strip()
    for ln in s.splitlines():
        ln = ln.strip()
        if ln: return ln if len(ln) <= n else ln[: n - 1].rstrip() + "…"
    return ""

def day(ts): return time.strftime("%Y-%m-%d", time.localtime(ts))
def hm(ts): return time.strftime("%-I:%M %p", time.localtime(ts))


# ---------------------------------------------------------------- log
def read_events():
    out = []
    try:
        with open(LOG, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln: continue
                try:
                    e = json.loads(ln)
                    if isinstance(e, dict): out.append(e)
                except ValueError:
                    pass   # a torn line never stops the rest
    except FileNotFoundError:
        pass
    return out

class Locked:
    def __enter__(self):
        os.makedirs(DIR, exist_ok=True)
        self.fh = open(LOCK, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self
    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN); self.fh.close()

def append(events):
    """Append events (under the lock) and regenerate today's view."""
    with Locked():
        with open(LOG, "a", encoding="utf-8") as fh:
            for e in events:
                e.setdefault("ts", time.time())
                fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
        render()


# ---------------------------------------------------------------- fold
def fold(events, now=None):
    """-> (agents: list of dicts, pending: list of dicts, newest_ts). Agents keyed by agent id; tool_use ids alias to it."""
    now = now or time.time()
    alias = {}
    for e in events:
        if e.get("id") and e.get("tuid") and e["id"] != e["tuid"]: alias[e["tuid"]] = e["id"]
    agents, pend, newest = {}, {}, 0.0
    for e in events:
        ts = float(e.get("ts") or 0); newest = max(newest, ts); ev = e.get("ev")
        if ev in ("start", "info", "finish"):
            k = e.get("id") or e.get("tuid")
            if not k: continue
            k = alias.get(k, k)
            a = agents.setdefault(k, {"id": k, "start": None, "end": None, "last_start": 0.0, "last_end": 0.0})
            for f in ("desc", "model", "type", "prompt", "session", "proj", "src", "dept", "qid"):
                if e.get(f) and not a.get(f): a[f] = e[f]
            if e.get("retag") and e.get("dept"): a["dept"] = e["dept"]   # `agentq tag` overrides the first tag
            if ev in ("start", "info"):
                a["start"] = ts if a["start"] is None else min(a["start"], ts)
                if ev == "start" and e.get("via") != "post": a["last_start"] = max(a["last_start"], ts)   # PostToolUse can arrive after the finish
            else:
                a["end"] = max(a["end"] or 0, ts); a["last_end"] = max(a["last_end"], ts)
                r = e.get("result") or ""
                if r and not a.get("result") and not r.startswith("This agent's report was delivered"): a["result"] = r   # the agent's own last message wins
        elif ev == "pending_add" and e.get("pid"):
            pend[e["pid"]] = {"pid": e["pid"], "text": e.get("text", ""), "added": ts, "done": None, "note": "",
                              "top": ts if e.get("top") else 0.0, "started": None, "maybe": ""}
        elif ev == "pending_top" and e.get("pid") in pend:
            pend[e["pid"]]["top"] = ts
        elif ev == "pending_done" and e.get("pid") in pend:
            pend[e["pid"]].update(done=ts, note=e.get("note", ""))
    out = []
    for a in agents.values():
        if a["start"] is None: a["start"] = a["end"] or 0
        if a["last_end"] and a["last_end"] >= a["last_start"]: a["status"] = "finished"
        elif now - a["start"] > LOST_H * 3600: a["status"] = "lost"
        else: a["status"] = "running"
        out.append(a)
    out.sort(key=lambda a: a["start"])
    for a in out:   # [Q14] link: a running agent starts the item, a finished one closes it; a lost one releases it
        p = pend.get(a.get("qid", ""))
        if not p or p["done"]: continue
        if a["status"] == "finished" and a["end"] >= p["added"]: p.update(done=a["end"], note="auto: agent finished (%s)" % one_line(a.get("desc") or a["id"], 60))
        elif a["status"] == "running": p["started"] = max(p["started"] or 0, a["start"])
    return out, sorted(pend.values(), key=lambda p: int(re.sub(r"\D", "", p["pid"]) or 0)), newest

def pend_order(p):
    """Priority items first (most recently promoted first), then NEWEST first. Never oldest first."""
    n = int(re.sub(r"\D", "", p["pid"]) or 0)
    return (0, -p["top"], -n) if p["top"] else (1, 0, -n)

WORDS_SKIP = set("the and for with from into that this are was our all not you your its has have will new add run per out off but can".split())
def tokens(s): return {w for w in re.findall(r"[a-z0-9]+", str(s or "").lower()) if len(w) > 2 and w not in WORDS_SKIP}

def close_match(task, desc):
    """A finished agent's description closely matches a pending item: most of the shorter one's words are shared (and at least two), or the strings are alike."""
    a, b = tokens(task), tokens(desc)
    if len(a & b) >= 2 and len(a & b) / min(len(a), len(b)) >= 0.6: return True
    import difflib
    return difflib.SequenceMatcher(None, str(task).lower(), str(desc).lower()).ratio() >= 0.6

def flag_maybe(queued, agents):
    for p in queued:
        for a in reversed(agents):   # newest agent first
            d = a.get("desc")
            if a["status"] == "finished" and d and a["end"] >= p["added"] and a.get("qid") != p["pid"] and close_match(p["text"], d):
                p["maybe"] = one_line(d, 60); break

def summary(now=None):
    now = now or time.time()
    events = read_events()
    agents, pend, newest = fold(events, now)
    ideas, promos = fold_ideas(events)
    today = day(now)
    run = [a for a in agents if a["status"] == "running"]
    fin = [a for a in agents if a["status"] == "finished" and day(a["end"]) == today]
    lost = [a for a in agents if a["status"] == "lost" and day(a["start"]) == today]
    openp = [p for p in pend if not p["done"] and not p["started"]]
    prog = [p for p in pend if not p["done"] and p["started"]]
    openp.sort(key=pend_order); prog.sort(key=lambda p: -p["started"])
    flag_maybe(openp, agents)
    for p in openp: p["age"] = now - p["added"]
    aged = [p for p in openp if p["age"] > AGE_H * 3600]
    by_dept = {}
    for a in run: by_dept[a.get("dept") or NO_DEPT] = by_dept.get(a.get("dept") or NO_DEPT, 0) + 1
    lo, hi = business_hours()
    hour = time.localtime(now).tm_hour
    age = now - newest if newest else None
    stale = (age is None or age > STALE_H * 3600) and lo <= hour < hi
    return {"running": len(run), "finished_today": len(fin), "lost_today": len(lost), "pending": len(openp), "in_progress": len(prog), "aged": len(aged), "maybe_done": len([p for p in openp if p["maybe"]]),
            "next_up": [p["pid"] for p in openp[:3]],
            "newest": newest or None, "age_min": None if age is None else int(age // 60), "stale": stale,
            "by_dept": by_dept, "needs_supervisor": sorted(d for d, n in by_dept.items() if n > SUP_OVER and d != NO_DEPT),
            "ideas_new": len([i for i in ideas if i["status"] == "new"]), "promoted": len(promos),
            "_run": run, "_fin": fin, "_lost": lost, "_pend": openp, "_prog": prog, "_agents": agents, "_allpend": pend}


# ---------------------------------------------------------------- markdown view
def cell(s): return one_line(s, 160).replace("|", "/")

def render(date=None):
    now = time.time()
    date = date or day(now)
    s = summary(now)
    agents = s["_agents"]
    started_today = [a for a in agents if day(a["start"]) == date]
    run = [a for a in agents if a["status"] == "running"]
    fin = [a for a in started_today if a["status"] == "finished"] + \
          [a for a in agents if a["status"] == "finished" and day(a["start"]) != date and day(a["end"]) == date]
    lost = [a for a in started_today if a["status"] == "lost"]
    done_today = [p for p in s["_allpend"] if p["done"] and day(p["done"]) == date]
    pretty = time.strftime("%a %b %-d, %Y", time.strptime(date, "%Y-%m-%d"))
    L = ["# Agent queue, %s" % pretty, "", MARK, "",
         "Written automatically by Claude Code hooks (every agent start and finish) and by `queue add` / `queue done`."
         " Source of truth: `queue/agents.jsonl`. Times are local.", "",
         "Newest entry: %s%s" % (hm(s["newest"]) if s["newest"] else "none",
                                 " (STALE: nothing for %d min)" % s["age_min"] if s["stale"] and s["age_min"] is not None else ""), "",
         "## Running (%d)" % len(run), "", "| # | Job | Started | Model | Agent | Dept |", "|---|---|---|---|---|---|"]
    for i, a in enumerate(run, 1):
        L.append("| R%d | %s | %s | %s | %s | %s |" % (i, cell(a.get("desc") or a.get("prompt") or a["id"]), hm(a["start"]), cell(a.get("model", "")),
                                                    cell(a.get("type", "")), cell(a.get("dept", ""))))
    if any(a.get("dept") for a in run):   # only once departments are in use, so untagged setups look as before
        L += ["", "## Departments (running agents)", "", "| Dept | Running | Note |", "|---|---|---|"]
        for d, n in sorted(s["by_dept"].items(), key=lambda kv: (kv[0] == NO_DEPT, -kv[1], kv[0])):
            L.append("| %s | %d | %s |" % (cell(d), n, "needs supervisor (over %d)" % SUP_OVER if d in s["needs_supervisor"] else ""))
    if s["_prog"]:
        L += ["", "## IN PROGRESS (an agent tagged [Qn] is working on it) (%d)" % len(s["_prog"]), "", "| # | Task | Since |", "|---|---|---|"]
        for p in s["_prog"]: L.append("| %s | %s | %s |" % (p["pid"], cell(p["text"]), hm(p["started"])))
    L += ["", "## PENDING (queued, priority first, then newest first) (%d)" % len(s["_pend"]), "", "| # | Task | Added | Flags |", "|---|---|---|---|"]
    for p in s["_pend"]:
        L.append("| %s | %s | %s | %s |" % (p["pid"], cell(p["text"]), time.strftime("%b %-d %-I:%M %p", time.localtime(p["added"])), "; ".join(flags(p))))
    L += ["", "## Finished (%d)" % len(fin), "", "| # | Job | Started | Finished | Model | Result | Dept |", "|---|---|---|---|---|---|---|"]
    for i, a in enumerate(fin, 1):
        L.append("| F%d | %s | %s | %s | %s | %s | %s |" % (i, cell(a.get("desc") or a.get("prompt") or a["id"]), hm(a["start"]), hm(a["end"]),
                                                         cell(a.get("model", "")), cell(a.get("result", "")), cell(a.get("dept", ""))))
    if lost:
        L += ["", "## Lost (no finish after %g h) (%d)" % (LOST_H, len(lost)), ""] + ["- %s (started %s)" % (cell(a.get("desc") or a["id"]), hm(a["start"])) for a in lost]
    if done_today:
        L += ["", "## PENDING done today (%d)" % len(done_today), ""] + \
             ["- ~~%s~~ %s (%s)%s" % (p["pid"], cell(p["text"]), hm(p["done"]), (" " + cell(p["note"])) if p["note"] else "") for p in done_today]
    os.makedirs(MD_DIR, exist_ok=True)
    path = os.path.join(MD_DIR, "AGENT_QUEUE_%s.md" % date)
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh: old = fh.read()
        if MARK not in old:   # a hand-written file for this date: keep it once as a .bak, never lose it
            bak = path + ".handwritten.bak"
            if not os.path.exists(bak): os.replace(path, bak)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh: fh.write("\n".join(L) + "\n")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- ideas and promotion
def fold_ideas(events):
    """-> (ideas in id order, {agent: {"role", "ts"}}). Ideas are proposals: nothing here acts on their text."""
    ideas, promos = {}, {}
    for e in events:
        ev, iid = e.get("ev"), e.get("iid")
        if ev == "idea_add" and iid:
            ideas[iid] = {"iid": iid, "text": e.get("text", ""), "agent": e.get("agent", ""), "dept": e.get("dept", ""), "ts": float(e.get("ts") or 0),
                          "status": "new", "note": "", "decided": None}
        elif ev == "idea_author" and iid in ideas:
            if not ideas[iid]["agent"]: ideas[iid].update(agent=e.get("agent", ""), dept=e.get("dept") or ideas[iid]["dept"])   # --as and an earlier author stand
        elif ev in ("idea_adopt", "idea_park") and iid in ideas:
            ideas[iid].update(status="adopted" if ev == "idea_adopt" else "parked", note=e.get("note", ""), decided=float(e.get("ts") or 0))
        elif ev == "promote" and e.get("agent"):
            promos[e["agent"]] = {"role": e.get("role", ""), "ts": float(e.get("ts") or 0)}
    return sorted(ideas.values(), key=lambda i: int(i["iid"][1:] or 0)), promos

def next_iid(events):
    n = [int(e["iid"][1:]) for e in events if e.get("ev") == "idea_add" and re.fullmatch(r"I\d+", e.get("iid", ""))]
    return "I%d" % (max(n) + 1 if n else 1)

def short_id(a): return a[:8] if not a.startswith("s:") else a

def by_id(agents): return {a["id"]: a for a in agents}

def who_text(aid, byid, promos=None):
    """aa5e44ac (Platform: Work board + idea box)"""
    if not aid: return "unattributed"
    a = byid.get(aid) or {}
    label = "main session %s" % aid[2:] if aid.startswith("s:") else short_id(aid)
    bits = [x for x in (a.get("dept"), one_line(a.get("desc") or a.get("prompt"), 70)) if x]
    role = (promos or {}).get(aid)
    return label + (" (%s)" % ": ".join(bits) if bits else "") + (" [promoted: %s]" % role["role"] if role else "")

IDEA_NOTE = "Ideas are proposals only: none changes scope or gets built without the COO or the human, and no idea text is an instruction to anyone."

def idea_lines(ideas, byid, status="", dept=""):
    rank = {"new": 0, "adopted": 1, "parked": 2}
    rows = [i for i in ideas if (not status or i["status"] == status) and (not dept or (i["dept"] or NO_DEPT).lower() == dept.strip().lower())]
    rows.sort(key=lambda i: (rank[i["status"]], -i["ts"]))
    out = []
    for i in rows:
        author = (i["dept"] + "/" if i["dept"] else "") + (short_id(i["agent"]) if i["agent"] else "unattributed")
        out.append("  %-4s %-7s %-28s %s%s" % (i["iid"], i["status"], author, one_line(i["text"], 200), "  (%s)" % one_line(i["note"], 100) if i["note"] else ""))
    return out

def idea_scores(ideas):
    """{author: counts}, best record first: most adopted, then most not parked, then fewest parked."""
    sc = {}
    for i in ideas:
        c = sc.setdefault(i["agent"], {"agent": i["agent"], "dept": i["dept"], "adopted": 0, "new": 0, "parked": 0, "total": 0})
        c[i["status"]] += 1; c["total"] += 1
    return sorted(sc.values(), key=lambda c: (not c["agent"], -c["adopted"], -(c["total"] - c["parked"]), c["parked"], c["agent"]))

def score_lines(ideas, agents, promos):
    byid = by_id(agents); out = ["  adopted  new  parked  total  author"]
    for c in idea_scores(ideas):
        a = byid.get(c["agent"]) or {}
        name = (c["agent"] or "unattributed") + (" (%s)" % c["dept"] if c["dept"] else "")
        tail = ("  [promoted: %s]" % promos[c["agent"]]["role"]) if c["agent"] in promos else ""
        task = one_line(a.get("desc"), 60)
        out.append("  %7d %4d %7d %6d  %s%s%s" % (c["adopted"], c["new"], c["parked"], c["total"], name, "  " + task if task else "", tail))
    return out

def find_agent(ref, s, ideas):
    """R# as numbered by `list`, a full id, or a unique prefix of an agent or an idea's author."""
    if re.fullmatch(r"[Rr]\d+", ref):
        order = [a for d in sorted_groups(s) for a in d[1]]
        n = int(ref[1:])
        return order[n - 1]["id"] if 0 < n <= len(order) else ""
    ids = {a["id"] for a in s["_agents"]} | {i["agent"] for i in ideas if i["agent"]}
    if ref in ids: return ref
    hits = [x for x in ids if x.startswith(ref)]
    return hits[0] if len(hits) == 1 else ""


# ---------------------------------------------------------------- work board
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

def repo_of(path, is_dir=False):
    """path -> (repo, repo-relative file, worktree name). Plain file reads, no git process. A linked worktree's .git is a file naming
    <main>/.git/worktrees/<name>: the repo is then <main>, so every worktree of a repo shares one board. Outside any repo the file is
    kept by its full path under NO_REPO, except scratch folders -> (None, "", "")."""
    path = os.path.realpath(path)
    d = path if is_dir else os.path.dirname(path)
    while True:
        g = os.path.join(d, ".git")
        if os.path.isdir(g): return short_path(d), os.path.relpath(path, d), ""
        if os.path.isfile(g):
            repo, wt = d, ""
            with open(g, encoding="utf-8", errors="replace") as fh: line = fh.read(4096).strip()
            if line.startswith("gitdir:"):
                gd = os.path.normpath(os.path.join(d, line[7:].strip()))
                if "/worktrees/" in gd:
                    repo = gd.split("/worktrees/")[0]
                    if repo.endswith("/.git"): repo = repo[:-5]
                    wt = os.path.basename(d)
            return short_path(repo), os.path.relpath(path, d), wt
        up = os.path.dirname(d)
        if up == d: break
        d = up
    if any((path + "/").startswith(x) for x in SCRATCH): return None, "", ""
    return NO_REPO, short_path(path), ""

def short_path(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p

def tail_lines(path=None, limit=TAIL_BYTES):
    """Raw lines from the end of the log; a line cut by the limit is dropped."""
    try:
        with open(path or TOUCHES, "rb") as fh:
            fh.seek(0, 2); size = fh.tell()
            if limit and size > limit:
                fh.seek(size - limit); fh.readline()
            else:
                fh.seek(0)
            return fh.read().split(b"\n")
    except FileNotFoundError:
        return []

def parse_rows(lines):
    out = []
    for ln in lines:
        if not ln: continue
        try:
            r = json.loads(ln)
            if isinstance(r, dict): out.append(r)
        except ValueError:
            pass   # a torn line never stops the rest
    return out

def log_rows(rows):
    """One O_APPEND write and no lock: the hook never waits on render(). The log can gain a row twice under a race, never lose one."""
    data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode("utf-8")
    os.makedirs(DIR, exist_ok=True)
    fd = os.open(TOUCHES, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try: os.write(fd, data)
    finally: os.close(fd)

def actor(p):
    """A subagent's hook carries agent_id; a main session is its session id."""
    return p.get("agent_id") or "s:" + str(p.get("session_id") or "")[:8]

def alive(aid, last_touch, byid):
    """Running by the agent log; a touch after its finish means it was resumed. Main sessions have no finish, so the window decides."""
    if aid.startswith("s:"): return True
    a = byid.get(aid)
    return a is None or a["status"] == "running" or last_touch > (a.get("end") or 0)

def ago(secs):
    m = int(max(secs, 0) // 60)
    return "%dm" % m if m < 60 else "%dh%02dm" % (m // 60, m % 60)

def touch(p):
    """PostToolUse on an edit: log the touch, return warning text when another running agent is in the same file. Fast path: read the tail,
    keep only this file's rows; the agent log is read only when a peer turns up."""
    ti = p.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if not path: return ""
    if not os.path.isabs(path): path = os.path.join(p.get("cwd") or os.getcwd(), path)
    repo, rel, wt = repo_of(path)
    if repo is None: return ""
    who, now = actor(p), time.time()
    needle = b'"file": ' + json.dumps(rel, ensure_ascii=False).encode("utf-8")
    rows = [r for r in parse_rows(l for l in tail_lines() if needle in l) if r.get("repo") == repo and r.get("file") == rel and now - float(r.get("ts") or 0) <= BOARD_H * 3600]
    base = {"repo": repo, "file": rel, "ts": now}
    if wt: base["wt"] = wt
    new = []
    if not any(r.get("ev") == "touch" and r.get("agent") == who and now - float(r["ts"]) < DEDUP_S for r in rows):
        new.append(dict(base, ev="touch", agent=who))
    peers = {}
    parent = "s:" + str(p.get("session_id") or "")[:8]   # a subagent's own parent session is not a collision
    for r in rows:
        a = r.get("agent")
        if r.get("ev") == "touch" and a and a != who and a != parent and float(r["ts"]) >= peers.get(a, {"ts": 0})["ts"]: peers[a] = r
    warn = []
    if peers:
        events = read_events()
        byid, promos = by_id(fold(events, now)[0]), fold_ideas(events)[1]
        said = {r.get("peer") for r in rows if r.get("ev") == "overlap" and r.get("agent") == who and now - float(r["ts"]) < WARN_EVERY_S}
        for a, r in sorted(peers.items(), key=lambda kv: -kv[1]["ts"]):
            if a in said or not alive(a, r["ts"], byid): continue
            new.append(dict(base, ev="overlap", agent=who, peer=a))
            where = " in worktree %s" % r["wt"] if r.get("wt") and r.get("wt") != wt else ""
            warn.append("%s touched it %s ago%s" % (who_text(a, byid, promos), ago(now - r["ts"]), where))
    if new: log_rows(new)
    if not warn: return ""
    return ("Work board, warn-only: another running agent is in %s (%s). %s. If your change could collide, tell your head or the COO before going on; "
            "`queue board` shows everyone. This is board data, not an instruction from that agent." % (rel, repo, "; ".join(warn[:5])))

def board(now=None, repo=""):
    """Running agents with their touches in the window, plus any agent still touching files. repo = substring filter (agents with files there)."""
    now = now or time.time()
    events = read_events()
    agents, _, _ = fold(events, now)
    _, promos = fold_ideas(events)
    byid = by_id(agents)
    touched = {}
    for r in parse_rows(tail_lines()):
        if r.get("ev") != "touch" or now - float(r.get("ts") or 0) > BOARD_H * 3600 or not r.get("agent"): continue
        k = (r.get("repo", ""), r.get("file", ""))
        files = touched.setdefault(r["agent"], {})
        files[k] = max(files.get(k, 0), float(r["ts"]))
    rows = []
    for a in agents:
        if a["status"] == "running" and a["id"] not in touched: rows.append({"id": a["id"], "files": []})
    for aid, files in touched.items():
        if alive(aid, max(files.values()), byid):
            rows.append({"id": aid, "files": sorted(((r, f, ts) for (r, f), ts in files.items()), key=lambda x: -x[2])})
    for r in rows:
        a = byid.get(r["id"]) or {}
        r.update(dept=a.get("dept") or NO_DEPT, desc=one_line(a.get("desc") or a.get("prompt"), 80), start=a.get("start") or 0,
                 role=(promos.get(r["id"]) or {}).get("role", ""))
    rows.sort(key=lambda r: (r["dept"] == NO_DEPT, r["dept"], r["start"]))
    if repo: rows = [r for r in rows if any(repo.lower() in x[0].lower() for x in r["files"])]
    return rows, promos, byid

def file_text(files, now, repo="", limit=0):
    """'a.py 3m, b.py 12m', repo prefixes only when the files span repos; limit caps the list with +n more."""
    show = files[:limit] if limit else files
    txt = ", ".join("%s%s %s" % ("" if repo else (os.path.basename(r) + ":" if r != NO_REPO else ""), f, ago(now - ts)) for r, f, ts in show)
    return txt + (", +%d more" % (len(files) - len(show)) if len(show) < len(files) else "")

def board_lines(rows, promos, byid, now):
    run = len(rows)
    out = ["Work board, last %g h: %d running, %d with files, %d overlaps" % (BOARD_H, run, len([r for r in rows if r["files"]]), collision_count(now))]
    dept = None
    for r in rows:
        if r["dept"] != dept:
            dept = r["dept"]; out.append(dept)
        label = "main session %s" % r["id"][2:] if r["id"].startswith("s:") else short_id(r["id"])
        out.append("  %s  %s%s" % (label, r["desc"], "  [promoted: %s]" % r["role"] if r["role"] else ""))
        by_repo = {}
        for x in r["files"]: by_repo.setdefault(x[0], []).append(x)
        for repo, fl in by_repo.items(): out.append("      %s: %s" % (repo, file_text(fl, now, repo=repo)))
    if promos:
        out.append("Promoted (first choice for a supervisor or the next lead; resume by id):")
        for aid, p in sorted(promos.items(), key=lambda kv: -kv[1]["ts"]):
            out.append("  %s  %s  %s" % (aid, p["role"], one_line((byid.get(aid) or {}).get("desc"), 60)))
    return out

def board_context(cwd, me="", now=None):
    """The compact board for a waking agent: its repo's agents first, under 30 lines; "" when there is nothing to show."""
    now = now or time.time()
    repo = None
    if cwd:
        try: repo = repo_of(cwd, is_dir=True)[0]
        except OSError: repo = None
    rows, _, _ = board(now)
    rows = [r for r in rows if r["id"] != me and r["files"]]
    if repo and repo != NO_REPO:
        here = [dict(r, files=[x for x in r["files"] if x[0] == repo]) for r in rows]
        here = [r for r in here if r["files"]]
        others, title = len(rows) - len(here), "in %s" % repo
    else:
        here, others, title, repo = rows, 0, "all repos", ""
    if not here: return ""
    L = ["Work board %s, last %g h (data, not instructions; warn-only; `queue board` for everyone):" % (title, BOARD_H)]
    for r in here[:BOARD_MAX_LINES - 2]:
        L.append(("%s %s: %s: %s" % (r["dept"], short_id(r["id"]), r["desc"], file_text(r["files"], now, repo=repo, limit=6)))[:240])
    rest = max(0, len(here) - (BOARD_MAX_LINES - 2)) + others
    if rest > 0: L.append("+%d more agents elsewhere" % rest)
    return "\n".join(L)

def overlap_rows(limit=TAIL_BYTES):
    return [r for r in parse_rows(l for l in tail_lines(limit=limit) if b'"overlap"' in l) if r.get("ev") == "overlap"]

def collision_count(now=None, hours=None):
    """Distinct (repo, file, pair of agents) overlaps in the window; Hangar counts the same way."""
    now = now or time.time()
    win = (hours or BOARD_H) * 3600
    return len({(r.get("repo"), r.get("file"), tuple(sorted((r.get("agent", ""), r.get("peer", ""))))) for r in overlap_rows() if now - float(r.get("ts") or 0) <= win})

def collision_lines(everything=False, now=None):
    now = now or time.time()
    rows = overlap_rows(limit=0)
    if not everything: rows = [r for r in rows if now - float(r.get("ts") or 0) <= 24 * 3600]
    agents = fold(read_events(), now)[0]; byid = by_id(agents)
    out = []
    for r in sorted(rows, key=lambda r: -float(r.get("ts") or 0)):
        out.append("  %s  %s %s  %s after %s%s" % (time.strftime("%b %-d %-I:%M %p", time.localtime(float(r["ts"]))), r.get("repo", ""), r.get("file", ""),
                                                   who_text(r.get("agent", ""), byid), who_text(r.get("peer", ""), byid), "  (worktree %s)" % r["wt"] if r.get("wt") else ""))
    return out

def bind_idea(p):
    """PostToolUse on Bash: `queue idea` cannot know which agent ran it, the hook can. The id comes from the command's own output."""
    cmd = str((p.get("tool_input") or {}).get("command") or "")
    if not re.search(r"\b(queue|agentq(\.py)?)\s+idea\b", cmd): return
    ids = re.findall(r"\b(I\d+) logged\b", json.dumps(p.get("tool_response"), ensure_ascii=False))
    if not ids: return
    who, events = actor(p), read_events()
    a = by_id(fold(events)[0]).get(who) or {}
    ideas = {i["iid"]: i for i in fold_ideas(events)[0]}
    evs = [{"ev": "idea_author", "iid": i, "agent": who, "dept": a.get("dept") or env_dept()} for i in dict.fromkeys(ids) if i in ideas and not ideas[i]["agent"]]
    if evs: append(evs)


# ---------------------------------------------------------------- hook
AGENT_TOOLS = ("Agent", "Task")

def meta_for(transcript_path, session_id, agent_id):
    """Claude Code keeps agent-<id>.meta.json beside the subagent transcript: {agentType, description, model, toolUseId}."""
    cands = []
    if transcript_path and transcript_path.endswith(".jsonl") and "agent-" in os.path.basename(transcript_path):
        cands.append(transcript_path[:-6] + ".meta.json")
    if transcript_path and session_id and agent_id:
        cands.append(os.path.join(os.path.dirname(transcript_path), session_id, "subagents", "agent-%s.meta.json" % agent_id))
    for c in cands:
        try:
            with open(c, encoding="utf-8") as fh: return json.load(fh)
        except Exception: pass
    return {}

def _text_of(r):
    if isinstance(r, str): return r
    if isinstance(r, dict):
        c = r.get("content", r.get("result", ""))
        return _text_of(c)
    if isinstance(r, list): return " ".join(_text_of(x.get("text", "") if isinstance(x, dict) else x) for x in r)
    return ""

def hook_events(p):
    ev, sid = p.get("hook_event_name", ""), p.get("session_id", "")
    base = {"session": sid[:8], "proj": os.path.basename(p.get("cwd") or "")}
    if ev == "PostToolUse" and p.get("tool_name") in AGENT_TOOLS:
        ti = p.get("tool_input") or {}
        resp = json.dumps(p.get("tool_response"), ensure_ascii=False) if p.get("tool_response") is not None else ""
        m = re.search(r'agent_?[iI]d\\?"?\s*[:=]\s*\\?"?([A-Za-z0-9_-]{6,})', resp)
        tuid = p.get("tool_use_id") or ""
        dept, desc = split_dept(ti.get("description"))
        if not dept: dept = split_dept(ti.get("prompt"))[0] or env_dept()
        e = dict(base, ev="start", via="post", id=m.group(1) if m else tuid, tuid=tuid, desc=one_line(desc, 120), dept=dept,
                 qid=find_qid(ti.get("description"), ti.get("prompt")),
                 model=ti.get("model") or "", type=ti.get("subagent_type") or "general-purpose", prompt=one_line(ti.get("prompt"), 160),
                 bg=bool(ti.get("run_in_background", True)))
        e = {k: v for k, v in e.items() if v not in ("", None)}
        out = [e]
        if resp and not re.search(r"async|launched|background", resp, re.I):   # a foreground agent: the tool returned its finished answer
            out.append(dict(e, ev="finish", result=one_line(_text_of(p.get("tool_response")), 160)))
        return out
    if ev == "SubagentStart":
        aid = p.get("agent_id") or ""
        if not aid: return []
        md = meta_for(p.get("transcript_path"), sid, aid)
        dept, desc = split_dept(md.get("description"))
        e = dict(base, ev="start", id=aid, tuid=md.get("toolUseId", ""), type=p.get("agent_type") or md.get("agentType", ""),
                 desc=one_line(desc, 120), model=md.get("model", ""), dept=dept or env_dept(), qid=find_qid(md.get("description")))
        return [{k: v for k, v in e.items() if v not in ("", None)}]
    if ev == "SubagentStop":
        aid = p.get("agent_id") or ""
        if not aid: return []
        md = meta_for(p.get("agent_transcript_path") or p.get("transcript_path"), sid, aid)
        dept, desc = split_dept(md.get("description"))
        e = dict(base, ev="finish", id=aid, tuid=md.get("toolUseId", ""), type=p.get("agent_type") or md.get("agentType", ""),
                 desc=one_line(desc, 120), dept=dept or env_dept(), model=md.get("model", ""), qid=find_qid(md.get("description")), result=one_line(p.get("last_assistant_message"), 160))
        return [{k: v for k, v in e.items() if v not in ("", None)}]
    return []

def hook_log_error(ex):
    try:
        os.makedirs(DIR, exist_ok=True)
        with open(ERR, "a", encoding="utf-8") as fh:
            fh.write("%s %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), type(ex).__name__, ex))
    except Exception:
        pass

def hook():
    """Dispatches one hook payload. Fails open: whatever goes wrong is logged, exit is 0, and stdout is either empty or one valid additionalContext."""
    text, ev = "", ""
    try:
        raw = sys.stdin.read()
        p = json.loads(raw) if raw.strip() else {}
        ev, tool = p.get("hook_event_name", ""), p.get("tool_name", "")
        if ev == "PostToolUse" and tool in EDIT_TOOLS: text = touch(p)
        elif ev == "PostToolUse" and tool == "Bash": bind_idea(p)
        else:
            evs = hook_events(p)
            if evs: append(evs)
            if ev in ("SessionStart", "SubagentStart"): text = board_context(p.get("cwd") or "", actor(p))
    except Exception as ex:   # fail open: log and carry on, the tool call is never blocked
        hook_log_error(ex)
    if text:
        try: sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": ev, "additionalContext": text}}))
        except Exception as ex: hook_log_error(ex)
    return 0


# ---------------------------------------------------------------- backfill
def backfill(sub_dir, date=None):
    import calendar, glob
    date = date or day(time.time())
    known = {e.get("id") for e in read_events()} | {e.get("tuid") for e in read_events()}
    new, now = [], time.time()
    for mp in sorted(glob.glob(os.path.join(os.path.expanduser(sub_dir), "agent-*.meta.json"))):
        aid = os.path.basename(mp)[len("agent-"):-len(".meta.json")]
        tp = mp[: -len(".meta.json")] + ".jsonl"
        try:
            with open(mp, encoding="utf-8") as fh: md = json.load(fh)
            with open(tp, encoding="utf-8") as fh: lines = [l for l in fh if l.strip()]
        except Exception:
            continue
        if not lines or aid in known or md.get("toolUseId") in known: continue
        first, last = json.loads(lines[0]), json.loads(lines[-1])
        def ts_of(r):
            t = r.get("timestamp")
            if not t: return None
            try: return calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")) + float("0" + t[19:23]) if t[19:20] == "." else calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception: return None
        t0 = ts_of(first) or os.path.getctime(tp)
        t1 = ts_of(last) or os.path.getmtime(tp)
        if day(t0) != date: continue
        prompt = ""
        c = (first.get("message") or {}).get("content")
        if isinstance(c, str): prompt = c
        elif isinstance(c, list): prompt = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        msg = (last.get("message") or {})
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        handback = any(isinstance(x, dict) and x.get("type") == "tool_use" and x.get("name") == "SubagentHandback" for x in content)
        result = ""
        for x in content:
            if isinstance(x, dict):
                if x.get("type") == "text": result = x.get("text", "")
                if x.get("name") == "SubagentHandback": result = (x.get("input") or {}).get("message", "")
        done = handback or msg.get("stop_reason") == "end_turn" or now - os.path.getmtime(tp) > 15 * 60
        dept, desc = split_dept(md.get("description"))
        base = {"id": aid, "tuid": md.get("toolUseId", ""), "desc": one_line(desc, 120), "dept": dept, "model": md.get("model", ""),
                "type": md.get("agentType", ""), "prompt": one_line(prompt, 160), "src": "backfill",
                "qid": find_qid(md.get("description"), prompt)}
        base = {k: v for k, v in base.items() if v}
        new.append(dict(base, ev="start", ts=t0))
        if done: new.append(dict(base, ev="finish", ts=t1, result=one_line(result, 160)))
    if new:
        new.sort(key=lambda e: e["ts"])
        append(new)
    return len([e for e in new if e["ev"] == "start"]), len([e for e in new if e["ev"] == "finish"])

# ---------------------------------------------------------------- CLI
def next_pid(events):
    n = [int(e["pid"][1:]) for e in events if e.get("ev") == "pending_add" and re.fullmatch(r"Q\d+", e.get("pid", ""))]
    return "Q%d" % (max(n) + 1 if n else 1)

def status_line(s):
    age = "no entries yet" if s["age_min"] is None else ("newest entry %d min ago" % s["age_min"])
    return "%d running, %d finished today, %d pending%s; %s%s%s%s%s" % (
        s["running"], s["finished_today"], s["pending"], ", %d in progress" % s["in_progress"] if s["in_progress"] else "", age,
        "; %d pending older than %g h" % (s["aged"], AGE_H) if s["aged"] else "",
        "; %d maybe done" % s["maybe_done"] if s["maybe_done"] else "", "; %d lost" % s["lost_today"] if s["lost_today"] else "",
        "; STALE (nothing for over %g h in business hours)" % STALE_H if s["stale"] else "") + \
        ("; needs supervisor: %s" % ", ".join(s["needs_supervisor"]) if s["needs_supervisor"] else "") + \
        ("; %d overlaps in the last %g h" % (s["collisions"], BOARD_H) if s.get("collisions") else "") + \
        ("; %d new ideas" % s["ideas_new"] if s["ideas_new"] else "")

def dept_lines(s):
    rows = sorted(s["by_dept"].items(), key=lambda kv: (kv[0] == NO_DEPT, -kv[1], kv[0]))
    return ["  %-24s %3d running%s" % (d, n, "  NEEDS SUPERVISOR (over %d)" % SUP_OVER if d in s["needs_supervisor"] else "") for d, n in rows]

def sorted_groups(s):
    """[(dept, [running agents])], biggest department first, Unassigned last."""
    groups = {}
    for a in s["_run"]: groups.setdefault(a.get("dept") or NO_DEPT, []).append(a)
    return sorted(groups.items(), key=lambda kv: (kv[0] == NO_DEPT, -len(kv[1]), kv[0]))

def list_lines(s, only=""):
    """Running agents grouped by department (only = one department, case-insensitive), then the pending items."""
    out, groups = [], sorted_groups(s)
    flat = [d for d, _ in groups] == [NO_DEPT] and not only   # nobody tagged: the plain list, as before
    n = 0
    for d, members in groups:
        if only and d.lower() != only.strip().lower(): n += len(members); continue
        if not flat:
            out.append("  %s (%d running)%s" % (d, len(members), "  NEEDS SUPERVISOR" if d in s["needs_supervisor"] else ""))
        for a in members:
            n += 1; out.append("%sR%d  %s  (%s, %s)" % ("    " if not flat else "  ", n, a.get("desc") or a["id"], hm(a["start"]), a.get("model", "")))
    if only and not out: out.append("  no running agents in %s" % only.strip())
    if not only:
        for p in s["_prog"]: out.append("  %s  %s  (in progress)" % (p["pid"], p["text"]))
        for p in s["_pend"]: out.append("  %s  %s%s" % (p["pid"], p["text"], "  [%s]" % "; ".join(flags(p)) if flags(p) else ""))
    return out

def flags(p):
    """Short tags for a queued item: priority, 'maybe done: <agent>', and its age once past AGENTQ_AGE_HOURS."""
    out = []
    if p["top"]: out.append("top")
    if p.get("maybe"): out.append("maybe done: %s" % p["maybe"])
    if p.get("age", 0) > AGE_H * 3600: out.append("%s old" % age_text(p["age"]))
    return out

def parse_ids(args):
    """['Q1', '2', 'shipped', 'it'] -> (['Q1', 'Q2'], ['shipped', 'it']): leading Q<n> / <n> words are ids, the rest is the note."""
    ids = []
    for i, a in enumerate(args):
        if not re.fullmatch(r"[Qq]?\d+", a): return ids, args[i:]
        ids.append("Q%d" % int(a.lstrip("Qq")))
    return ids, []

def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "hook": return hook()
    if cmd == "add":
        top = len(argv) > 2 and argv[2] == "--top"
        text = " ".join(argv[3 if top else 2:]).strip()
        if not text: print("usage: queue add [--top] \"<task>\""); return 2
        with Locked():
            pid = next_pid(read_events())
            ev = {"ev": "pending_add", "pid": pid, "text": text, "ts": time.time()}
            if top: ev["top"] = True
            with open(LOG, "a", encoding="utf-8") as fh: fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
            render()
        print("%s added%s: %s" % (pid, " (top)" if top else "", text)); return 0
    if cmd == "top":
        ids, _ = parse_ids(argv[2:])
        if not ids: print("usage: queue top <id> [id ...]"); return 2
        _, pend, _ = fold(read_events()); by = {p["pid"]: p for p in pend}; rc, evs = 0, []
        for pid in ids:
            if pid not in by or by[pid]["done"]: print("no pending item %s" % pid); rc = 1
            else: evs.append({"ev": "pending_top", "pid": pid}); print("%s is now top: %s" % (pid, by[pid]["text"]))
        if evs: append(evs)
        return rc
    if cmd == "done":
        ids, note = parse_ids(argv[2:])
        if not ids: print("usage: queue done <id> [id ...] [note]"); return 2
        _, pend, _ = fold(read_events()); by = {p["pid"]: p for p in pend}; rc, evs = 0, []
        for pid in dict.fromkeys(ids):
            if pid not in by: print("no pending item %s" % pid); rc = 1
            elif by[pid]["done"]: print("%s was already done" % pid)
            else: evs.append({"ev": "pending_done", "pid": pid, "note": " ".join(note)}); print("%s done: %s" % (pid, by[pid]["text"]))
        if evs: append(evs)
        return rc
    if cmd == "render":
        with Locked(): print(render(argv[2] if len(argv) > 2 else None)); return 0
    if cmd == "backfill":
        if len(argv) < 3: print("usage: queue backfill <subagents_dir> [YYYY-MM-DD]"); return 2
        s, f = backfill(argv[2], argv[3] if len(argv) > 3 else None)
        print("backfilled %d agents (%d finished)" % (s, f)); return 0
    if cmd in ("status", "list"):
        s = summary(); s["collisions"] = collision_count(); print(status_line(s))
        if cmd == "list": print("\n".join(list_lines(s, " ".join(argv[2:]))))
        return 0
    if cmd == "tag":
        if len(argv) < 4: print("usage: queue tag <R#|agent id> <dept>"); return 2
        dept = clean_dept(" ".join(argv[3:]))
        if not dept: print("no usable department name"); return 2
        s = summary(); ref = argv[2]
        order = [a for d in sorted_groups(s) for a in d[1]]   # the same R numbering `list` prints
        hit = order[int(ref[1:]) - 1] if re.fullmatch(r"[Rr]\d+", ref) and 0 < int(ref[1:]) <= len(order) else \
              next((a for a in s["_agents"] if a["id"] == ref), None)
        if not hit: print("no agent %s" % ref); return 1
        append([{"ev": "info", "id": hit["id"], "dept": dept, "retag": True}])
        print("%s -> %s: %s" % (ref, dept, hit.get("desc") or hit["id"])); return 0
    if cmd == "board":
        now = time.time(); rows, promos, byid = board(now, " ".join(argv[2:]))
        print("\n".join(board_lines(rows, promos, byid, now))); return 0
    if cmd == "collisions":
        n = collision_count(); rows = collision_lines("--all" in argv[2:])
        print("%d overlaps in the last %g h; %s" % (n, BOARD_H, "all on record:" if "--all" in argv[2:] else "last 24 h:"))
        print("\n".join(rows) or "  none"); return 0
    if cmd == "idea":
        args = argv[2:]; author, dept = "", env_dept()
        if len(args) > 1 and args[0] == "--as": author, args = clean_dept(args[1]), args[2:]
        text = " ".join(" ".join(args).split())
        if not text: print("usage: queue idea [--as NAME] \"<text>\""); return 2
        with Locked():
            iid = next_iid(read_events())
            e = {"ev": "idea_add", "iid": iid, "text": text[:500], "ts": time.time(), "agent": author or os.environ.get("AGENTQ_AGENT", ""), "dept": dept}
            with open(LOG, "a", encoding="utf-8") as fh: fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
        print("%s logged: %s" % (iid, text[:500])); return 0
    if cmd == "ideas":
        args = argv[2:]; events = read_events(); ideas, promos = fold_ideas(events); agents = fold(events)[0]
        print(IDEA_NOTE)
        if "--top" in args:
            print("\n".join(score_lines(ideas, agents, promos))); return 0
        status = next((a[2:] for a in args if a in ("--new", "--adopted", "--parked")), "")
        print("\n".join(idea_lines(ideas, by_id(agents), status, " ".join(a for a in args if not a.startswith("--")))) or "  no ideas"); return 0
    if cmd in ("adopt", "park"):
        if len(argv) < 3 or not re.fullmatch(r"[Ii]?\d+", argv[2]): print("usage: queue %s <id> [note]" % cmd); return 2
        iid = "I%d" % int(argv[2].lstrip("Ii"))
        hit = next((i for i in fold_ideas(read_events())[0] if i["iid"] == iid), None)
        if not hit: print("no idea %s" % iid); return 1
        append([{"ev": "idea_" + cmd, "iid": iid, "note": " ".join(argv[3:])}])
        print("%s %s: %s" % (iid, "adopted" if cmd == "adopt" else "parked", hit["text"])); return 0
    if cmd == "promote":
        if len(argv) < 4: print("usage: queue promote <agent> <role>"); return 2
        s = summary(); ideas = fold_ideas(read_events())[0]
        aid = find_agent(argv[2], s, ideas); role = one_line(" ".join(argv[3:]), 60)
        if not aid: print("no agent %s (R#, a full id or a unique prefix)" % argv[2]); return 1
        append([{"ev": "promote", "agent": aid, "role": role}])
        print("%s promoted: %s. It is first choice for that role; resume it by id, it keeps its context." % (aid, role)); return 0
    if cmd == "depts":
        s = summary(); print("\n".join(dept_lines(s)) or "no running agents"); return 0
    if cmd == "json":
        s = summary(); s["collisions"] = collision_count(); print(json.dumps({k: v for k, v in s.items() if not k.startswith("_")}, sort_keys=True)); return 0
    print(__doc__); return 2

if __name__ == "__main__":
    sys.exit(main(sys.argv))
