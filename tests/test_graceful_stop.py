"""Stopping a job: SIGTERM, up to stop_grace_seconds for the job to save its work, then SIGKILL. Run: python3 -m unittest discover -s tests"""
import importlib, os, shutil, subprocess, sys, tempfile, time, unittest
os.environ["MEMGUARD_NO_DIALOG"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GB = 1 << 30

class GracefulStop(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="memguard-test-")
        os.environ["MEMGUARD_HOME"] = self.home; os.environ["MEMGUARD_STOP_GRACE"] = "6"
        sys.path.insert(0, ROOT); sys.modules.pop("memguard", None)
        self.mg = importlib.import_module("memguard")
        self.mg.mem = lambda: (8 * GB, 1)            # plenty of memory: the full grace applies
        self.procs = []
    def tearDown(self):
        os.environ.pop("MEMGUARD_STOP_GRACE", None)
        for p in self.procs:
            if p.poll() is None: p.kill(); p.wait()
        shutil.rmtree(self.home, ignore_errors=True)
    def start(self, code):
        p = subprocess.Popen([sys.executable, "-c", code]); self.procs.append(p); time.sleep(1); return p

    def test_job_that_saves_on_term_exits_cleanly(self):
        flag = os.path.join(self.home, "saved")
        p = self.start(f"import signal,time,sys\nsignal.signal(signal.SIGTERM, lambda *a: (time.sleep(2), open({flag!r},'w').write('ok'), sys.exit(0)))\nwhile True: time.sleep(1)")
        how, took = self.mg.stop_job({"pids": [p.pid]})
        self.assertEqual(how, "saved"); self.assertLess(took, 5); self.assertEqual(p.wait(5), 0); self.assertTrue(os.path.exists(flag))

    def test_job_ignoring_term_is_killed_after_grace(self):
        p = self.start("import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(1)")
        how, took = self.mg.stop_job({"pids": [p.pid]})
        self.assertEqual(how, "forced"); self.assertGreaterEqual(took, 5.5); self.assertEqual(p.wait(5), -9)

    def test_short_grace_when_memory_is_already_critical(self):
        self.mg.mem = lambda: (2 * GB, 4)             # under kill_avail_gb: only 3 s
        p = self.start("import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(1)")
        how, took = self.mg.stop_job({"pids": [p.pid]})
        self.assertEqual(how, "forced"); self.assertLess(took, 5); self.assertEqual(p.wait(5), -9)

    def test_dry_run_sends_nothing(self):
        p = self.start("import time\nwhile True: time.sleep(1)")
        self.mg.DRY = True
        try: self.assertEqual(self.mg.stop_job({"pids": [p.pid]})[0], "dry-run")
        finally: self.mg.DRY = False
        self.assertIsNone(p.poll())

if __name__ == "__main__": unittest.main()
