"""Tests for tidy.py: safety rules on a scratch fake tree. Run: python3 -m unittest discover -s tests
Everything happens in a temp HOME / temp Volumes / temp Trash / temp memguard home; no real tree, drive, remote or queue is touched."""
import json, os, shutil, subprocess, sys, tempfile, time, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIDY = os.path.join(ROOT, "tidy.py")
OLD = time.time() - 3 * 3600
PREFIX = "MyBackup_"

def sh(*a, cwd=None): subprocess.run(a, cwd=cwd, check=True, capture_output=True)
def mk(p, text="x"):
    os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(text)
def git_repo(d, files, ignore=""):
    os.makedirs(d, exist_ok=True); sh("git", "init", "-q", "-b", "main", d)
    for f, t in files.items(): mk(os.path.join(d, f), t)
    if ignore: mk(os.path.join(d, ".gitignore"), ignore)
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "init")
def age(top):
    for dp, dn, fn in os.walk(top):
        for n in dn + fn:
            try: os.utime(os.path.join(dp, n), (OLD, OLD), follow_symlinks=False)
            except OSError: pass
    os.utime(top, (OLD, OLD))

class Base(unittest.TestCase):
    def setUp(self):
        self.t = os.path.realpath(tempfile.mkdtemp(prefix="tidytest."))
        self.H, self.V, self.T, self.G = (os.path.join(self.t, x) for x in ("home", "Volumes", "Trash", "mg"))
        for d in (self.H, self.V, self.T, self.G): os.makedirs(d)
        self.W = os.path.join(self.H, "Work")
        json.dump({"tidy": {
            "roots": ["~/Work", "~/Desktop/MainRepo"],
            "protect": ["~/Work/bench_tree", "~/Work/named_tree"],
            "push_repos": ["*"], "save_command": "true",
            "backup_sources": ["~/Desktop/Business", "~/Desktop/Decks"],
            "backup_extra_files": ["~/Desktop/Business/HOW_TO.md"],
            "backup_folder_prefix": PREFIX, "refuse_drive_markers": ["DCIM"]}}, open(os.path.join(self.G, "config.json"), "w"))
        env = {k: v for k, v in os.environ.items() if k not in ("MEMGUARD_CONFIG", "MEMGUARD_HOME")}
        self.env = dict(env, TIDY_HOME=self.H, TIDY_VOLUMES=self.V, TIDY_TRASH=self.T, TIDY_MEMGUARD_DIR=self.G,
                        TIDY_LOG=os.path.join(self.H, "log.md"), TIDY_NO_NOTIFY="1", TIDY_NO_CCACHE="1", TIDY_NO_TMUTIL="1",
                        TIDY_FAKE_MOUNTS="1", TIDY_NO_HEAVYRUN="1", TIDY_NO_MEMGATE="1", TIDY_ASSUME_PRIVATE="1")
        self.procs = []
    def tearDown(self):
        for p in self.procs: p.kill(); p.wait()
        shutil.rmtree(self.t, ignore_errors=True)   # the scratch temp dir this test created
    def tidy(self, *a):
        p = subprocess.run([sys.executable, "-I", TIDY] + list(a), env=self.env, capture_output=True, text=True, timeout=600)
        self.assertEqual(p.returncode, 0, p.stderr); return p.stdout

class Clean(Base):
    def build_tree(self):
        W, H = self.W, self.H
        for name in ("bench_tree", "named_tree", "recent_wt", "busy_wt", "hr_wt"):
            git_repo(os.path.join(W, name), {"firmware/src/main.c": "int main(){}"}, ".pio/\n")
            mk(os.path.join(W, name, "firmware/.pio/build/fw.bin"))
        git_repo(os.path.join(W, "stale_wt"), {"firmware/src/main.c": "int main(){}", "tools/build_src/CMakeCache.txt": "tracked",
                                               "tools/build_src/real_source.c": "int x;", "app/mod.py": "x=1"},
                 ".pio/\nbuild_base/\n__pycache__/\n")
        S = os.path.join(W, "stale_wt")
        mk(os.path.join(S, "firmware/.pio/build/fw.bin")); mk(os.path.join(S, "firmware/platformio.ini"), "platform_packages = toolchain-y@2.0.0\n")
        mk(os.path.join(S, "board/build_base/CMakeCache.txt")); os.makedirs(os.path.join(S, "board/build_base/zephyr"))
        mk(os.path.join(S, "board/build_base/app/CMakeCache.txt"))
        mk(os.path.join(S, "app/__pycache__/mod.cpython-39.pyc"))
        mk(os.path.join(S, "other/build_notignored/CMakeCache.txt"))
        mk(os.path.join(S, "dist/__pycache__/x.pyc"))                                                                         # release folder: never entered
        mk(os.path.join(S, ".venv/pyvenv.cfg")); mk(os.path.join(S, ".venv/lib/site-packages/m/__pycache__/m.pyc"))           # venv: never entered
        MR = os.path.join(H, "Desktop", "MainRepo")
        git_repo(MR, {"Prod/CMakeLists.txt": "x", "Prod/make_installer.sh": "cp build-universal/x out"}, "build*/\n")
        mk(os.path.join(MR, "Prod/build-release/CMakeCache.txt")); mk(os.path.join(MR, "Prod/build-universal/CMakeCache.txt"))
        mk(os.path.join(MR, "Prod/build-scratch/CMakeCache.txt"))
        mk(os.path.join(H, "outside/proj/.pio/x"))                          # outside the allowed roots
        os.symlink(os.path.join(H, "outside/proj"), os.path.join(W, "link_wt"))
        P = os.path.join(H, ".platformio/packages")
        for n in ("toolchain-x", "toolchain-x@1.0.0", "toolchain-y", "toolchain-y@2.0.0", "toolchain-z@3.0.0"): mk(os.path.join(P, n, "bin/gcc"))
        for top in (W, os.path.join(H, "Desktop"), os.path.join(H, "outside"), os.path.join(H, ".platformio")): age(top)
        mk(os.path.join(W, "recent_wt/firmware/src/new.c"))               # an agent is editing right now
        self.procs.append(subprocess.Popen(["sleep", "120"], cwd=os.path.join(W, "busy_wt"), stdout=subprocess.DEVNULL))   # a process works in busy_wt
        os.makedirs(os.path.join(self.G, "heavy.lock")); mk(os.path.join(self.G, "heavy.lock/pid"), str(os.getpid()))
        mk(os.path.join(self.G, "heavy.lock/label"), "agent: build firmware in ~/Work/hr_wt (0.8 GB)")

    def test_safety_rules(self):
        self.build_tree(); W, H = self.W, self.H
        dry = self.tidy("--dry-run", "--only", "clean", "-v")
        self.assertIn("would free", dry)
        self.assertEqual(os.listdir(self.T), [], "dry run moved something")
        out = self.tidy("--only", "clean", "-v")
        gone = lambda p: not os.path.exists(os.path.join(H, p))
        here = lambda p: os.path.exists(os.path.join(H, p))
        for p in ("Work/stale_wt/firmware/.pio", "Work/stale_wt/board/build_base", "Work/stale_wt/app/__pycache__",
                  "Desktop/MainRepo/Prod/build-scratch", ".platformio/packages/toolchain-x@1.0.0"):
            self.assertTrue(gone(p), f"{p} should be in the Trash\n{out}")
        for p, why in (("Work/bench_tree/firmware/.pio", "protected in config"), ("Work/named_tree/firmware/.pio", "protected in config"),
                       ("Work/recent_wt/firmware/.pio", "edited in the last 30 min"),
                       ("Work/busy_wt/firmware/.pio", "a process has it open"), ("Work/hr_wt/firmware/.pio", "heavyrun"),
                       ("Work/stale_wt/tools/build_src/real_source.c", "git-tracked"), ("Work/stale_wt/other/build_notignored", "not git-ignored"),
                       ("Work/stale_wt/firmware/src/main.c", None), ("Work/stale_wt/app/mod.py", None),
                       ("Work/stale_wt/dist/__pycache__", None), ("Work/stale_wt/.venv/lib/site-packages/m/__pycache__", None),
                       ("Desktop/MainRepo/Prod/build-release", "release build folder"),
                       ("Desktop/MainRepo/Prod/build-universal", "a script uses it"), ("outside/proj/.pio", None),
                       (".platformio/packages/toolchain-y@2.0.0", "platformio.ini names"), (".platformio/packages/toolchain-z@3.0.0", "only copy"),
                       (".platformio/packages/toolchain-x", None)):
            self.assertTrue(here(p), f"{p} must not be touched\n{out}")
            if why: self.assertIn(why, out)
        runs = [d for d in os.listdir(self.T) if d.startswith("tidy_")]
        self.assertEqual(len(runs), 1)
        man = open(os.path.join(self.T, runs[0], "TIDY_MANIFEST.txt")).read()
        self.assertIn(os.path.join(W, "stale_wt/firmware/.pio"), man)
        self.assertIn("freed", open(os.path.join(H, "log.md")).read())

    def test_status_is_json(self):
        st = json.loads(self.tidy("--status").strip().splitlines()[-1])
        for k in ("mem_free_pct", "afterburner", "drive", "last_tidy", "disk_free_gb"): self.assertIn(k, st)

class Backup(Base):
    def setUp(self):
        super().setUp()
        git_repo(os.path.join(self.W, "repo_a"), {"a.txt": "a"})
        biz = os.path.join(self.H, "Desktop/Business"); mk(os.path.join(biz, "doc.txt"), "hello")
        mk(os.path.join(biz, "HOW_TO.md"), "how"); mk(os.path.join(biz, "node_modules/x.js"))
        mk(os.path.join(self.H, "Desktop/Decks/deck.pdf"))
        self.card = os.path.join(self.V, "CAMERA"); os.makedirs(os.path.join(self.card, "DCIM")); os.makedirs(os.path.join(self.card, PREFIX + "2026-01-01/files"))
        self.tm = os.path.join(self.V, "TMVOL"); os.makedirs(os.path.join(self.tm, "Backups.backupdb")); os.makedirs(os.path.join(self.tm, PREFIX + "2026-01-01/files"))
    def snapshot(self, d): return sorted(os.path.join(dp, n) for dp, dn, fn in os.walk(d) for n in dn + fn)

    def test_never_a_marked_drive_or_time_machine(self):
        before = self.snapshot(self.V)
        out = self.tidy("--only", "backup")
        self.assertIn("SKIPPED: no backup drive attached", out)
        self.assertIn("DCIM", out); self.assertIn("Time Machine", out)
        self.assertEqual(before, self.snapshot(self.V))

    def test_backup_to_good_drive(self):
        good = os.path.join(self.V, "GOOD"); prev = os.path.join(good, PREFIX + "2026-01-01/files/Business")
        mk(os.path.join(prev, "doc.txt"), "hello"); os.utime(os.path.join(prev, "doc.txt"), (OLD, OLD))
        os.utime(os.path.join(self.H, "Desktop/Business/doc.txt"), (OLD, OLD))
        before_card = self.snapshot(self.card)
        out = self.tidy("--only", "backup")
        dest = os.path.join(good, f"{PREFIX}{time.strftime('%Y-%m-%d')}")
        b = os.path.join(dest, "git_bundles/repo_a.bundle")
        self.assertTrue(os.path.exists(b), out); self.assertIn("verified", out)
        self.assertEqual(subprocess.run(["git", "-C", os.path.join(self.W, "repo_a"), "bundle", "verify", "-q", b], capture_output=True).returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(dest, "files/Business/doc.txt")))
        self.assertTrue(os.path.exists(os.path.join(dest, "files/Decks/deck.pdf")))
        self.assertTrue(os.path.exists(os.path.join(dest, "HOW_TO.md")))
        self.assertFalse(os.path.exists(os.path.join(dest, "files/Business/node_modules")))
        self.assertEqual(os.stat(os.path.join(dest, "files/Business/doc.txt")).st_ino, os.stat(os.path.join(prev, "doc.txt")).st_ino,
                         "an unchanged file should be a hard link to the previous dated folder")
        self.assertTrue(os.path.isdir(os.path.dirname(prev)), "the previous dated folder must be kept")
        self.assertEqual(before_card, self.snapshot(self.card))
        out2 = self.tidy("--only", "backup")    # same day again: the earlier bundle is kept, not overwritten
        self.assertTrue([d for d in os.listdir(os.path.join(dest, "git_bundles")) if d.startswith("_before_")], out2)

    def test_approved_drive(self):
        mk(os.path.join(self.card, ".fake_uuid"), "UUID-CARD")
        self.assertIn("SKIPPED", self.tidy("--only", "backup"))
        self.tidy("--approve-drive", self.card)
        out = self.tidy("--only", "backup")
        self.assertIn("verified", out); self.assertTrue(os.path.isdir(os.path.join(self.card, "DCIM")))
        self.assertFalse(os.path.exists(os.path.join(self.tm, f"{PREFIX}{time.strftime('%Y-%m-%d')}")))

    def test_explicit_drive_overrides(self):
        out = self.tidy("--only", "backup", "--drive", self.card)
        self.assertIn("verified", out)

class Save(Base):
    def test_push_ff_only_and_secrets(self):
        origin = os.path.join(self.t, "origin.git"); sh("git", "init", "-q", "--bare", origin)
        r = os.path.join(self.W, "app"); git_repo(r, {"a.txt": "a"})
        sh("git", "-C", r, "remote", "add", "origin", origin); sh("git", "-C", r, "push", "-q", "origin", "main")
        c = lambda *a: sh("git", "-C", r, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a)
        c("checkout", "-qb", "feature"); mk(os.path.join(r, "b.txt")); c("add", "-A"); c("commit", "-qm", "b")
        c("checkout", "-qb", "leaky", "main"); mk(os.path.join(r, ".env"), "KEY=1"); c("add", "-A", "-f"); c("commit", "-qm", "env")
        c("checkout", "-qb", "diverged", "main"); mk(os.path.join(r, "d.txt")); c("add", "-A"); c("commit", "-qm", "d"); c("push", "-q", "origin", "diverged")
        c("reset", "-q", "--hard", "main"); mk(os.path.join(r, "e.txt")); c("add", "-A"); c("commit", "-qm", "e")
        c("checkout", "-q", "main")
        out = self.tidy("--only", "save")
        heads = subprocess.run(["git", "-C", origin, "branch", "--format=%(refname:short)"], capture_output=True, text=True).stdout.split()
        self.assertIn("feature", heads, out); self.assertNotIn("leaky", heads, out)
        self.assertIn("possible secret", out); self.assertIn("needs a merge", out)
        self.assertNotEqual(subprocess.run(["git", "-C", origin, "rev-parse", "diverged"], capture_output=True, text=True).stdout,
                            subprocess.run(["git", "-C", r, "rev-parse", "diverged"], capture_output=True, text=True).stdout, "diverged branch was forced")

    def test_public_repo_is_never_pushed_by_default(self):
        self.env.pop("TIDY_ASSUME_PRIVATE")
        origin = os.path.join(self.t, "origin.git"); sh("git", "init", "-q", "--bare", origin)
        r = os.path.join(self.W, "app"); git_repo(r, {"a.txt": "a"})
        sh("git", "-C", r, "remote", "add", "origin", origin)      # not a GitHub slug gh can see: visibility unknown
        out = self.tidy("--only", "save")
        self.assertIn("never pushed by tidy", out)
        self.assertEqual(subprocess.run(["git", "-C", origin, "branch"], capture_output=True, text=True).stdout.strip(), "")

if __name__ == "__main__": unittest.main()
