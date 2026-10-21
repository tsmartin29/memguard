"""Tests for the timing lane: quiet window in memguard.py, host baseline probe, heavyrun HEAVY_TIMING=1.
Run: python3 -m unittest discover -s tests -v"""
import json, os, re, subprocess, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_memguard import Base, ROOT, GB

PROBE = os.path.join(ROOT, "timing_probe.py")


class TimingGateTests(Base):
    """timing_gate(): which tickets may start while a timing job waits or runs."""
    def slot(self, name, label, pid=None):
        p = os.path.join(self.home, name); os.makedirs(p)
        open(os.path.join(p, "pid"), "w").write(str(pid or os.getpid())); open(os.path.join(p, "label"), "w").write(label)
    def gate(self, labels, free=3):
        T = {f"t{i}": ("q", l) for i, l in enumerate(labels)}
        return self.mg.timing_gate(list(T), T, free)
    def hold_file(self): return os.path.join(self.home, "timing_hold")

    def test_no_timing_ticket_changes_nothing(self):
        self.assertEqual(self.gate(["a", "b", "c"], free=2), ["t0", "t1"])
        self.assertEqual(self.mg.TIMING_HOLD, ""); self.assertFalse(os.path.exists(self.hold_file()))

    def test_timing_next_waits_for_running_jobs_and_holds_others(self):
        self.slot("heavy.slot2", "other session: eval")
        self.assertEqual(self.gate(["[timing] bench", "b"]), [])
        self.assertTrue(self.mg.TIMING_HOLD); self.assertTrue(os.path.exists(self.hold_file()))

    def test_priority_label_is_never_held(self):
        self.slot("heavy.lock", "priority: long training run")
        self.assertEqual(self.gate(["priority: long training run", "[timing] bench", "b"]), ["t0", "t1"])

    def test_priority_pattern_is_configurable(self):
        self.mg.PRIORITY_RE = self.mg.re.compile("urgent", self.mg.re.I)
        self.slot("heavy.lock", "urgent: release build")
        self.assertEqual(self.gate(["[timing] bench"]), ["t0"])      # a running "urgent" job does not block the window
        self.slot("heavy.slot2", "priority: not special any more")
        self.assertEqual(self.gate(["[timing] bench"]), [])           # but a "priority" label is now an ordinary job

    def test_quiet_host_starts_timing_job_then_holds_while_it_runs(self):
        self.assertEqual(self.gate(["[timing] bench", "b"]), ["t0"])
        self.slot("heavy.lock", "[timing] bench")
        self.assertEqual(self.gate(["b", "priority: x"]), ["t1"])      # only priority tickets may start
        os.rename(os.path.join(self.home, "heavy.lock"), os.path.join(self.home, "gone"))
        self.assertEqual(self.gate(["b"]), ["t0"]); self.assertEqual(self.mg.TIMING_HOLD, "")
        self.assertFalse(os.path.exists(self.hold_file()))
        self.assertIn("TIMING window closed", open(self.mg.LOG).read())

    def test_max_wait_in_the_label_releases_the_timing_job(self):
        self.slot("light.lock", "[light] other")
        self.assertEqual(self.gate(["[timing max-wait 5m] bench"]), [])
        f = os.path.join(self.home, "timing_window.json"); st = json.load(open(f)); st["since"] -= 301
        json.dump(st, open(f, "w"))
        self.assertEqual(self.gate(["[timing max-wait 5m] bench"]), ["t0"])
        self.assertIn("max wait 5 min reached", open(self.mg.LOG).read())

    def test_default_max_wait_comes_from_config(self):
        self.mg.CFG["timing_max_wait_minutes"] = 2
        self.slot("heavy.lock", "other: job")
        self.assertEqual(self.gate(["[timing] bench"]), [])
        f = os.path.join(self.home, "timing_window.json"); st = json.load(open(f)); st["since"] -= 121
        json.dump(st, open(f, "w"))
        self.assertEqual(self.gate(["[timing] bench"]), ["t0"])
        self.assertIn("max wait 2 min reached", open(self.mg.LOG).read())

    def test_not_released_before_the_max_wait(self):
        self.slot("heavy.lock", "other: job")
        self.gate(["[timing] bench"])
        self.assertEqual(self.gate(["[timing] bench"]), [])

    def test_tickets_behind_a_timing_ticket_wait_for_it(self):
        self.assertEqual(self.gate(["a", "[timing] bench", "c"], free=3), ["t0"])

    def test_dead_slot_holder_does_not_count(self):
        self.slot("heavy.slot2", "gone job", pid=999999)
        self.assertEqual(self.gate(["[timing] bench"]), ["t0"])

    def test_free_slots_still_limit_the_start(self):
        self.assertEqual(self.gate(["[timing] bench", "b"], free=0), [])

    def test_dry_run_writes_no_state(self):
        self.slot("heavy.lock", "other: job"); self.mg.DRY = True
        try: self.assertEqual(self.gate(["[timing] bench"]), [])
        finally: self.mg.DRY = False
        for f in ("timing_hold", "timing_window.json"): self.assertFalse(os.path.exists(os.path.join(self.home, f)))


class TimingDispatchTests(Base):
    def light(self, name, label):
        p = subprocess.Popen(["sleep", "60"]); self.sleepers.append(p)
        f = f"{name}-{p.pid}"
        os.makedirs(os.path.join(self.home, "queue_light"), exist_ok=True)
        open(os.path.join(self.home, "queue_light", f), "w").write(label)
        return f

    def test_heavy_dispatch_holds_new_starts_behind_a_timing_ticket(self):
        t = self.ticket("1000", "[timing] bench"); b = self.ticket("2000", "b: job")
        self.avail = 15 * GB
        self.mg.dispatch()
        self.assertEqual(self.visible(), [t])
        self.assertTrue(os.path.exists(os.path.join(self.home, "queue_hold", b)))

    def test_light_lane_is_held_during_the_window_except_priority(self):
        slot = os.path.join(self.home, "heavy.lock"); os.makedirs(slot)
        open(os.path.join(slot, "pid"), "w").write(str(os.getpid())); open(os.path.join(slot, "label"), "w").write("[timing] bench")
        self.mg.dispatch()
        self.assertTrue(self.mg.TIMING_HOLD)
        self.light("100", "[light] x"); p = self.light("101", "[light] priority: y")
        self.mg.dispatch("queue_light", "queue_light_hold", heavy_lane=False)
        self.assertEqual(open(os.path.join(self.home, "visible_light.txt")).read().split(), [p])

    def test_light_lane_normal_without_a_window(self):
        a = self.light("100", "[light] x"); self.light("101", "[light] y")
        self.mg.dispatch("queue_light", "queue_light_hold", heavy_lane=False)
        self.assertEqual(open(os.path.join(self.home, "visible_light.txt")).read().split(), [a])

    def test_stall_reason_names_the_quiet_window(self):
        now = time.time_ns()
        slot = os.path.join(self.home, "heavy.lock"); os.makedirs(slot)
        open(os.path.join(slot, "pid"), "w").write(str(os.getpid())); open(os.path.join(slot, "label"), "w").write("other: running job")
        self.ticket(str(now - 60 * 60 * 10**9), "[timing] waits an hour")
        self.avail = 15 * GB
        self.mg.dispatch()
        self.assertIn("timing quiet window", open(os.path.join(self.home, "stalled.txt")).read())


class TicketTimeTests(Base):
    def test_seconds_milliseconds_and_nanoseconds(self):
        t = 1_790_000_000
        self.assertAlmostEqual(self.mg.ticket_time(f"{t}-5"), t)
        self.assertAlmostEqual(self.mg.ticket_time(f"{t * 1000}-5"), t, places=2)
        self.assertAlmostEqual(self.mg.ticket_time(f"{t * 10**9}-5"), t, places=2)
        self.assertAlmostEqual(self.mg.ticket_time(f"0-{t * 1000}-5"), t, places=2)   # HEAVY_PRIORITY ticket

    def test_millisecond_tickets_are_not_reported_as_stalled(self):
        # heavyrun names tickets with a millisecond timestamp; a ticket that just arrived must not raise a stall alert
        self.ticket(str(int(time.time() * 1000)), "first: runs"); b = self.ticket(str(int(time.time() * 1000) + 1), "second: waits")
        self.mg.dispatch()
        self.assertFalse(os.path.exists(os.path.join(self.home, "stalled.txt")))


class ProbeTests(unittest.TestCase):
    def run_probe(self, home, *extra, env=None):
        r = subprocess.run(["/usr/bin/python3", "-I", PROBE, "--secs", "1", "--dir", home, *extra],
                           capture_output=True, text=True, timeout=60, env=dict(os.environ, **(env or {})))
        self.assertEqual(r.returncode, 0, r.stderr)
        kv = dict(l.split("=", 1) for l in r.stdout.splitlines() if re.match(r"^HOST_[A-Z0-9_]+=", l))
        return kv, r.stdout
    def setUp(self): self.home = tempfile.mkdtemp(prefix="memguard-probe-")
    def tearDown(self):
        import shutil; shutil.rmtree(self.home, ignore_errors=True)
    def slot(self, name, label):
        p = os.path.join(self.home, name); os.makedirs(p)
        open(os.path.join(p, "pid"), "w").write(str(os.getpid())); open(os.path.join(p, "label"), "w").write(label)

    def test_exports_the_documented_numbers(self):
        kv, out = self.run_probe(self.home)
        for k in ("HOST_BASELINE_JITTER_P99_MS", "HOST_BASELINE_JITTER_P50_MS", "HOST_TIMER_SLACK_P99_MS", "HOST_TIMER_SLACK_P50_MS",
                  "HOST_LOADAVG_1M", "HOST_OTHER_JOBS", "HOST_PRIORITY_RUNNING", "HOST_BASELINE_BUSY"):
            self.assertIn(k, kv); float(kv[k])
        self.assertLessEqual(float(kv["HOST_BASELINE_JITTER_P50_MS"]), float(kv["HOST_BASELINE_JITTER_P99_MS"]))
        self.assertEqual(kv["HOST_OTHER_JOBS"], "0"); self.assertEqual(kv["HOST_PRIORITY_RUNNING"], "0")
        self.assertIn("SUMMARY=", out)

    def test_counts_other_jobs_but_not_itself(self):
        self.slot("heavy.lock", "other: job"); self.slot("light.lock", "[light] mine")
        kv, out = self.run_probe(self.home, "--self-pid", str(os.getpid()))
        self.assertEqual(kv["HOST_OTHER_JOBS"], "0")           # both slots are held by this very process = "self"
        self.assertIn("other jobs 0", out)

    def test_priority_job_running_marks_the_host_busy(self):
        p = subprocess.Popen(["sleep", "30"])
        try:
            d = os.path.join(self.home, "heavy.lock"); os.makedirs(d)
            open(os.path.join(d, "pid"), "w").write(str(p.pid)); open(os.path.join(d, "label"), "w").write("priority: training")
            kv, out = self.run_probe(self.home)
            self.assertEqual(kv["HOST_PRIORITY_RUNNING"], "1"); self.assertEqual(kv["HOST_BASELINE_BUSY"], "1")
            self.assertIn("INCONCLUSIVE", out)
        finally: p.kill(); p.wait()

    def test_priority_pattern_comes_from_the_config(self):
        p = subprocess.Popen(["sleep", "30"])
        try:
            d = os.path.join(self.home, "heavy.lock"); os.makedirs(d)
            open(os.path.join(d, "pid"), "w").write(str(p.pid)); open(os.path.join(d, "label"), "w").write("priority: training")
            json.dump({"priority_pattern": "urgent"}, open(os.path.join(self.home, "config.json"), "w"))
            kv, _ = self.run_probe(self.home)
            self.assertEqual(kv["HOST_PRIORITY_RUNNING"], "0"); self.assertEqual(kv["HOST_OTHER_JOBS"], "1")
        finally: p.kill(); p.wait()


class HeavyrunTimingTests(Base):
    def env(self, **extra):
        return dict(os.environ, MEMGUARD_HOME=self.home, HEAVY_MIN_FREE="0", HEAVYRUN_POLL="1", HEAVY_TIMING_PROBE_SECS="1", **extra)

    def test_timing_job_gets_host_numbers_and_logs_two_baselines(self):
        r = subprocess.run([os.path.join(ROOT, "heavyrun"), "bench: loop", "sh", "-c", "env | grep '^HOST_' | sort"],
                           env=self.env(HEAVY_TIMING="1"), capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        for k in ("HOST_BASELINE_JITTER_P99_MS", "HOST_BASELINE_JITTER_P50_MS", "HOST_TIMER_SLACK_P99_MS", "HOST_LOADAVG_1M",
                  "HOST_OTHER_JOBS", "HOST_PRIORITY_RUNNING", "HOST_BASELINE_BUSY"):
            self.assertRegex(r.stdout, rf"(?m)^{k}=-?[\d.]+$")
        log = open(os.path.join(self.home, "heavy_queue.log")).read()
        self.assertIn("QUEUED  [timing] bench: loop", log)
        self.assertIn("BASELINE-start [timing] bench: loop", log); self.assertIn("BASELINE-end [timing] bench: loop", log)
        self.assertLess(log.index("BASELINE-start"), log.index("DONE"));  self.assertLess(log.index("DONE"), log.index("BASELINE-end"))

    def test_max_wait_goes_into_the_label(self):
        subprocess.run([os.path.join(ROOT, "heavyrun"), "bench: x", "true"], env=self.env(HEAVY_TIMING="1", HEAVY_TIMING_MAX_WAIT="7"),
                       capture_output=True, timeout=120)
        self.assertIn("[timing max-wait 7m] bench: x", open(os.path.join(self.home, "heavy_queue.log")).read())

    def test_timing_wins_over_light_and_uses_the_heavy_slot(self):
        r = subprocess.run([os.path.join(ROOT, "heavyrun"), "bench: y", "true"], env=self.env(HEAVY_TIMING="1", HEAVY_LIGHT="1"),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        log = open(os.path.join(self.home, "heavy_queue.log")).read()
        self.assertIn("heavy.lock", log); self.assertNotIn("[light]", log)

    def test_exit_code_is_passed_through_and_end_baseline_still_runs(self):
        r = subprocess.run([os.path.join(ROOT, "heavyrun"), "bench: fail", "sh", "-c", "exit 5"], env=self.env(HEAVY_TIMING="1"),
                           capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 5)
        self.assertIn("BASELINE-end", open(os.path.join(self.home, "heavy_queue.log")).read())

    def test_a_normal_job_gets_no_host_numbers(self):
        r = subprocess.run([os.path.join(ROOT, "heavyrun"), "plain: job", "sh", "-c", "env | grep -c '^HOST_' || true"],
                           env=self.env(), capture_output=True, text=True, timeout=120)
        self.assertEqual(r.stdout.strip(), "0")
        self.assertNotIn("BASELINE", open(os.path.join(self.home, "heavy_queue.log")).read())

    def run_light(self, label, wait=4):
        p = subprocess.Popen([os.path.join(ROOT, "heavyrun"), label, "true"], env=self.env(HEAVY_LIGHT="1", LIGHT_MIN_FREE="0"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sleepers.append(p)
        try: p.wait(timeout=wait)
        except subprocess.TimeoutExpired: pass
        return p.poll()

    def test_light_job_waits_during_a_quiet_window_but_a_priority_one_does_not(self):
        open(os.path.join(self.home, "visible_light.txt"), "w").write("")        # fresh list that names nobody
        open(os.path.join(self.home, "timing_hold"), "w").write("quiet window for [timing] bench\n")   # fresh window
        self.assertIsNone(self.run_light("app: build"))                           # held: no "nobody else waiting" shortcut
        self.sleepers[-1].terminate(); self.sleepers[-1].wait(); time.sleep(0.5)  # the held job leaves the queue
        self.assertEqual(self.run_light("priority: release build"), 0)

    def test_light_job_waits_while_a_timing_ticket_is_queued(self):
        open(os.path.join(self.home, "visible_light.txt"), "w").write("")
        qd = os.path.join(self.home, "queue_hold"); os.makedirs(qd)
        open(os.path.join(qd, "1-1"), "w").write("[timing] bench")
        self.assertIsNone(self.run_light("app: build"))

    def test_symlinked_heavyrun_finds_the_probe(self):
        link = os.path.join(self.home, "bin"); os.makedirs(link)
        os.symlink(os.path.join(ROOT, "heavyrun"), os.path.join(link, "heavyrun"))   # what install.sh does in ~/.local/bin
        r = subprocess.run([os.path.join(link, "heavyrun"), "bench: z", "true"], env=self.env(HEAVY_TIMING="1"),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        log = open(os.path.join(self.home, "heavy_queue.log")).read()
        self.assertIn("BASELINE-start", log); self.assertNotIn("probe FAILED", log)


@unittest.skipUnless(os.environ.get("MEMGUARD_RUN_SELFTEST"), "slow (~40 s): set MEMGUARD_RUN_SELFTEST=1 to run the end-to-end dry run")
class SelftestTests(unittest.TestCase):
    def test_selftest_timing_passes(self):
        r = subprocess.run([os.path.join(ROOT, "heavyrun"), "--selftest-timing"], capture_output=True, text=True, timeout=400)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:])
        self.assertIn("selftest-timing: PASS", r.stdout)


if __name__ == "__main__":
    unittest.main()
