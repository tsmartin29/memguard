"""Tests for memguard's dispatcher, job detection and the heavyrun queue wrapper. Run: python3 -m unittest discover -s tests -v"""
from unittest import mock
import json, os, subprocess, sys, tempfile, time, unittest, importlib, shutil

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
        self.mg.CFG["notify"] = False   # no real Mac notifications during tests
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

    def test_old_swap_with_plenty_of_memory_does_not_hold(self):
        a = self.ticket("1000", "a"); self.swap = 8 * GB; self.avail = 15 * GB
        self.mg.dispatch()
        self.assertIn(a, self.visible())
        self.assertFalse(os.path.exists(os.path.join(self.home, "hold_disk_swap")))

    def test_very_high_swap_holds_even_with_memory(self):
        self.ticket("1000", "a"); self.swap = 11 * GB; self.avail = 15 * GB
        self.mg.dispatch()
        self.assertEqual(self.visible(), [])

    def test_stall_alert_once_for_old_ticket_only(self):
        now = time.time_ns()
        self.ticket(str(now - 10 * 60 * 10**9), "first: runs")
        old = self.ticket(str(now - 60 * 60 * 10**9 + 1), "old: waited an hour")
        new = self.ticket(str(now), "new: just queued")
        self.disk = 2 * GB                  # hold: nothing runs
        self.mg.dispatch(); self.mg.dispatch()
        lines = open(os.path.join(self.home, "stalled.txt")).read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("old: waited an hour", lines[0]); self.assertIn("disk/swap hold", lines[0])

class ManualOrderTests(Base):
    def T(self, *labels): return {f"t{i}": ("d", l) for i, l in enumerate(labels)}
    def test_listed_tickets_swap_among_their_own_positions(self):
        T = self.T("a", "b", "c", "d")
        self.assertEqual(self.mg.apply_manual_order(["t0", "t1", "t2", "t3"], T, ["t3", "t1"]), ["t0", "t3", "t2", "t1"])
    def test_full_reorder(self):
        T = self.T("a", "b", "c")
        self.assertEqual(self.mg.apply_manual_order(["t0", "t1", "t2"], T, ["t2", "t0", "t1"]), ["t2", "t0", "t1"])
    def test_unknown_duplicate_zero_and_priority_label_are_ignored(self):
        T = self.T("a", "Release PRIORITY: build", "c")
        T["0-9-1"] = ("d", "urgent")
        out = self.mg.apply_manual_order(["t0", "t1", "t2"], T, ["gone", "t2", "t2", "t1", "0-9-1", "t0"])
        self.assertEqual(out, ["t2", "t1", "t0"])   # t1 matches priority_pattern and never moves; t2 and t0 swap
    def test_priority_pattern_is_configurable(self):
        self.mg.PRIORITY_RE = self.mg.re.compile("urgent-work", self.mg.re.I)
        T = self.T("a", "urgent-work: x", "c")
        self.assertEqual(self.mg.apply_manual_order(["t0", "t1", "t2"], T, ["t2", "t1", "t0"]), ["t2", "t1", "t0"])
        self.mg.PRIORITY_RE = None   # empty pattern = no priority labels, so every ticket can move
        self.assertEqual(self.mg.apply_manual_order(["t0", "t1"], self.T("a", "priority"), ["t1", "t0"]), ["t1", "t0"])
    def test_empty_manual_changes_nothing(self):
        self.assertEqual(self.mg.apply_manual_order(["t0", "t1"], self.T("a", "b"), []), ["t0", "t1"])
    def test_read_comments_blank_lines_and_labels(self):
        path = os.path.join(self.home, "manual_order")
        open(path, "w").write("# comment\n\n111-5\tlabel one\n222-5  label two\n")
        self.assertEqual(self.mg.read_manual_order(path), ["111-5", "222-5"])
    def test_read_missing_file(self):
        self.assertEqual(self.mg.read_manual_order(os.path.join(self.home, "nope")), [])
    def test_dispatch_uses_manual_order_and_rank_for_the_rest(self):
        a, b, c = self.ticket("1", "job a"), self.ticket("2", "job b"), self.ticket("3", "job c")
        open(os.path.join(self.home, "manual_order"), "w").write(f"{c}\n{a}\n")
        self.mg.dispatch()
        self.assertEqual(self.visible(), [c])       # c and a swap places; b keeps the middle slot
        open(os.path.join(self.home, "manual_order"), "w").write(f"{b}\n")
        self.mg.dispatch()
        self.assertEqual(self.visible(), [a])       # one listed ticket keeps its own place, so arrival order returns

class LightLaneTests(Base):
    def light(self, name, label):
        p = subprocess.Popen(["sleep", "60"]); self.sleepers.append(p)
        f = f"{name}-{p.pid}"
        os.makedirs(os.path.join(self.home, "queue_light"), exist_ok=True)
        open(os.path.join(self.home, "queue_light", f), "w").write(label)
        return f
    def test_dispatch_writes_visible_light_in_rank_order(self):
        a, b = self.light("1", "[light] job a"), self.light("2", "[light] job b")
        open(os.path.join(self.home, "rank.txt"), "w").write("job b\n")
        self.mg.dispatch("queue_light", "queue_light_hold", heavy_lane=False)
        self.assertEqual(open(os.path.join(self.home, "visible_light.txt")).read().split(), [b])
        self.assertEqual(os.listdir(os.path.join(self.home, "queue_light")), [b])
        self.assertEqual(os.listdir(os.path.join(self.home, "queue_light_hold")), [a])
    def test_dry_run_writes_nothing(self):
        self.light("1", "[light] job a"); self.mg.DRY = True
        try: self.mg.dispatch("queue_light", "queue_light_hold", heavy_lane=False)
        finally: self.mg.DRY = False
        self.assertFalse(os.path.exists(os.path.join(self.home, "visible_light.txt")))
    def run_light(self, env_extra=None, wait=3):
        env = dict(os.environ, MEMGUARD_HOME=self.home, LIGHT_MIN_FREE="0", HEAVY_LIGHT="1")
        p = subprocess.Popen([os.path.join(ROOT, "heavyrun"), "test: light", "true"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sleepers.append(p)
        try: p.wait(timeout=wait)
        except subprocess.TimeoutExpired: pass
        return p.poll()
    def test_light_waiter_starts_at_once_when_alone(self):
        open(os.path.join(self.home, "visible_light.txt"), "w").write("")   # fresh list, nothing named
        self.assertEqual(self.run_light(), 0)
    def test_light_waiter_waits_when_another_is_ahead_and_it_is_not_listed(self):
        open(os.path.join(self.home, "visible_light.txt"), "w").write("someone-else\n")
        self.light("1", "[light] other waiting job")
        self.assertIsNone(self.run_light())          # still waiting after 3 s: the dispatcher did not name it
    def test_light_waiter_falls_back_to_first_come_without_a_fresh_list(self):
        self.assertEqual(self.run_light(), 0)

class Level2Tests(Base):
    """main() at macOS pressure level 2 with the process table and memory faked."""
    def job(self, root, rss, cmd="python3 job.py", etime="05:00"):
        return dict(root=root, pids=[root], rss=rss, cmd=cmd, etime=etime)
    def run_main(self, avail, lvl, jobs, paused=()):
        self.signals = []
        P = {j["root"]: dict(pid=j["root"], ppid=1, rss=j["rss"], state="S", etime="05:00", cmd=j["cmd"]) for j in list(jobs) + list(paused)}
        json.dump(list(paused), open(os.path.join(self.home, "paused.json"), "w"))
        self.mg.procs = lambda: P
        self.mg.mem = lambda: (avail, lvl)
        self.mg.session_jobs = lambda P_: list(jobs) + list(paused)
        self.mg.sig = lambda job, s: self.signals.append((job["root"], s))
        self.mg.main()
        return json.load(open(os.path.join(self.home, "paused.json")))
    def test_one_job_keeps_running_at_level_2_with_memory_to_spare(self):
        state = self.run_main(8 * GB, 2, [self.job(10, 2 * GB)])
        self.assertEqual(state, []); self.assertEqual(self.signals, [])
    def test_level_2_pauses_when_close_to_the_pause_line(self):
        state = self.run_main(6.5 * GB, 2, [self.job(10, 2 * GB)])
        self.assertEqual([j["root"] for j in state], [10])
    def test_level_2_with_two_running_jobs_pauses_the_biggest(self):
        state = self.run_main(9 * GB, 2, [self.job(10, 2 * GB), self.job(11, 3 * GB)])
        self.assertEqual([j["root"] for j in state], [11])
    def test_level_2_pauses_below_pause_line_regardless(self):
        state = self.run_main(5 * GB, 1, [self.job(10, 2 * GB)])
        self.assertEqual([j["root"] for j in state], [10])
    def test_option_off_restores_pause_on_any_level_2(self):
        self.mg.CFG["level2_one_job"] = False
        state = self.run_main(12 * GB, 2, [self.job(10, 2 * GB)])
        self.assertEqual([j["root"] for j in state], [10])
    def test_deadlock_breaker_resumes_smallest_paused_job(self):
        import signal
        state = self.run_main(9 * GB, 2, [], paused=[self.job(10, 3 * GB), self.job(11, 1 * GB)])
        self.assertEqual([j["root"] for j in state], [10])
        self.assertEqual(self.signals, [(11, signal.SIGCONT)])
    def test_deadlock_breaker_needs_the_extra_margin(self):
        state = self.run_main(7.5 * GB, 2, [], paused=[self.job(10, 3 * GB)])
        self.assertEqual([j["root"] for j in state], [10]); self.assertEqual(self.signals, [])
    def test_deadlock_breaker_waits_while_a_job_is_running(self):
        state = self.run_main(9 * GB, 2, [self.job(20, 2 * GB)], paused=[self.job(10, 3 * GB)])
        self.assertEqual([j["root"] for j in state], [10]); self.assertEqual(self.signals, [])

class StuckRuleTests(Base):
    def test_priority_label_is_stopped_last_and_pattern_is_configurable(self):
        import signal
        self.mg.PRIORITY_RE = self.mg.re.compile("release", self.mg.re.I)
        paused = [dict(root=10, pids=[10], rss=1 * GB, cmd="a", etime="01:00"), dict(root=11, pids=[11], rss=3 * GB, cmd="b", etime="01:00")]
        P = {10: dict(pid=10, ppid=100, rss=GB, state="S", etime="01:00", cmd="a"), 11: dict(pid=11, ppid=101, rss=3 * GB, state="S", etime="01:00", cmd="b"),
             100: dict(pid=100, ppid=1, rss=1, state="S", etime="01:00", cmd="/bin/bash /x/heavyrun release: ship"),
             101: dict(pid=101, ppid=1, rss=1, state="S", etime="01:00", cmd="/bin/bash /x/heavyrun docs: render")}
        json.dump(paused, open(os.path.join(self.home, "paused.json"), "w"))
        json.dump({"since": time.time() - 3600}, open(os.path.join(self.home, "stuck.json"), "w"))
        sent = []
        self.mg.procs = lambda: P; self.mg.mem = lambda: (4 * GB, 1); self.mg.session_jobs = lambda P_: []
        self.mg.sig = lambda job, s: sent.append((job["root"], s))
        with mock.patch.object(self.mg.time, "sleep"): self.mg.main()
        self.assertIn((11, signal.SIGTERM), sent)         # the bigger, non-priority job goes first even though 10 is smaller
        self.assertNotIn((10, signal.SIGTERM), sent)

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
