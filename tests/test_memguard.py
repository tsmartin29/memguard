"""Tests for memguard's dispatcher, job detection and the heavyrun queue wrapper. Run: python3 -m unittest discover -s tests -v"""
import os, subprocess, sys, tempfile, time, unittest, importlib, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GB = 1 << 30

class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="memguard-test-")
        os.environ["MEMGUARD_HOME"] = self.home
        sys.path.insert(0, ROOT)
        sys.modules.pop("memguard", None)
        self.mg = importlib.import_module("memguard")
        self.mg.mem = lambda: (self.avail, 1)
        self.mg.disk_and_swap = lambda: (self.disk, self.swap)
        self.avail, self.disk, self.swap = 5 * GB, 100 * GB, 0
        self.sleepers = []
    def tearDown(self):
        for p in self.sleepers: p.kill(); p.wait()
        shutil.rmtree(self.home, ignore_errors=True)
    def ticket(self, name, label):
        p = subprocess.Popen(["sleep", "60"]); self.sleepers.append(p)
        f = f"{name}-{p.pid}"
        os.makedirs(os.path.join(self.home, "queue"), exist_ok=True)
        open(os.path.join(self.home, "queue", f), "w").write(label)
        return f
    def visible(self):
        return open(os.path.join(self.home, "visible.txt")).read().split()

class DispatchTests(Base):
    def test_first_come_first_served(self):
        a, b = self.ticket("1000", "a: job"), self.ticket("2000", "b: job")
        self.mg.dispatch()
        self.assertEqual(self.visible(), [a])           # 5 GB available = one slot
        self.assertTrue(os.path.exists(os.path.join(self.home, "queue_hold", b)))

    def test_rank_file_wins_over_age(self):
        a, b = self.ticket("1000", "docs: render"), self.ticket("2000", "release: build")
        open(os.path.join(self.home, "rank.txt"), "w").write("# comment\nrelease\n")
        self.mg.dispatch()
        self.assertEqual(self.visible(), [b])

    def test_priority_ticket_first(self):
        a, b = self.ticket("1000", "a"), self.ticket("0-3000", "urgent")
        self.mg.dispatch()
        self.assertEqual(self.visible(), [b])

    def test_more_memory_more_slots(self):
        a, b, c = self.ticket("1", "a"), self.ticket("2", "b"), self.ticket("3", "c")
        self.avail = 15 * GB
        self.mg.dispatch()
        self.assertEqual(self.visible(), [a, b, c])

    def test_dead_ticket_removed(self):
        d = os.path.join(self.home, "queue"); os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "1000-999999"), "w").write("ghost")   # pid that does not exist
        self.mg.dispatch()
        self.assertFalse(os.path.exists(os.path.join(d, "1000-999999")))

    def test_low_disk_holds_new_jobs(self):
        self.ticket("1000", "a"); self.disk = 2 * GB
        self.mg.dispatch()
        self.assertEqual(self.visible(), [])
        self.assertTrue(os.path.exists(os.path.join(self.home, "hold_disk_swap")))
        self.disk = 100 * GB; self.mg.dispatch()
        self.assertFalse(os.path.exists(os.path.join(self.home, "hold_disk_swap")))

    def test_high_swap_holds_new_jobs(self):
        self.ticket("1000", "a"); self.swap = 8 * GB
        self.mg.dispatch()
        self.assertEqual(self.visible(), [])

class JobDetectionTests(Base):
    def proc(self, pid, ppid, cmd, rss=2 * GB):
        return dict(pid=pid, ppid=ppid, rss=rss, state="S", etime="01:00", cmd=cmd)
    def test_session_child_is_a_job_and_protected_is_not(self):
        P = {1: self.proc(1, 0, "/sbin/launchd", 1),
             10: self.proc(10, 1, "/Users/x/.local/share/claude/versions/2.1.0 --resume", 1 * GB),
             11: self.proc(11, 10, "/bin/zsh -c python3 render.py", 10 << 20),
             12: self.proc(12, 11, "python3 render.py", 2 * GB),
             20: self.proc(20, 10, "/Applications/Safari.app/Contents/MacOS/Safari", 3 * GB),
             30: self.proc(30, 1, "python3 unrelated.py", 4 * GB)}   # not started by a session
        roots = [j["root"] for j in self.mg.session_jobs(P)]
        self.assertEqual(roots, [11])                    # the render tree, grouped under its shell
        self.assertGreaterEqual(self.mg.session_jobs(P)[0]["rss"], 2 * GB)

    def test_heavyrun_jobs_count_when_detached(self):
        P = {1: self.proc(1, 0, "/sbin/launchd", 1),
             40: self.proc(40, 1, "/bin/bash /home/x/memguard/heavyrun label sleep 5", 5 << 20),
             41: self.proc(41, 40, "python3 train.py", 3 * GB)}
        self.assertEqual([j["root"] for j in self.mg.session_jobs(P)], [41])

class HeavyrunTests(Base):
    def test_runs_command_and_logs(self):
        env = dict(os.environ, MEMGUARD_HOME=self.home, HEAVY_MIN_FREE="0")
        r = subprocess.run([os.path.join(ROOT, "heavyrun"), "test: echo", "sh", "-c", "echo workers=$HEAVY_WORKERS"],
                           env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("workers=", r.stdout)
        log = open(os.path.join(self.home, "heavy_queue.log")).read()
        self.assertIn("QUEUED", log); self.assertIn("START", log); self.assertIn("DONE", log)
        self.assertEqual(os.listdir(os.path.join(self.home, "queue")), [])       # ticket cleaned up
        self.assertFalse(os.path.exists(os.path.join(self.home, "heavy.lock")))  # slot released

    def test_exit_code_is_passed_through(self):
        env = dict(os.environ, MEMGUARD_HOME=self.home, HEAVY_MIN_FREE="0")
        r = subprocess.run([os.path.join(ROOT, "heavyrun"), "test: fail", "sh", "-c", "exit 7"], env=env, capture_output=True, timeout=90)
        self.assertEqual(r.returncode, 7)

if __name__ == "__main__":
    unittest.main()
