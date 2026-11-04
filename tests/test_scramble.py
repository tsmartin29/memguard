"""SCRAMBLE modes (owner, Nov 4, 2026). Run: python3 -m unittest discover -s tests
Everything in a temp state dir with a faked free-disk figure; never touches the real scramble.json."""
import contextlib, io, json, os, shutil, sys, tempfile, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import scramble as S

class Modes(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); os.environ["SCRAMBLE_STATE_DIR"] = self.d; os.environ["SCRAMBLE_FREE_GB"] = "50"
    def tearDown(self):
        shutil.rmtree(self.d); os.environ.pop("SCRAMBLE_STATE_DIR", None); os.environ.pop("SCRAMBLE_FREE_GB", None)
    def run_cli(self, *a):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): rc = S.main(["scramble"] + list(a))
        return rc, buf.getvalue()
    def state(self):
        with open(os.path.join(self.d, "scramble.json")) as fh: return json.load(fh)
    def status_json(self): return json.loads(self.run_cli("status")[1].strip().splitlines()[-1])

    def test_counts(self):
        for cmd, mode, n in [("cap", "cap", 12), ("on", "scramble", 20), ("surge", "surge", 25), ("cas", "cas", 30), ("warthog", "cas", 30)]:
            rc, _ = self.run_cli(cmd); st = self.state()
            self.assertEqual((rc, st["on"], st["mode"], st["target_agents"]), (0, True, mode, n))
    def test_on_still_means_20_and_keeps_duration_and_reason(self):
        self.run_cli("on", "2h", "because"); st = self.state()
        self.assertEqual(st["target_agents"], 20); self.assertEqual(st["reason"], "because")
        self.assertAlmostEqual(st["until"] - st["since"], 7200, delta=2)
    def test_default_duration_8h(self):
        self.run_cli("cap"); st = self.state(); self.assertAlmostEqual(st["until"] - st["since"], 8 * 3600, delta=2)
    def test_set_custom_range(self):
        for n in (1, 14, 20, 40):
            self.assertEqual(self.run_cli("set", str(n))[0], 0); st = self.state()
            self.assertEqual((st["mode"], st["target_agents"]), ("custom", n))
    def test_set_rejects_out_of_range_and_junk(self):
        self.run_cli("cap")
        for bad in ("0", "41", "-3", "abc", "2.5", "", "1000"):
            rc, out = self.run_cli("set", bad); self.assertEqual(rc, 2, bad); self.assertIn("usage", out)
        self.assertEqual(self.run_cli("set")[0], 2)
        self.assertEqual(self.state()["target_agents"], 12)   # untouched by the bad calls
    def test_off(self):
        self.run_cli("surge"); self.run_cli("off"); self.assertFalse(self.state()["on"])
        self.assertFalse(self.status_json()["scramble"])
    def test_status_json_fields(self):
        self.run_cli("set", "17", "3h"); j = self.status_json()
        self.assertEqual((j["scramble"], j["target_agents"], j["mode"], j["disk_low"], j["disk_free_gb"]), (True, 17, "custom", False, 50.0))
        self.assertGreater(j["scramble_left_min"], 170)
    def test_warthog_state_file_reads_as_cas(self):   # a state written under the first name still resolves
        S.save({"on": True, "mode": "warthog", "since": 1, "until": 9e12, "target_agents": 30}); self.assertEqual(self.status_json()["mode"], "cas")
    def test_legacy_state_without_mode(self):   # the file written before Nov 4 has no "mode"
        for n, mode in [(20, "scramble"), (12, "cap"), (25, "surge"), (30, "cas"), (7, "custom")]:
            S.save({"on": True, "since": 1, "until": 9e12, "target_agents": n}); self.assertEqual(self.status_json()["mode"], mode)

class DiskSafety(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); os.environ["SCRAMBLE_STATE_DIR"] = self.d
    def tearDown(self):
        shutil.rmtree(self.d); os.environ.pop("SCRAMBLE_STATE_DIR", None); os.environ.pop("SCRAMBLE_FREE_GB", None)
    def run_cli(self, *a):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): rc = S.main(["scramble"] + list(a))
        return rc, buf.getvalue()
    def test_status_warns_under_10gb(self):
        os.environ["SCRAMBLE_FREE_GB"] = "4.2"; rc, out = self.run_cli("status")
        self.assertIn("WARNING: disk low", out); self.assertTrue(json.loads(out.strip().splitlines()[-1])["disk_low"])
    def test_status_quiet_at_10gb_or_more(self):
        os.environ["SCRAMBLE_FREE_GB"] = "10"; rc, out = self.run_cli("status")
        self.assertNotIn("WARNING", out); self.assertFalse(json.loads(out.strip().splitlines()[-1])["disk_low"])
    def test_surge_and_custom_above_20_refuse_when_low(self):
        os.environ["SCRAMBLE_FREE_GB"] = "9.9"
        for args in (("surge",), ("cas",), ("warthog",), ("set", "21"), ("set", "40")):
            rc, out = self.run_cli(*args); self.assertEqual(rc, 1, args); self.assertIn("REFUSED", out); self.assertIn("9.9 GB", out)
        self.assertFalse(S.load().get("on"))
    def test_refusal_does_not_change_a_running_state(self):
        os.environ["SCRAMBLE_FREE_GB"] = "50"; self.run_cli("cap")
        os.environ["SCRAMBLE_FREE_GB"] = "3"; self.assertEqual(self.run_cli("surge")[0], 1)
        st = S.load(); self.assertEqual((st["on"], st["target_agents"]), (True, 12))
    def test_cap_scramble_and_custom_up_to_20_allowed_when_low(self):
        os.environ["SCRAMBLE_FREE_GB"] = "3"
        for args, n in ((("cap",), 12), (("on",), 20), (("set", "20"), 20), (("set", "5"), 5)):
            rc, out = self.run_cli(*args); self.assertEqual(rc, 0, args); self.assertEqual(S.load()["target_agents"], n)
            self.assertIn("WARNING: disk low", out)
    def test_above_20_allowed_at_exactly_10gb(self):
        os.environ["SCRAMBLE_FREE_GB"] = "10"; self.assertEqual(self.run_cli("surge")[0], 0)

class RealCli(unittest.TestCase):
    def test_script_runs_with_isolated_state(self):
        import subprocess
        d = tempfile.mkdtemp()
        try:
            env = dict(os.environ, SCRAMBLE_STATE_DIR=d, SCRAMBLE_FREE_GB="3")
            r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "scramble.py"), "surge"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1); self.assertIn("REFUSED", r.stdout)
            r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "scramble.py"), "cap"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)
        finally: shutil.rmtree(d)

if __name__ == "__main__": unittest.main()
