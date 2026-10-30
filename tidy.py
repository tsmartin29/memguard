#!/usr/bin/python3 -I
"""tidy: one step to clean, save and back up. Everything it may touch comes from the "tidy" block of config.json.

  tidy                       clean -> save -> backup -> report
  tidy --dry-run             show the plan, change nothing
  tidy --only clean,save     run some steps (clean | save | backup)
  tidy --drive /Volumes/X    back up to this drive (overrides the marker-folder / Time Machine refusal)
  tidy --approve-drive /Volumes/X   allow backups to a drive that has a refuse marker folder (remembered by volume UUID)
  tidy --pio-cache           also move PlatformIO download archives older than pio_cache_days to the Trash
  tidy --status              one-line JSON state (the Hangar menu-bar app reads it)

1 CLEAN  regenerable build output only (.pio, CMake/JUCE/Zephyr build dirs that git ignores, __pycache__, .pytest_cache, unused old
         PlatformIO toolchain versions, ccache over its cap), found under `roots` -> ONE dated Trash folder per run
         (~/.Trash/tidy_<date_time>, with TIDY_MANIFEST.txt = original path + size of every item). Never rm. Never: a `protect` path,
         a tree edited in the last `recent_minutes`, a tree a process has open or a heavyrun job uses, git-tracked files, `never_dirs`,
         build folders named `keep_build_prefixes` or named by a tracked script, virtualenvs, anything outside the roots.
2 SAVE   `save_command` (optional, your own snapshot script), then every local branch with unpushed commits in the `push_repos`
         (owner/name slugs, or "*" for every repo under the roots) goes to origin under the same name: fast-forward only, never
         forced, a secrets check on the new commits, private repos only unless `push_public_repos` is true.
3 BACKUP verified `git bundle --all` per repo + rsync (no --delete, hard links to the previous dated folder) of `backup_sources`
         into <drive>/<backup_folder_prefix><YYYY-MM-DD>/. Runs as ONE light-lane heavyrun job (nice). Never the startup disk, a Time
         Machine volume or a volume with a `refuse_drive_markers` folder unless named with --drive or approved with --approve-drive.
4 REPORT one summary line -> `log_file` (default tidy.log in the memguard home) + a notification.
"""
import argparse, json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
E = os.environ
HOME = E.get("TIDY_HOME", os.path.expanduser("~"))
MG = E.get("TIDY_MEMGUARD_DIR") or os.path.expanduser(E.get("MEMGUARD_HOME", "~/.config/memguard"))   # memguard home: slots, queues, state
CONFIG_FILE = os.path.expanduser(E.get("MEMGUARD_CONFIG") or os.path.join(MG, "config.json"))
GB = 1 << 30

TIDY_DEFAULTS = {
    "roots": ["~/Developer", "~/code", "~/Work"],   # build output is only ever looked for under these (missing ones are skipped)
    "protect": [],                                  # trees never cleaned (e.g. a hardware bench checkout)
    "recent_minutes": 30,                           # a tree edited this recently = an agent may still be working there
    "never_dirs": [".git", "node_modules", "dist", "release", "releases"],   # never entered, never touched
    "keep_build_prefixes": ["build-release"],       # build folders kept on purpose (installer builds)
    "push_repos": [],                               # owner/name slugs whose local branches tidy pushes; ["*"] = every repo under the roots
    "push_public_repos": False,                     # false = only private repos are ever pushed
    "save_command": "",                             # optional shell command run first in the SAVE step (your own snapshot script)
    "backup_sources": [],                           # folders rsynced to the backup drive
    "backup_extra_files": [],                       # files copied to the top of the dated backup folder too (a how-to, a README)
    "backup_folder_prefix": "memguard_backup_",     # <drive>/<prefix><YYYY-MM-DD>/
    "backup_drive": "",                             # preferred volume name; otherwise a volume that already holds a <prefix>* folder
    "refuse_drive_markers": ["DCIM"],               # a volume with one of these top-level folders is never used (camera cards ...)
    "min_drive_free_gb": 10,
    "rsync_excludes": ["node_modules/", ".netlify/", ".wrangler/", "__pycache__/", ".pytest_cache/", ".pio/", ".venv/"],
    "log_file": "",                                 # default: tidy.log in the memguard home
    "pio_cache_days": 14,
}

def load_config():
    cfg, prio = dict(TIDY_DEFAULTS), "priority"
    try:
        with open(CONFIG_FILE) as f: c = json.load(f)
        cfg.update(c.get("tidy") or {}); prio = c.get("priority_pattern", prio)
    except Exception: pass
    return cfg, prio

CFG, PRIORITY_PATTERN = load_config()

def xp(p):
    """Expand ~ against HOME (TIDY_HOME in tests) and make absolute."""
    p = os.path.expandvars(p)
    if p == "~" or p.startswith("~/"): p = HOME + p[1:]
    return os.path.abspath(p)

VOLUMES = E.get("TIDY_VOLUMES", "/Volumes")
TRASH = E.get("TIDY_TRASH", os.path.join(HOME, ".Trash"))
LOGF = E.get("TIDY_LOG") or (xp(CFG["log_file"]) if CFG["log_file"] else os.path.join(MG, "tidy.log"))
STATE = os.path.join(MG, "tidy_state.json")
HEAVYRUN = os.path.join(HERE, "heavyrun")
RECENT = int(CFG["recent_minutes"]) * 60
ROOTS = [xp(r) for r in CFG["roots"]]
PROTECT = [xp(p) for p in CFG["protect"]]
PIO = os.path.join(HOME, ".platformio")
PUSH_REPOS = set(CFG["push_repos"])
BACKUP_SOURCES = [xp(s) for s in CFG["backup_sources"]]
EXTRA_FILES = [xp(f) for f in CFG["backup_extra_files"]]
PREFIX = CFG["backup_folder_prefix"]
MARKERS = list(CFG["refuse_drive_markers"])
RSYNC_EXCLUDES = list(CFG["rsync_excludes"])
BUILD_RE = re.compile(r"^(build|cmake-build|out)([-_.].*)?$", re.I)
NEVER_DIRS = set(CFG["never_dirs"])
SECRET_NAMES = re.compile(r"(^|/)(\.env(\..*)?|signing\.env|\.dev\.vars|.*secret.*|.*\.(p12|pem|key|mobileprovision|keystore))$", re.I)
# Common token shapes (Stripe live keys, AWS, GitHub, Slack, Resend, private key blocks). Written with [] so this file itself
# does not trip a plain-text secrets scan.
SECRET_TEXT = re.compile(r'^\+.*([sr]k_l[i]ve_[0-9A-Za-z]{10}|AK[I]A[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE[ ]KEY|gh[p]_[0-9A-Za-z]{30}|'
                         r'github_[p]at_|xox[bap]-|r[e]_[A-Za-z0-9]{20,})', re.M)

def run(cmd, timeout=600, **kw):
    try: p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw); return p.returncode, p.stdout, p.stderr
    except Exception as e: return 1, "", repr(e)

def gb(b): return f"{b / GB:.2f} GB"
def short(p): return p.replace(HOME, "~", 1) if p.startswith(HOME) else p
def inside(p, top): return p == top or p.startswith(top.rstrip("/") + "/")

def size_of(p):
    total = 0
    for dp, dn, fn in os.walk(p):
        for n in dn + fn:
            try: total += os.lstat(os.path.join(dp, n)).st_blocks * 512
            except OSError: pass
    try: total += os.lstat(p).st_blocks * 512
    except OSError: pass
    return total

# ---------------------------------------------------------------- protection: what must never be touched
def open_paths():
    """Every working directory and open file of every process (one lsof call), limited to the roots."""
    if E.get("TIDY_NO_LSOF") == "1": return []
    _, out, _ = run(["lsof", "-n", "-w", "-Fn"], timeout=120)
    tops = [os.path.realpath(r) for r in ROOTS + [PIO]] + list(ROOTS) + [PIO]
    return sorted({l[1:] for l in out.splitlines() if l.startswith("n/") and any(inside(l[1:], t) for t in tops)})

def heavyrun_paths():
    """Paths a heavyrun job uses or is queued for: slot holders' process trees (cwd) + any path or tree name in a label."""
    labels, pids = [], []
    try: names = os.listdir(MG)
    except OSError: names = []
    for n in names:
        d = os.path.join(MG, n)
        if re.match(r"^(heavy|light)\.(lock|slot\d+)$", n) and os.path.isdir(d):
            for f, acc in (("label", labels), ("pid", pids)):
                try: acc.append(open(os.path.join(d, f)).read().strip())
                except OSError: pass
        if n in ("queue", "queue_hold", "queue_light", "queue_light_hold") and os.path.isdir(d):
            for t in os.listdir(d):
                try: labels.append(open(os.path.join(d, t)).read().strip())
                except OSError: pass
    paths = []
    units = [(r, n) for r in ROOTS if os.path.isdir(r) for n in os.listdir(r)]
    for lab in labels:
        for m in re.findall(r"(~?/[\w.@+-][\w./@+-]*)", lab): paths.append(HOME + m[1:] if m.startswith("~/") else m)
        for r, w in units:
            if len(w) > 3 and re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", lab): paths.append(os.path.join(r, w))
    if pids:
        _, ps, _ = run(["ps", "-axo", "pid=,ppid="])
        kids = {}
        for l in ps.splitlines():
            a = l.split()
            if len(a) == 2: kids.setdefault(a[1], []).append(a[0])
        tree, todo = set(), [p for p in pids if p.isdigit()]
        while todo:
            p = todo.pop(); tree.add(p); todo += [k for k in kids.get(p, []) if k not in tree]
        if tree:
            _, cw, _ = run(["lsof", "-a", "-p", ",".join(sorted(tree)), "-d", "cwd", "-Fn"])
            paths += [l[1:] for l in cw.splitlines() if l.startswith("n/")]
    return paths

def unit_of(p):
    """The tree an agent works in: the first folder under a root. None for a root itself or a path outside the roots."""
    for top in ROOTS:
        for t in (top, os.path.realpath(top)):
            if p.startswith(t + "/"): return os.path.join(top, p[len(t) + 1:].split("/")[0])
    return None

def git_admin(tree):
    g = os.path.join(tree, ".git")
    if os.path.isfile(g):
        try:
            m = re.match(r"gitdir:\s*(.+)", open(g).read().strip())
            return m.group(1) if m else None
        except OSError: return None
    return g if os.path.isdir(g) else None

def recent_edit(tree, cutoff):
    """First file or folder in `tree` changed after `cutoff` (skips .git contents; checks the worktree's index/HEAD), else None."""
    adm = git_admin(tree)
    if adm:
        for f in ("index", "HEAD"):
            try:
                if os.stat(os.path.join(adm, f)).st_mtime > cutoff: return os.path.join(adm, f)
            except OSError: pass
    stack = [tree]
    while stack:
        d = stack.pop()
        try:
            if os.lstat(d).st_mtime > cutoff: return d
            it = os.scandir(d)
        except OSError: continue
        with it:
            for e in it:
                try:
                    if e.name == ".git": continue
                    st = e.stat(follow_symlinks=False)
                    if st.st_mtime > cutoff: return e.path
                    if e.is_dir(follow_symlinks=False): stack.append(e.path)
                except OSError: pass
    return None

class Tracked:
    """Is anything under a path tracked by git (= source)? One `git ls-files` per repo top, cached."""
    def __init__(self): self.tops, self.dirs = {}, {}
    def top(self, p):
        d = p
        while True:
            if d in self.tops: return self.tops[d]
            if os.path.exists(os.path.join(d, ".git")): break
            nd = os.path.dirname(d)
            if nd == d: d = None; break
            d = nd
        self.tops[p] = d
        return d
    def tracked(self, p):
        t = self.top(os.path.dirname(p))
        if t is None: return False
        if t not in self.dirs:
            rc, out, _ = run(["git", "-C", t, "ls-files", "-z"], timeout=300)
            s = set()
            for f in out.split("\0"):
                parts = f.split("/")[:-1]
                for i in range(1, len(parts) + 1): s.add("/".join(parts[:i]))
            self.dirs[t] = s if rc == 0 else None
        s = self.dirs[t]
        if s is None: return True                    # can't tell = treat as source
        return os.path.relpath(p, t) in s
    def ignored(self, p):
        t = self.top(os.path.dirname(p))
        return t is not None and run(["git", "-C", t, "check-ignore", "-q", os.path.relpath(p, t) + "/"])[0] == 0

# ---------------------------------------------------------------- 1. CLEAN
def find_candidates():
    """Walk the roots once: build-output candidates + every platformio.ini (for the toolchain rule)."""
    cands, inis = [], []
    for root in ROOTS:
        if not os.path.isdir(root) or os.path.islink(root): continue
        for dp, dn, fn in os.walk(root):
            if "platformio.ini" in fn: inis.append(os.path.join(dp, "platformio.ini"))
            for d in list(dn):
                full = os.path.join(dp, d)
                if d in NEVER_DIRS or os.path.islink(full): dn.remove(d); continue
                if d in ("site-packages", ".venv", "venv") or os.path.exists(os.path.join(full, "pyvenv.cfg")): dn.remove(d); continue   # venvs stay whole
                kind = None
                if d == ".pio": kind = "PlatformIO .pio"
                elif d in ("__pycache__", ".pytest_cache"): kind = "Python cache"
                elif BUILD_RE.match(d) and os.path.isfile(os.path.join(full, "CMakeCache.txt")):
                    kind = "Zephyr build" if os.path.isdir(os.path.join(full, "zephyr")) or any(
                        os.path.isdir(os.path.join(full, x, "zephyr")) for x in os.listdir(full)) else "CMake/JUCE build"
                if kind: cands.append((full, kind)); dn.remove(d)
    return cands, inis

def script_uses(tree_top, unit, name):
    """Does a tracked script in this tree name this build folder? (an installer may rely on it)"""
    rel = os.path.relpath(unit, tree_top) if inside(unit, tree_top) else "."
    rc, out, _ = run(["git", "-C", tree_top, "grep", "-l", "-F", "-e", name, "--", rel])
    files = [l for l in out.splitlines() if re.search(r"\.(sh|command|py|mjs|js|txt|cmake)$|Makefile$|CMakeLists", l)]
    return files[0] if files else None

def plan_clean(args):
    now = time.time(); cutoff = now - RECENT
    keep, todo = [], []
    protected = [(p, "protected in config") for p in PROTECT]
    busy = [(p, "a process has it open") for p in open_paths()] + [(p, "a heavyrun job uses it") for p in heavyrun_paths()]
    prot_units = {}
    for p, why in protected + busy:
        for q in {p, os.path.realpath(p)}:
            u = unit_of(q)
            if u: prot_units.setdefault(u, why)
    cands, inis = find_candidates()
    tr, recent_cache = Tracked(), {}
    for path, kind in cands:
        unit = unit_of(path) or path
        why = prot_units.get(unit)
        if not why:
            for p, w in busy:
                if inside(p, path): why = w; break
            for p, w in protected:
                if inside(p, path) or inside(path, p): why = w; break
        if not why:
            if unit not in recent_cache: recent_cache[unit] = recent_edit(unit, cutoff)
            if recent_cache[unit]: why = f"edited in the last {RECENT // 60} min (an agent may still be working)"
        if not why and tr.tracked(path): why = "holds git-tracked (source) files"
        if not why and kind.startswith(("CMake", "Zephyr")):
            top = tr.top(os.path.dirname(path))
            name = os.path.basename(path)
            if top is None: why = "not in a git tree: can't prove it's build output"
            elif not tr.ignored(path): why = "not git-ignored: can't prove it's build output"
            elif any(name.lower().startswith(k.lower()) for k in CFG["keep_build_prefixes"]): why = "release build folder (kept)"
            else:
                f = script_uses(top, unit, name)
                if f: why = f"a script uses it ({f})"
        (keep if why else todo).append({"path": path, "kind": kind, "why": why})
    for item in todo: item["bytes"] = size_of(item["path"])
    # old toolchain versions: name@version next to a current `name`, version not in any platformio.ini, not in use
    refs = ""
    for f in inis:
        try: refs += open(f, errors="replace").read()
        except OSError: pass
    pk = os.path.join(PIO, "packages")
    busy_paths = [p for p, _ in busy]
    for n in (sorted(os.listdir(pk)) if os.path.isdir(pk) else []):
        if "@" not in n: continue
        full, (base, ver) = os.path.join(pk, n), n.split("@", 1)
        why = None
        if not os.path.isdir(os.path.join(pk, base)): why = "the only copy of this package"
        elif ver in refs or ver.split("+")[0] in refs: why = "a platformio.ini names this version"
        elif any(inside(p, full) for p in busy_paths): why = "in use right now"
        item = {"path": full, "kind": "old PlatformIO toolchain", "why": why}
        if why: keep.append(item)
        else: item["bytes"] = size_of(full); todo.append(item)
    if args.pio_cache:
        dl = os.path.join(PIO, ".cache", "downloads")
        for n in (sorted(os.listdir(dl)) if os.path.isdir(dl) else []):
            f = os.path.join(dl, n)
            if os.path.isfile(f) and now - os.stat(f).st_mtime > int(CFG["pio_cache_days"]) * 86400:
                todo.append({"path": f, "kind": "PlatformIO download archive", "why": None, "bytes": size_of(f)})
    return {"todo": todo, "keep": keep, "ccache": ccache_info()}

def ccache_bin(): return shutil.which("ccache") or ("/opt/homebrew/bin/ccache" if os.path.exists("/opt/homebrew/bin/ccache") else None)
def ccache_info():
    b = ccache_bin()
    if not b or E.get("TIDY_NO_CCACHE") == "1": return None
    _, out, _ = run([b, "-s"])
    m = re.search(r"Cache size \(GB\):\s*([\d.]+)\s*/\s*([\d.]+)", out)
    return {"size_gb": float(m.group(1)), "cap_gb": float(m.group(2))} if m else None

def do_clean(plan, dry):
    freed, moved, failed = 0, [], []
    tdir = os.path.join(TRASH, "tidy_" + time.strftime("%Y-%m-%d_%H%M%S"))
    for it in plan["todo"]:
        if dry: freed += it["bytes"]; continue
        p = it["path"]
        try:   # last look right before the move: changed since planning -> leave it
            if os.lstat(p).st_mtime > time.time() - RECENT and not it["kind"].startswith(("old Platform", "PlatformIO download")):
                failed.append((p, "changed a moment ago")); continue
        except OSError: failed.append((p, "gone")); continue
        os.makedirs(tdir, exist_ok=True)
        rel = short(p).lstrip("~/").replace("/", "__")
        dest = os.path.join(tdir, rel)
        try:
            os.rename(p, dest)     # same disk -> instant; a cross-disk move fails and is skipped (never copy + delete)
            with open(os.path.join(tdir, "TIDY_MANIFEST.txt"), "a") as m: m.write(f"{rel}\t{p}\t{it['bytes']}\n")
            freed += it["bytes"]; moved.append(p)
        except OSError as e: failed.append((p, e.strerror or str(e)))
    cc = plan.get("ccache")
    cc_note = ""
    if cc and cc["size_gb"] > cc["cap_gb"]:
        if dry: cc_note = f"ccache {cc['size_gb']:.1f}/{cc['cap_gb']:.1f} GB: would trim to cap"
        else:
            run([ccache_bin(), "-c"]); after = ccache_info() or cc
            freed += int(max(0, cc["size_gb"] - after["size_gb"]) * GB); cc_note = f"ccache trimmed {cc['size_gb']:.1f} -> {after['size_gb']:.1f} GB"
    elif cc: cc_note = f"ccache {cc['size_gb']:.1f}/{cc['cap_gb']:.1f} GB, under cap"
    return {"freed": freed, "moved": moved, "failed": failed, "trash": tdir if moved else None, "ccache": cc_note}

# ---------------------------------------------------------------- 2. SAVE
def repos():
    """Every git repo that is a root or a first-level folder under a root."""
    out = []
    for root in ROOTS:
        if not os.path.isdir(root): continue
        if os.path.exists(os.path.join(root, ".git")): out.append(root)
        for n in sorted(os.listdir(root)):
            d = os.path.join(root, n)
            if os.path.isdir(d) and not os.path.islink(d) and os.path.exists(os.path.join(d, ".git")): out.append(d)
    return out

def common_dir(r):
    rc, out, _ = run(["git", "-C", r, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    return os.path.realpath(out.strip()) if rc == 0 and out.strip() else None

def slug_of(r):
    rc, url, _ = run(["git", "-C", r, "remote", "get-url", "origin"])
    if rc: return None
    return re.sub(r"\.git$", "", re.sub(r"^(git@github\.com:|https://github\.com/)", "", url.strip()))

def unique_repos():
    """One entry per real repository (worktrees share one). Prefers a main checkout over a worktree."""
    seen = {}
    for r in repos():
        c = common_dir(r)
        if not c: continue
        if c not in seen or (os.path.isdir(os.path.join(r, ".git")) and not os.path.isdir(os.path.join(seen[c], ".git"))): seen[c] = r
    return sorted(seen.values())

def visibility(slug):
    if E.get("TIDY_ASSUME_PRIVATE") == "1": return "PRIVATE"
    rc, out, _ = run(["gh", "repo", "view", slug, "--json", "visibility", "-q", ".visibility"], timeout=30)
    return out.strip() if rc == 0 else None

NOT_ON_ORIGIN = ["--not", "--exclude=refs/remotes/origin/autosave/*", "--remotes=origin"]   # autosave snapshot branches don't count as pushed

def wanted(slug):
    return "*" in PUSH_REPOS or slug in PUSH_REPOS or E.get("TIDY_ALL_REPOS") == "1"

def branch_plan():
    plan = []
    for r in unique_repos():
        slug = slug_of(r)
        if not slug or not wanted(slug): continue
        vis = visibility(slug)
        entry = {"repo": r, "slug": slug, "push": [], "cant": []}
        plan.append(entry)
        if vis != "PRIVATE" and not (vis and CFG["push_public_repos"]):
            entry["cant"].append(("*", f"repo is {vis or 'unknown'}, never pushed by tidy (push_public_repos is off)")); continue
        _, heads, _ = run(["git", "-C", r, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"])
        for l in heads.splitlines():
            b, tip = l.rsplit(" ", 1)
            rc, n, _ = run(["git", "-C", r, "rev-list", "--count", tip] + NOT_ON_ORIGIN)
            n = int(n.strip() or 0) if rc == 0 else 0
            if n == 0: continue
            rc, rtip, _ = run(["git", "-C", r, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{b}"])
            rtip = rtip.strip()
            if rtip and run(["git", "-C", r, "merge-base", "--is-ancestor", rtip, tip])[0] != 0:
                entry["cant"].append((b, f"{n} commit(s), but origin/{b} has moved on: needs a merge, not forced")); continue
            _, names, _ = run(["git", "-C", r, "log", "--name-only", "--format=", tip] + NOT_ON_ORIGIN)
            _, patch, _ = run(["git", "-C", r, "log", "-p", "--format=", tip] + NOT_ON_ORIGIN, timeout=300)
            if any(SECRET_NAMES.search(f) for f in names.splitlines() if f) or SECRET_TEXT.search(patch):
                entry["cant"].append((b, "possible secret in the new commits: check by hand")); continue
            entry["push"].append((b, n, "new branch" if not rtip else "fast-forward"))
    return plan

def do_save(dry):
    res = {"saveall": "", "pushed": [], "cant": []}
    cmd = CFG["save_command"]
    if not cmd: res["saveall"] = "no save_command set"
    elif dry: res["saveall"] = f"would run: {cmd}"
    else:
        rc, out, err = run(["/bin/sh", "-c", cmd], timeout=900)
        res["saveall"] = (out.strip().splitlines() or [err.strip() or f"exit {rc}"])[-1]
    env = dict(E, GIT_SSH_COMMAND="ssh -o ConnectTimeout=8 -o BatchMode=yes", GIT_TERMINAL_PROMPT="0")
    for e in branch_plan():
        name = e["slug"].split("/")[-1] if e["slug"] else os.path.basename(e["repo"])
        res["cant"] += [(name, b, why) for b, why in e["cant"]]
        if not e["push"]: continue
        if dry: res["pushed"] += [(name, b, f"{n} commit(s), {how}") for b, n, how in e["push"]]; continue
        specs = [f"refs/heads/{b}:refs/heads/{b}" for b, _, _ in e["push"]]   # no "+": git refuses anything but a fast-forward
        rc, out, err = run(["git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=15", "-C", e["repo"], "push", "--porcelain", "origin"] + specs, timeout=900, env=env)
        status = {}
        for l in out.splitlines():
            a = l.split("\t")
            if len(a) >= 3 and a[1].startswith("refs/heads/"): status[a[1].split(":")[0][11:]] = (a[0], a[2])
        for b, n, how in e["push"]:
            flag, summ = status.get(b, ("!", (err.strip().splitlines() or ["no answer from the remote"])[-1]))
            if flag in ("!",): res["cant"].append((name, b, f"push refused: {summ}"))
            else: res["pushed"].append((name, b, f"{n} commit(s), {how}"))
    return res

# ---------------------------------------------------------------- 3. BACKUP
def tm_names():
    if E.get("TIDY_NO_TMUTIL") == "1": return set()
    _, out, _ = run(["tmutil", "destinationinfo"], timeout=20)
    return {m.strip() for m in re.findall(r"^Name\s*:\s*(.+)$", out, re.M)} | {os.path.basename(m.strip()) for m in re.findall(r"^Mount Point\s*:\s*(.+)$", out, re.M)}

APPROVED = os.path.join(MG, "tidy_drives.json")   # drives approved once with --approve-drive (by volume UUID)
def vol_uuid(v):
    if E.get("TIDY_FAKE_MOUNTS") == "1":
        try: return open(os.path.join(v, ".fake_uuid")).read().strip()
        except OSError: return None
    _, out, _ = run(["diskutil", "info", v], timeout=20)
    m = re.search(r"Volume UUID:\s*(\S+)", out); return m.group(1) if m else None
def approved():
    try: return json.load(open(APPROVED))
    except Exception: return {}

def volume_problem(v, tm):
    if os.path.islink(v): return "a link (the startup disk)"
    if E.get("TIDY_FAKE_MOUNTS") != "1":
        if not os.path.ismount(v): return "not a mounted drive"
        if os.stat(v).st_dev == os.stat("/").st_dev: return "the startup disk"
    for mk in MARKERS:
        if os.path.exists(os.path.join(v, mk)) and vol_uuid(v) not in approved():
            return f"has a {mk} folder; to use it anyway: tidy --approve-drive {v}"
    n = os.path.basename(v)
    if n.startswith("com.apple.TimeMachine") or n in tm or os.path.exists(os.path.join(v, "Backups.backupdb")): return "Time Machine"
    if not os.access(v, os.W_OK): return "read-only"
    try:
        st = os.statvfs(v)
        if st.f_bavail * st.f_frsize < float(CFG["min_drive_free_gb"]) * GB: return f"under {CFG['min_drive_free_gb']} GB free"
    except OSError: return "can't read free space"
    return None

def pick_drive(explicit=None):
    """-> (path or None, {volume: reason skipped})"""
    skipped, tm = {}, tm_names()
    if explicit:
        v = os.path.realpath(explicit)
        if not os.path.isdir(v): return None, {explicit: "not found"}
        if E.get("TIDY_FAKE_MOUNTS") != "1" and (not os.path.ismount(v) or os.stat(v).st_dev == os.stat("/").st_dev):
            return None, {explicit: "not an external mounted drive"}
        if not os.access(v, os.W_OK): return None, {explicit: "read-only"}
        return v, {}
    try: vols = sorted(os.listdir(VOLUMES))
    except OSError: vols = []
    pref = CFG["backup_drive"]
    order = ([pref] if pref and pref in vols else []) + [v for v in vols if v != pref]
    for n in order:
        v = os.path.join(VOLUMES, n)
        why = volume_problem(v, tm)
        if why: skipped[n] = why; continue
        try: has = any(x.startswith(PREFIX) for x in os.listdir(v))
        except OSError: has = False
        if n == pref or has: return v, skipped
        skipped[n] = f"no {PREFIX}* folder (name it with --drive to use it)"
    return None, skipped

def bundle_names():
    out, used = [], set()
    for r in unique_repos():
        s = slug_of(r)
        name = s.split("/")[-1] if s else os.path.basename(r)
        if name in used: name = f"{name}__{os.path.basename(r)}"
        used.add(name); out.append((name, r))
    return out

def prev_backup(drive, today):
    olds = sorted(x for x in os.listdir(drive) if x.startswith(PREFIX) and x < f"{PREFIX}{today}"
                  and os.path.isdir(os.path.join(drive, x, "files")))
    return os.path.join(drive, olds[-1]) if olds else None

def rsync_cmd(src, dest_parent, prev, dry):
    cmd = ["rsync", "-a", "--stats"] + (["-n"] if dry else [])
    for x in RSYNC_EXCLUDES: cmd += ["--exclude", x]
    if prev and os.path.isdir(os.path.join(prev, "files", os.path.basename(src))):
        cmd.append("--link-dest=" + os.path.join(prev, "files", os.path.basename(src)))   # unchanged files = hard links, no new space
    return cmd + [src.rstrip("/") + "/", os.path.join(dest_parent, os.path.basename(src)) + "/"]

def rsync_stats(out):
    n = re.search(r"Number of (?:regular )?files transferred:\s*([\d,]+)", out)
    s = re.search(r"Total transferred file size:\s*([\d,]+)", out)
    return int(n.group(1).replace(",", "")) if n else 0, int(s.group(1).replace(",", "")) if s else 0

def backup_worker(plan_file):
    """Runs inside heavyrun (light lane, nice). Writes its result next to the plan file."""
    P = json.load(open(plan_file)); res = {"bundles": [], "files": [], "errors": []}
    dest, stamp = P["dest"], time.strftime("%H%M%S")
    gbd = os.path.join(dest, "git_bundles"); inc = os.path.join(gbd, f".incoming_{stamp}"); os.makedirs(inc, exist_ok=True)
    for name, repo in P["bundles"]:
        f = os.path.join(inc, name + ".bundle")
        rc, _, err = run(["git", "-c", "pack.threads=2", "-c", "pack.windowMemory=256m", "-C", repo, "bundle", "create", f, "--all"], timeout=3600)
        ok = rc == 0 and run(["git", "-C", repo, "bundle", "verify", "-q", f], timeout=1800)[0] == 0
        if not ok:
            res["bundles"].append((name, False, (err.strip().splitlines() or ["verify failed"])[-1]))
            if os.path.exists(f): os.rename(f, f + ".FAILED")
            continue
        final = os.path.join(gbd, name + ".bundle")
        if os.path.exists(final):   # same day run again: keep the earlier one, never overwrite
            old = os.path.join(gbd, f"_before_{stamp}"); os.makedirs(old, exist_ok=True); os.rename(final, os.path.join(old, name + ".bundle"))
        os.rename(f, final); res["bundles"].append((name, True, os.path.getsize(final)))
    try: os.rmdir(inc)       # empty unless something failed
    except OSError: pass
    files = os.path.join(dest, "files"); os.makedirs(files, exist_ok=True)
    for src in P["sources"]:
        if not os.path.isdir(src): res["errors"].append(f"{short(src)} missing"); continue
        rc, out, err = run(rsync_cmd(src, files, P.get("prev"), False), timeout=6 * 3600)
        n, b = rsync_stats(out)
        res["files"].append((os.path.basename(src), rc in (0, 24), n, b, (err.strip().splitlines() or [""])[-1]))
    for f in P["extra"]:
        if os.path.exists(f): shutil.copy2(f, dest)
    json.dump(res, open(plan_file + ".result", "w"))
    return 0

def memory_gate():
    """memguard's gates for the heavy part: pressure WARN+ -> wait; afterburner + a priority job running + free < 40 % -> wait."""
    if E.get("TIDY_NO_MEMGATE") == "1": return None
    _, lvl, _ = run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    _, mp, _ = run(["memory_pressure"], timeout=20)
    m = re.search(r"free percentage:\s*(\d+)", mp); free = int(m.group(1)) if m else 50
    if (lvl.strip() or "1") not in ("1", ""): return f"memory pressure is {'WARN' if lvl.strip() == '2' else 'CRITICAL'}"
    try: ab = json.load(open(os.path.join(MG, "afterburner.json"))).get("on")
    except Exception: ab = False
    prio_re = re.compile(PRIORITY_PATTERN, re.I) if PRIORITY_PATTERN else None
    prio = False
    for n in (os.listdir(MG) if os.path.isdir(MG) else []):
        if prio_re and re.match(r"^heavy\.(lock|slot\d+)$", n):
            try: prio |= bool(prio_re.search(open(os.path.join(MG, n, "label")).read()))
            except OSError: pass
    if ab and prio and free < 40: return f"afterburner and a priority job are running and only {free} % memory is free"
    return None

def do_backup(args, dry):
    drive, skipped = pick_drive(args.drive)
    res = {"drive": drive, "skipped_volumes": skipped}
    if not drive: res["skip"] = "no backup drive attached" + (f" ({'; '.join(f'{k}: {v}' for k, v in skipped.items())})" if skipped else ""); return res
    today = time.strftime("%Y-%m-%d"); dest = os.path.join(drive, f"{PREFIX}{today}")
    prev = prev_backup(drive, today)
    P = {"dest": dest, "prev": prev, "bundles": bundle_names(), "sources": [s for s in BACKUP_SOURCES if os.path.isdir(s)], "extra": EXTRA_FILES}
    res.update(dest=dest, prev=prev, plan=P)
    if dry:
        res["bundles"] = [(n, None, r) for n, r in P["bundles"]]
        res["files"] = []
        for src in P["sources"]:   # what rsync would copy (read only, nice)
            target = os.path.join(dest, "files")
            rc, out, _ = run(["nice", "-n", "15"] + rsync_cmd(src, target, prev, True), timeout=3600) if os.path.isdir(dest) or prev else (0, "", "")
            n, b = rsync_stats(out) if out else (None, None)
            res["files"].append((os.path.basename(src), True, n, b, ""))
        return res
    why = memory_gate()
    if why: res["skip"] = f"backup postponed: {why}; run `tidy --only backup` later"; return res
    os.makedirs(dest, exist_ok=True)
    os.makedirs(MG, exist_ok=True)
    pf = os.path.join(MG, f"tidy_backup_plan_{os.getpid()}.json"); json.dump(P, open(pf, "w"))
    me = [sys.executable, "-I", os.path.realpath(__file__), "--_backup-worker", pf]
    if E.get("TIDY_NO_HEAVYRUN") == "1" or not os.path.exists(HEAVYRUN): rc = subprocess.call(["nice", "-n", "15"] + me)
    else: rc = subprocess.call([HEAVYRUN, f"tidy: backup bundles + rsync to {os.path.basename(drive)} (~0.6 GB peak, I/O)"] + me,
                               env=dict(E, HEAVY_LIGHT="1", MEMGUARD_HOME=MG))
    try: r = json.load(open(pf + ".result"))
    except Exception: r = {"bundles": [], "files": [], "errors": [f"backup job ended with exit {rc} and no result"]}
    for f in (pf, pf + ".result"):
        try: os.rename(f, os.path.join(TRASH, os.path.basename(f))) if os.path.exists(f) else None
        except OSError: pass
    res.update(r)
    return res

# ---------------------------------------------------------------- 4. REPORT
def notify(msg):
    if E.get("TIDY_NO_NOTIFY") == "1": return
    try:
        sys.path.insert(0, HERE); import memguard; memguard.notify(msg.replace('"', "'"))
    except Exception: run(["osascript", "-e", f'display notification "{msg}" with title "tidy"'])

def save_state(key):
    try: st = json.load(open(STATE))
    except Exception: st = {}
    st[key] = time.time()
    try: os.makedirs(MG, exist_ok=True); json.dump(st, open(STATE, "w"))
    except OSError: pass

def status():
    try: st = json.load(open(STATE))
    except Exception: st = {}
    drive, skipped = pick_drive(None)
    _, mp, _ = run(["memory_pressure"], timeout=20); m = re.search(r"free percentage:\s*(\d+)", mp)
    try: ab = json.load(open(os.path.join(MG, "afterburner.json")))
    except Exception: ab = {}
    ab_on = bool(ab.get("on")) and ab.get("until", 0) > time.time()
    out = {"mem_free_pct": int(m.group(1)) if m else None, "load1": round(os.getloadavg()[0], 1),
           "afterburner": ab_on, "afterburner_left_min": int((ab.get("until", 0) - time.time()) // 60) if ab_on else 0,
           "drive": os.path.basename(drive) if drive else None, "drives_skipped": skipped,
           "last_tidy": st.get("clean"), "last_save": st.get("save"), "last_backup": st.get("backup"),
           "disk_free_gb": round(shutil.disk_usage(HOME).free / GB, 1)}
    print(json.dumps(out))

def main(argv):
    ap = argparse.ArgumentParser(prog="tidy", description="clean build output, save + push, back up to a drive")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--only", default="clean,save,backup")
    ap.add_argument("--drive"); ap.add_argument("--pio-cache", action="store_true")
    ap.add_argument("--status", action="store_true"); ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--_backup-worker", dest="worker")
    ap.add_argument("--approve-drive", help="allow backups to this drive although it has a refuse marker folder (remembered by UUID)")
    a = ap.parse_args(argv)
    if a.worker: return backup_worker(a.worker)
    if a.status: status(); return 0
    if a.approve_drive:
        v = os.path.realpath(a.approve_drive); u = vol_uuid(v)
        if not u or os.path.islink(a.approve_drive): print(f"tidy: {a.approve_drive} is not a mounted drive"); return 1
        d = approved(); d[u] = {"name": os.path.basename(v), "approved": time.strftime("%Y-%m-%d %H:%M")}
        os.makedirs(MG, exist_ok=True)
        json.dump(d, open(APPROVED, "w"), indent=1); print(f"tidy: backups may now go to {os.path.basename(v)} ({u}); new dated folders only, nothing on it is ever deleted"); return 0
    steps = [s.strip() for s in a.only.split(",") if s.strip()]
    bad = [s for s in steps if s not in ("clean", "save", "backup")]
    if bad: ap.error(f"unknown step {bad}")
    dry, summary = a.dry_run, []
    say = lambda s="": print(s, flush=True)
    say(f"tidy {'DRY RUN (nothing changes) ' if dry else ''}{time.strftime('%Y-%m-%d %H:%M')}  steps: {', '.join(steps)}")
    if "clean" in steps:
        say("\n1 CLEAN  (to one dated Trash folder, never rm)")
        p = plan_clean(a)
        todo = sorted(p["todo"], key=lambda x: -x["bytes"]); show = todo if a.verbose else todo[:25]
        for it in show: say(f"  {'would move' if dry else 'move'} {gb(it['bytes']):>9}  {it['kind']:<26} {short(it['path'])}")
        if len(todo) > len(show): say(f"  ... and {len(todo) - len(show)} smaller item(s), {gb(sum(i['bytes'] for i in todo[len(show):]))} together (-v lists all)")
        groups = {}
        for it in p["keep"]: groups.setdefault(it["why"], []).append(it)
        for why, its in groups.items():
            if a.verbose or len(its) <= 6: [say(f"  skip  {short(i['path'])}  ({why})") for i in its]
            else: say(f"  skip  {len(its)} items ({why}), e.g. {short(its[0]['path'])}; -v lists all")
        r = do_clean(p, dry)
        if r["ccache"]: say("  " + r["ccache"])
        for f, why in r["failed"]: say(f"  NOT moved {short(f)} ({why})")
        say(f"  {'would free' if dry else 'freed'} {gb(r['freed'])}" + (f" -> {short(r['trash'])}" if r["trash"] else ""))
        summary.append(f"{'would free' if dry else 'freed'} {gb(r['freed'])} ({len(p['todo'])} item(s){', ' + str(len(r['failed'])) + ' not moved' if r['failed'] else ''})")
        if not dry: save_state("clean")
    if "save" in steps:
        say("\n2 SAVE")
        r = do_save(dry); say("  save_command: " + r["saveall"])
        for repo, b, what in r["pushed"]: say(f"  {'would push' if dry else 'pushed'} {repo}: {b} ({what})")
        for repo, b, why in r["cant"]: say(f"  NOT pushed {repo}: {b} ({why})")
        if not r["pushed"] and not r["cant"]:
            say("  every branch in push_repos is already on origin" if PUSH_REPOS else "  push_repos is empty: nothing to push")
        summary.append(f"{'would push' if dry else 'pushed'} {len(r['pushed'])} branch(es)" + (f", {len(r['cant'])} not pushable" if r["cant"] else ""))
        if not dry: save_state("save")
    if "backup" in steps:
        say("\n3 BACKUP")
        r = do_backup(a, dry)
        for k, v in r.get("skipped_volumes", {}).items(): say(f"  not using {os.path.join(VOLUMES, k)}: {v}")
        if r.get("skip"): say("  SKIPPED: " + r["skip"].split(" (")[0]); summary.append("backup skipped: " + r["skip"].split(" (")[0])
        else:
            say(f"  drive {r['drive']}  ->  {r['dest']}" + (f"  (unchanged files hard-linked to {os.path.basename(r['prev'])})" if r.get("prev") else ""))
            okb = 0
            for name, ok, info in r["bundles"]:
                if ok is None: say(f"  would bundle + verify {name}.bundle  ({short(info)})")
                elif ok: okb += 1; say(f"  bundle {name}.bundle {gb(info)} verified")
                else: say(f"  bundle {name} FAILED: {info}")
            nf = 0
            for name, ok, n, b, err in r["files"]:
                if n is None: say(f"  would copy {name} (first copy on this drive)")
                else: nf += n or 0; say(f"  {'would copy' if dry else ('copied' if ok else 'FAILED')} {name}: {n} file(s), {gb(b or 0)}" + (f" ({err})" if not ok and err else ""))
            for e in r.get("errors", []): say("  ERROR " + e)
            if EXTRA_FILES: say(f"  + {', '.join(os.path.basename(f) for f in EXTRA_FILES)} at the top of the dated folder")
            if dry: summary.append(f"would back up {len(r['bundles'])} bundle(s) + {len(r['files'])} folder(s) to {os.path.basename(r['drive'])}")
            else:
                summary.append(f"{okb}/{len(r['bundles'])} bundles verified, {nf} file(s) copied to {os.path.basename(r['drive'])}"); save_state("backup")
    line = "; ".join(summary)
    say(f"\nSUMMARY: {line}")
    if not dry:
        try:
            os.makedirs(os.path.dirname(LOGF), exist_ok=True)
            with open(LOGF, "a") as f: f.write(f"- {time.strftime('%Y-%m-%d %H:%M:%S')} [tidy] {line}\n")
        except OSError: pass
        notify("tidy: " + line[:200])
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
