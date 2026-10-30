"""Tests for afterburner mode (afterburner.py, its use in memguard.py and heavyrun). Run: python3 -m unittest discover -s tests
Everything runs in temp dirs with stubbed memory; never touches a real afterburner.json, queue or job."""
import importlib, os, re, shutil, subprocess, sys, tempfile, time, unittest
os.environ["MEMGUARD_NO_DIALOG"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import afterburner as AB
GB = 1 << 30
DAW = "/Applications/Some DAW.app/Contents/MacOS/SomeDAW"
CLI = "/Users/x/.local/share/claude/versions/2.1.0 --resume"

def fresh_memguard(home):
    for k in [k for k in os.environ if k.startswith("HEAVY") or k.startswith("LIGHT_")]: del os.environ[k]
    os.environ["MEMGUARD_HOME"] = home
    sys.modules.pop("memguard", None)
    mg = importlib.import_module("memguard")
    mg.CFG["notify"] = False
    return mg

class Floors(unittest.TestCase):
    def test_afterburner_never_changes_the_memory_floors(self):
        d = tempfile.mkdtemp()
        try:
            mg = fresh_memguard(d)
            before = (mg.PAUSE_AVAIL, mg.KILL_AVAIL, mg.RESUME_AVAIL, mg.SECOND_OK, mg.SECOND_PAUSE, mg.SLOT2_AVAIL, mg.SLOT3_AVAIL, mg.STOP_HARD_AVAIL)
            now = time.time(); AB.save(dict(on=True, since=now, until=now + 3600, reason="t", checked=now), d)
            mg = fresh_memguard(d)
            self.assertEqual(before, (mg.PAUSE_AVAIL, mg.KILL_AVAIL, mg.RESUME_AVAIL, mg.SECOND_OK, mg.SECOND_PAUSE, mg.SLOT2_AVAIL, mg.SLOT3_AVAIL, mg.STOP_HARD_AVAIL))
            self.assertEqual((mg.PAUSE_AVAIL, mg.KILL_AVAIL, mg.RESUME_AVAIL), (6 * GB, 3 * GB, 9 * GB))   # config.example.json defaults
        finally: shutil.rmtree(d)

class MemSlots(unittest.TestCase):
    def setUp(self): self.d = tempfile.mkdtemp(); self.mg = fresh_memguard(self.d)
    def tearDown(self): shutil.rmtree(self.d)
    def test_normal_mode_equals_the_old_tiers(self):
        for tenth in range(0, 250):
            a = tenth * GB / 10
            self.assertEqual(self.mg.mem_slots(a, 3), 1 + (a >= self.mg.SLOT2_AVAIL) + (a >= self.mg.SLOT3_AVAIL))
    def test_afterburner_raises_the_cap_only_as_memory_allows(self):
        f = lambda gb: self.mg.mem_slots(gb * GB, 10)
        # defaults: 2nd at 10 GB, 3rd at 14 GB, then +3 GB each
        self.assertEqual([f(5), f(10), f(14), f(16.9), f(17), f(20), f(1000)], [1, 2, 3, 3, 4, 5, 10])

class Check(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.logs, self.notes = [], []
        self.now = time.time(); self.old_drop = AB.DROP_APPS
        AB.DROP_APPS = re.compile(r"Some DAW|Logic Pro", re.I)
        AB.save(dict(on=True, since=self.now, until=self.now + 4 * 3600, reason="test", drop_app_pids_at_on=[500], checked=self.now), self.d)
    def tearDown(self): AB.DROP_APPS = self.old_drop; shutil.rmtree(self.d)
    def chk(self, lvl=1, cmds=None, now=None, load1=1.0, front="Terminal", batt=(False, 90)):
        return AB.check(lvl, cmds if cmds is not None else {1: CLI, 2: CLI, 3: CLI, 4: CLI}, here=self.d, log=self.logs.append,
                        notify=self.notes.append, now=now or self.now + 15, load1=load1, front=front, batt=batt)
    def st(self): return AB.load(self.d)
    def test_on_gives_raised_caps_and_N(self):
        c = self.chk()
        self.assertEqual(c, dict(AB.ON_CAPS))
        self.assertEqual(self.st()["workers_per_agent"], max(1, (AB.CORES - 2) // 4)); self.assertEqual(self.logs, [])
    def test_N_minimum_one(self):
        self.chk(cmds={i: CLI for i in range(40)}); self.assertEqual(self.st()["workers_per_agent"], 1)
    def assertDropped(self, words):
        s = self.st(); self.assertFalse(s["on"]); self.assertIn(words, s["off_reason"])
        self.assertTrue(any("AFTERBURNER DROPPED" in l and words in l for l in self.logs)); self.assertEqual(len(self.notes), 1)
        self.assertEqual(AB.caps(here=self.d), AB.NORMAL); self.assertEqual(s["workers_per_agent"], 1)
    def test_pressure_warn_drops(self):
        self.assertEqual(self.chk(lvl=2), AB.NORMAL); self.assertDropped("memory pressure WARN")
    def test_pressure_critical_drops(self):
        self.chk(lvl=4); self.assertDropped("CRITICAL")
    def test_load_must_stay_high(self):
        hi, S = AB.LOAD_LIMIT + 1, AB.LOAD_SECS
        self.chk(load1=hi, now=self.now + 15); self.assertTrue(self.st()["on"])
        self.chk(load1=hi, now=self.now + 15 + S - 20); self.assertTrue(self.st()["on"])
        self.chk(load1=1, now=self.now + 15 + S - 5); self.assertNotIn("load_high_since", self.st())   # a dip resets the timer
        t = self.now + 15 + S + 10
        self.chk(load1=hi, now=t); self.chk(load1=hi, now=t + S - 1); self.assertTrue(self.st()["on"])
        self.chk(load1=hi, now=t + S + 1); self.assertDropped("load")
    def test_drop_app_open_at_on_is_fine_but_a_new_one_drops(self):
        self.chk(cmds={500: DAW, 1: CLI}); self.assertTrue(self.st()["on"])
        self.chk(cmds={501: DAW, 1: CLI}); self.assertDropped("started")
    def test_drop_app_frontmost_drops(self):
        self.chk(front="Logic Pro"); self.assertDropped("frontmost")
    def test_no_drop_apps_configured_means_no_app_drops(self):
        AB.DROP_APPS = None
        self.chk(cmds={501: DAW, 1: CLI}, front="Logic Pro"); self.assertTrue(self.st()["on"])
    def test_battery(self):
        self.chk(batt=(False, 10)); self.assertTrue(self.st()["on"])     # on power: no drop
        self.chk(batt=(True, AB.BATT_MIN)); self.assertTrue(self.st()["on"])
        self.chk(batt=(True, AB.BATT_MIN - 1)); self.assertDropped("battery")
    def test_auto_off_when_time_is_up(self):
        self.chk(now=self.now + 4 * 3600 + 1)
        s = self.st(); self.assertFalse(s["on"]); self.assertIn("time up", s["off_reason"]); self.assertTrue(any("AFTERBURNER OFF" in l for l in self.logs))
    def test_stale_guard_means_normal_caps(self):
        s = self.st(); s["checked"] = time.time() - 600; AB.save(s, self.d)
        self.assertEqual(AB.caps(here=self.d), AB.NORMAL)
    def test_caps_cli_for_heavyrun(self):
        self.chk()
        cli = lambda d: subprocess.run(["/usr/bin/python3", "-I", os.path.join(ROOT, "afterburner.py"), "caps", "--dir", d],
                                       capture_output=True, text=True, env=dict(os.environ, MEMGUARD_CONFIG=os.path.join(d, "none.json"))).stdout.split()
        self.assertEqual(cli(self.d), [str(AB.MAX_HEAVY), "2", "10"])
        e = tempfile.mkdtemp()
        self.assertEqual(cli(e), ["3", "1", "15"]); shutil.rmtree(e)
    def test_duration_parse(self):
        self.assertEqual([AB.parse_dur(x) for x in ("4h", "90m", "1h30m", "2", "later")], [14400, 5400, 5400, 7200, None])

class Dispatch(unittest.TestCase):
    """Heavy + light dispatch in a scratch home with 6 heavy and 3 light tickets."""
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.mg = fresh_memguard(self.d)
        self.mg.disk_and_swap = lambda: (99 * GB, 0)
        base = int(time.time() * 1000)
        for lane, n in (("queue", 6), ("queue_light", 3)):
            os.makedirs(os.path.join(self.d, lane))
            for i in range(n): open(os.path.join(self.d, lane, f"{base + i}-{os.getpid()}"), "w").write(f"{'[light] ' if 'light' in lane else ''}job {i}")
    def tearDown(self): shutil.rmtree(self.d)
    def run_both(self, avail_gb):
        self.mg.mem = lambda: (avail_gb * GB, 1)
        self.mg.dispatch(); self.mg.dispatch("queue_light", "queue_light_hold", heavy_lane=False)
        rd = lambda f: open(os.path.join(self.d, f)).read().split()
        return len(rd("visible.txt")), len(rd("visible_light.txt"))
    def ab_on(self):
        now = time.time(); AB.save(dict(on=True, since=now, until=now + 3600, reason="t", checked=now), self.d)
    def test_normal(self):
        self.assertEqual(self.run_both(15), (3, 1)); self.assertEqual(self.run_both(10), (2, 1))
    def test_afterburner(self):
        if AB.MAX_HEAVY < 5: self.skipTest("needs 7+ cores")
        self.ab_on(); self.assertEqual(self.run_both(17), (4, 2)); self.assertEqual(self.run_both(20), (5, 2))
    def test_afterburner_memory_tiers_still_rule(self):
        self.ab_on(); self.assertEqual(self.run_both(10), (2, 2)); self.assertEqual(self.run_both(5), (1, 2))
    def test_running_slot4_counts(self):
        if AB.MAX_HEAVY < 4: self.skipTest("needs 6+ cores")
        self.ab_on(); p = os.path.join(self.d, "heavy.slot4"); os.makedirs(p)
        open(os.path.join(p, "pid"), "w").write(str(os.getpid())); open(os.path.join(p, "label"), "w").write("x")
        self.assertEqual(self.run_both(17)[0], 3)

class Heavyrun(unittest.TestCase):
    def setUp(self): self.d = tempfile.mkdtemp()
    def tearDown(self): shutil.rmtree(self.d)
    def env(self, **x):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("HEAVY", "LIGHT_"))}
        return dict(e, MEMGUARD_HOME=self.d, MEMGUARD_CONFIG=os.path.join(self.d, "config.json"), HEAVY_MIN_FREE="0", HEAVYRUN_POLL="1", **x)
    def test_normal_mode_runs_at_nice_15(self):
        r = subprocess.run([os.path.join(ROOT, "heavyrun"), "t: nice", "sh", "-c", "ps -o nice= -p $$"], env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), str(min(20, os.nice(0) + 15)), r.stderr)   # nice is relative to the test's own niceness
    def test_afterburner_runs_at_its_nice_and_logs_it(self):
        now = time.time(); AB.save(dict(on=True, since=now, until=now + 3600, reason="t", checked=now), self.d)
        r = subprocess.run([os.path.join(ROOT, "heavyrun"), "t: nice", "sh", "-c", "ps -o nice= -p $$"], env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), str(min(20, os.nice(0) + AB.ON_CAPS["nice"])), r.stderr)
        self.assertIn("afterburner nice", open(os.path.join(self.d, "heavy_queue.log")).read())

if __name__ == "__main__": unittest.main()
