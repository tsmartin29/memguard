"""Tests for the usage-limit prompt (usagegate.py, usage_statusline.py, the dispatcher and heavyrun holds).
Run: python3 -m unittest discover -s tests

Never shows a dialog: MEMGUARD_NO_DIALOG=1 is set for the whole run, osascript is never started (one test proves it with a stub that
fails if it is called), and nothing touches your real memguard folder (everything lives in temp dirs)."""
import json, os, shutil, stat, subprocess, sys, tempfile, time, unittest

os.environ["MEMGUARD_NO_DIALOG"] = "1"   # before anything else can import or run usagegate
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_memguard import Base, ROOT, GB
import usagegate as ug

GATE = os.path.join(ROOT, "usagegate.py")
HOOK = os.path.join(ROOT, "usage_statusline.py")
RESET = "2026-10-22T12:00:00Z"
T0 = ug.parse_iso("2026-10-21T17:50:00Z")
def U(wk, spent=1700.0, reset="2026-10-22T11:59:59.979Z", en=True): return {"weekly_pct": wk, "weekly_resets_at": reset, "extra_spent": spent, "extra_enabled": en}
def future(secs=3600): return ug.iso(time.time() + secs)


class Evaluate(unittest.TestCase):
    def test_below_threshold_nothing_but_baseline_recorded(self):
        t, st, _ = ug.evaluate(U(80), {}, T0); self.assertIsNone(t); self.assertEqual(st["last_extra_spent"], 1700.0)
    def test_95_fires_once_per_window(self):
        t, st, _ = ug.evaluate(U(95), {}, T0); self.assertEqual(t, "95")
        self.assertIsNone(ug.evaluate(U(96.5), st, T0)[0])
    def test_threshold_is_configurable(self):
        self.assertIsNone(ug.evaluate(U(85), {}, T0)[0]); self.assertEqual(ug.evaluate(U(85), {}, T0, thresh=80)[0], "95")
    def test_100_is_extra_and_uses_both_triggers(self):
        t, st, _ = ug.evaluate(U(100), {}, T0); self.assertEqual(t, "extra"); self.assertTrue(st["fired_95"] and st["fired_extra"])
        self.assertIsNone(ug.evaluate(U(100), st, T0)[0])
    def test_95_then_100_asks_twice(self):
        t, st, _ = ug.evaluate(U(96), {}, T0); self.assertEqual(t, "95")
        self.assertEqual(ug.evaluate(U(100), st, T0)[0], "extra")
    def test_spend_rise_below_100_is_extra_start_and_needs_a_baseline(self):
        t, st, _ = ug.evaluate(U(40, 10.0), {}, T0); self.assertIsNone(t)
        t, st, rf = ug.evaluate(U(40, 12.5), st, T0); self.assertEqual((t, rf), ("extra", 10.0))
        self.assertIsNone(ug.evaluate(U(40, 20.0), st, T0)[0])
    def test_no_spend_figure_still_triggers_on_weekly_percent(self):
        u = U(100); del u["extra_spent"]
        self.assertEqual(ug.evaluate(u, {}, T0)[0], "extra")
    def test_new_window_fires_again_and_keeps_spend_baseline(self):
        _, st, _ = ug.evaluate(U(100), {}, T0)
        t, st2, _ = ug.evaluate(U(96, 1700.0, "2026-10-29T12:00:00Z"), st, ug.parse_iso("2026-10-28T12:00:00Z"))
        self.assertEqual(t, "95"); self.assertEqual(st2["last_extra_spent"], 1700.0)
    def test_ended_window_is_ignored(self):
        self.assertIsNone(ug.evaluate(U(100), {}, ug.parse_iso("2026-10-22T12:00:01Z"))[0])
    def test_missing_reset_or_percent_is_ignored(self):
        self.assertIsNone(ug.evaluate({"weekly_pct": 99}, {}, T0)[0])
        self.assertIsNone(ug.evaluate({"weekly_resets_at": RESET}, {}, T0)[0])
    def test_reset_jitter_is_one_window(self):
        _, st, _ = ug.evaluate(U(50, reset="2026-10-22T11:59:59.979Z"), {}, T0)
        self.assertEqual(ug.window_key(ug.parse_iso("2026-10-22T12:00:00.310Z")), st["window"])

    def test_reset_time_is_rounded_to_the_window_not_shown_as_6_59(self):
        os.environ["TZ"] = "UTC"; time.tzset()
        try: self.assertIn("12:00 PM", ug.prompt_text("95", U(96)))   # reset 11:59:59.979Z
        finally: del os.environ["TZ"]; time.tzset()
    def test_texts_have_percent_reset_and_spend_in_local_time(self):
        old = os.environ.get("TZ"); os.environ["TZ"] = "America/Chicago"; time.tzset()
        try:
            for trig, wk in (("95", 95.4), ("extra", 100)):
                s = ug.prompt_text(trig, dict(U(wk), weekly_resets_at=RESET, extra_spent=1723.71))
                self.assertIn("Thursday 7:00 AM CDT", s); self.assertIn("$1,723.71", s); self.assertIn("%", s)
                self.assertIn("10 minutes", s)
        finally:
            if old is None: del os.environ["TZ"]
            else: os.environ["TZ"] = old
            time.tzset()
        self.assertIn("Answer on the Mac.", ug.push_text(s))
    def test_text_names_the_three_choices_and_no_business_words(self):
        s = ug.prompt_text("95", U(96))
        for w in ("Keep going on credits", "Pause new agent work", "Pause heavy jobs only"): self.assertIn(w, s)
        for w in ("C" + "OO", "Chi" + "cago"): self.assertNotIn(w, s)   # nothing business-specific in the text
    def test_extra_off_text_says_claude_will_stop(self):
        self.assertIn("Extra usage is switched off", ug.prompt_text("extra", U(100, en=False)))


class Gate(unittest.TestCase):
    def setUp(self): self.d = tempfile.mkdtemp()
    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)
    def put(self, ch, reset=RESET): json.dump({"choice": ch, "time": "t", "window_reset": reset}, open(os.path.join(self.d, "usage_decision.json"), "w"))
    def g(self, label, lane="heavy", now=T0): return ug.gate(label, lane, self.d, now)[0]
    def test_pause_all(self):
        self.put("pause_all"); self.assertEqual(self.g("x"), 75); self.assertEqual(self.g("[light] x", "light"), 75)
        self.assertEqual(self.g("priority: long training run"), 0)
    def test_pause_heavy_only_holds_heavy_lane(self):
        self.put("pause_heavy"); self.assertEqual(self.g("x"), 76); self.assertEqual(self.g("[light] x", "light"), 0)
        self.assertEqual(self.g("Priority: y"), 0)
    def test_keep_going_none_and_expiry(self):
        self.assertEqual(self.g("x"), 0); self.put("keep_going"); self.assertEqual(self.g("x"), 0)
        self.put("pause_all"); self.assertEqual(self.g("x", now=ug.parse_iso("2026-10-22T12:00:00Z")), 0)
    def test_priority_pattern_comes_from_config(self):
        self.put("pause_all"); json.dump({"priority_pattern": "urgent"}, open(os.path.join(self.d, "config.json"), "w"))
        self.assertEqual(self.g("urgent: release"), 0); self.assertEqual(self.g("priority: training"), 75)
    def test_empty_priority_pattern_exempts_nothing(self):
        self.put("pause_all"); json.dump({"priority_pattern": ""}, open(os.path.join(self.d, "config.json"), "w"))
        self.assertEqual(self.g("priority: training"), 75)
    def test_messages_are_clear_and_generic(self):
        self.put("pause_all"); msg = ug.gate("x", "heavy", self.d, T0)[1]
        self.assertIn("REFUSED", msg); self.assertIn("usage_decision.json", msg); self.assertNotIn("C" + "OO", msg)
    def test_gate_cli_exit_codes_and_fail_open(self):
        self.put("pause_all", future())
        r = subprocess.run(["/usr/bin/python3", "-I", GATE, "--gate", "x", "--dir", self.d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 75); self.assertIn("REFUSED", r.stdout)
        open(os.path.join(self.d, "usage_decision.json"), "w").write("{broken")
        r = subprocess.run(["/usr/bin/python3", "-I", GATE, "--gate", "x", "--dir", self.d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
    def test_check_moves_expired_decision_aside(self):
        self.put("pause_all"); os.environ["USAGE_NOW"] = "2026-10-22T12:00:01Z"
        try: ug.check(self.d, dry=True)
        finally: del os.environ["USAGE_NOW"]
        self.assertFalse(os.path.exists(os.path.join(self.d, "usage_decision.json")))
        self.assertTrue(os.path.exists(os.path.join(self.d, "usage_decision.last.json")))


class Prompt(unittest.TestCase):
    """run_prompt() with the dialog and the push command faked: the three choices, dismissal, and notify_command."""
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.old = ug.show_dialog
        self.payload = {"trigger": "95", "text": ug.prompt_text("95", U(96)), "window_reset": RESET}
    def tearDown(self): ug.show_dialog = self.old; shutil.rmtree(self.d, ignore_errors=True)
    def decision(self): return json.load(open(os.path.join(self.d, "usage_decision.json")))

    def test_each_button_writes_its_decision_until_the_reset(self):
        for label, want in (("Keep going on credits", "keep_going"), ("Pause new agent work until reset", "pause_all"), ("Pause heavy jobs only", "pause_heavy")):
            ug.show_dialog = lambda text, secs=0, title="": label
            self.assertEqual(ug.run_prompt(self.payload, self.d), want)
            self.assertEqual((self.decision()["choice"], self.decision()["window_reset"]), (want, RESET))
    def test_dismissed_or_timed_out_means_no_action(self):
        ug.show_dialog = lambda *a, **k: None
        self.assertIsNone(ug.run_prompt(self.payload, self.d))
        self.assertFalse(os.path.exists(os.path.join(self.d, "usage_decision.json")))
        self.assertIn("no action", open(os.path.join(self.d, "memguard.log")).read())
    def test_the_dialog_gives_up_after_ten_minutes(self):
        seen = {}
        ug.show_dialog = lambda text, secs=0, title="": seen.setdefault("secs", secs) and None
        ug.run_prompt(self.payload, self.d); self.assertEqual(seen["secs"], 600)
    def test_notify_command_gets_message_title_and_stdin(self):
        out = os.path.join(self.d, "pushed.txt")
        json.dump({"notify_command": f'printf "%s|%s|" "$MEMGUARD_TITLE" "$MEMGUARD_MESSAGE" > {out}; cat >> {out}'}, open(os.path.join(self.d, "config.json"), "w"))
        ug.show_dialog = lambda *a, **k: None
        ug.run_prompt(self.payload, self.d)
        got = open(out).read(); self.assertTrue(got.startswith("Claude usage|Your weekly Claude usage has reached 96%"), got)
        self.assertTrue(got.count("Answer on the Mac.") == 2)    # once from the env var, once from stdin
    def test_no_notify_command_sends_nothing(self):
        ug.show_dialog = lambda *a, **k: None
        ug.run_prompt(self.payload, self.d)      # would raise or hang if it tried to start something
    def test_a_failing_notify_command_does_not_break_the_prompt(self):
        json.dump({"notify_command": "exit 3"}, open(os.path.join(self.d, "config.json"), "w"))
        ug.show_dialog = lambda *a, **k: "Pause heavy jobs only"
        self.assertEqual(ug.run_prompt(self.payload, self.d), "pause_heavy")
    def test_dry_run_prints_and_writes_nothing(self):
        os.environ["USAGE_DRYRUN_CHOICE"] = "all"
        import contextlib, io
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out: self.assertEqual(ug.run_prompt(self.payload, self.d, dry=True), "pause_all")
        finally: del os.environ["USAGE_DRYRUN_CHOICE"]
        self.assertIn("NOTIFY (not sent)", out.getvalue()); self.assertEqual(self.decision()["choice"], "pause_all")


class NeverOnScreen(unittest.TestCase):
    """The dialog (including --test-dialog) must never reach osascript while MEMGUARD_NO_DIALOG=1, which the suite sets."""
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.bin = os.path.join(self.d, "bin"); os.makedirs(self.bin)
        self.flag = os.path.join(self.d, "osascript-was-called")
        stub = os.path.join(self.bin, "osascript")
        open(stub, "w").write(f"#!/bin/sh\ntouch '{self.flag}'\nexit 1\n"); os.chmod(stub, 0o755)
        self.env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], MEMGUARD_NO_DIALOG="1", MEMGUARD_HOME=self.d)
    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)

    def test_the_suite_has_the_guard_on(self): self.assertEqual(os.environ.get("MEMGUARD_NO_DIALOG"), "1")
    def test_show_dialog_returns_none_without_starting_osascript(self):
        old = os.environ["PATH"]; os.environ["PATH"] = self.bin + os.pathsep + old
        try: self.assertIsNone(ug.show_dialog("text"))
        finally: os.environ["PATH"] = old
        self.assertFalse(os.path.exists(self.flag))
    def test_test_dialog_mode_shows_nothing(self):
        r = subprocess.run(["/usr/bin/python3", "-I", GATE, "--test-dialog", "--dir", self.d], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("suppressed", r.stdout); self.assertIn("nothing (dismissed or timed out)", r.stdout)
        self.assertFalse(os.path.exists(self.flag))
        self.assertFalse(os.path.exists(os.path.join(self.d, "usage_decision.json")))
    def test_a_real_trigger_in_a_detached_child_shows_nothing_and_writes_no_decision(self):
        json.dump(U(100, reset=future(86400)), open(os.path.join(self.d, "usage.json"), "w"))
        r = subprocess.run(["/usr/bin/python3", "-I", GATE, "--check", "--dir", self.d], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        for _ in range(100):   # the detached child writes its verdict to the log and removes its lock
            if os.path.exists(os.path.join(self.d, "memguard.log")) and "no action" in open(os.path.join(self.d, "memguard.log")).read(): break
            time.sleep(0.1)
        log = open(os.path.join(self.d, "memguard.log")).read()
        self.assertIn("TRIGGER extra", log); self.assertIn("no action", log)
        self.assertFalse(os.path.exists(self.flag)); self.assertFalse(os.path.exists(os.path.join(self.d, "usage_decision.json")))
    def test_ask_now_cli_shows_nothing(self):
        json.dump(U(97, reset=future(86400)), open(os.path.join(self.d, "usage.json"), "w"))
        subprocess.run(["/usr/bin/python3", "-I", GATE, "--ask-now", "--dir", self.d], capture_output=True, text=True, env=self.env, timeout=60)
        time.sleep(1.5)
        self.assertFalse(os.path.exists(self.flag))


class CheckCycle(unittest.TestCase):
    def setUp(self): self.d = tempfile.mkdtemp()
    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)
    def test_no_usage_file_does_nothing(self):
        self.assertIsNone(ug.check(self.d, dry=True)); self.assertFalse(os.path.exists(os.path.join(self.d, "usage_state.json")))
    def test_dry_check_fires_once_per_window_and_records_state(self):
        json.dump(U(97, reset=future(86400)), open(os.path.join(self.d, "usage.json"), "w"))
        self.assertEqual(ug.check(self.d, dry=True), "95"); self.assertIsNone(ug.check(self.d, dry=True))
        self.assertTrue(json.load(open(os.path.join(self.d, "usage_state.json")))["fired_95"])
    def test_threshold_from_config(self):
        json.dump(U(85, reset=future(86400)), open(os.path.join(self.d, "usage.json"), "w"))
        json.dump({"usage_threshold_pct": 80}, open(os.path.join(self.d, "config.json"), "w"))
        self.assertEqual(ug.check(self.d, dry=True), "95")


class DispatchHold(Base):
    def lane(self, lane, name, label):
        p = subprocess.Popen(["sleep", "60"]); self.sleepers.append(p)
        f = f"{name}-{p.pid}"; os.makedirs(os.path.join(self.home, lane), exist_ok=True)
        open(os.path.join(self.home, lane, f), "w").write(label); return f
    def setUp(self):
        super().setUp(); self.avail = 20 * GB
        self.a, self.p = self.lane("queue", "1000", "docs: render"), self.lane("queue", "1001", "priority: training")
        self.la, self.lp = self.lane("queue_light", "1000", "[light] docs: render"), self.lane("queue_light", "1001", "[light] priority: x")
    def decide(self, ch, reset=None):
        if ch: json.dump({"choice": ch, "time": "t", "window_reset": reset or future()}, open(os.path.join(self.home, "usage_decision.json"), "w"))
        self.mg.dispatch(); self.mg.dispatch("queue_light", "queue_light_hold", heavy_lane=False)
        rd = lambda f: open(os.path.join(self.home, f)).read().split()
        return rd("visible.txt"), rd("visible_light.txt")

    def test_no_decision_normal(self):
        h, l = self.decide(None); self.assertEqual(sorted(h), sorted([self.a, self.p])); self.assertEqual(len(l), 1)
    def test_keep_going_changes_nothing(self):
        h, l = self.decide("keep_going"); self.assertEqual(sorted(h), sorted([self.a, self.p]))
    def test_pause_heavy_only_priority_starts_heavy_light_unchanged(self):
        h, l = self.decide("pause_heavy"); self.assertEqual(h, [self.p]); self.assertEqual(len(l), 1); self.assertEqual(l, [self.la])
        self.assertIn("usage pause", self.mg.USAGE_HOLD)
    def test_pause_all_only_priority_starts_in_both_lanes(self):
        h, l = self.decide("pause_all"); self.assertEqual(h, [self.p]); self.assertEqual(l, [self.lp])
    def test_expired_decision_holds_nothing(self):
        h, l = self.decide("pause_all", reset=ug.iso(time.time() - 5)); self.assertEqual(sorted(h), sorted([self.a, self.p]))
    def test_hold_ends_when_the_decision_goes(self):
        self.decide("pause_heavy"); os.remove(os.path.join(self.home, "usage_decision.json"))
        h, _ = self.decide(None); self.assertEqual(sorted(h), sorted([self.a, self.p])); self.assertEqual(self.mg.USAGE_HOLD, "")
    def test_option_off_ignores_the_decision(self):
        self.mg.CFG["usage_prompt"] = False
        h, l = self.decide("pause_all"); self.assertEqual(sorted(h), sorted([self.a, self.p]))
    def test_stall_alert_names_the_usage_pause(self):
        n = time.time_ns()
        slow = self.lane("queue", str(n - 3600 * 10**9), "docs: waits an hour")
        self.decide("pause_heavy")
        self.assertIn("usage pause", open(os.path.join(self.home, "stalled.txt")).read())


class HeavyrunHold(Base):
    def env(self, **extra): return dict(os.environ, MEMGUARD_HOME=self.home, HEAVY_MIN_FREE="0", LIGHT_MIN_FREE="0", HEAVYRUN_POLL="1", **extra)
    def decide(self, ch, reset=None):
        json.dump({"choice": ch, "time": "t", "window_reset": reset or future()}, open(os.path.join(self.home, "usage_decision.json"), "w"))
    def run_hr(self, label, light=False, wait=6):
        env = self.env(HEAVY_LIGHT="1") if light else self.env()
        p = subprocess.Popen([os.path.join(ROOT, "heavyrun"), label, "true"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.sleepers.append(p)
        try: out, err = p.communicate(timeout=wait)
        except subprocess.TimeoutExpired: return None, "", ""
        return p.returncode, out, err

    def test_no_decision_runs(self): self.assertEqual(self.run_hr("docs: job")[0], 0)
    def test_pause_all_refuses_with_exit_75_and_a_message(self):
        self.decide("pause_all"); rc, _, err = self.run_hr("docs: job")
        self.assertEqual(rc, 75); self.assertIn("REFUSED", err); self.assertIn("usage_decision.json", err)
        self.assertEqual(os.listdir(os.path.join(self.home, "queue")), [])      # no ticket left behind
    def test_pause_all_refuses_the_light_lane_too(self):
        self.decide("pause_all"); self.assertEqual(self.run_hr("docs: job", light=True)[0], 75)
    def test_priority_label_is_exempt(self):
        self.decide("pause_all"); self.assertEqual(self.run_hr("priority: training")[0], 0)
    def test_pause_heavy_holds_the_heavy_lane_but_not_the_light_lane(self):
        self.decide("pause_heavy")
        self.assertIsNone(self.run_hr("docs: job", wait=4)[0])                    # still waiting
        self.sleepers[-1].terminate(); self.sleepers[-1].wait(); time.sleep(0.5)   # the held job leaves the queue
        self.assertEqual(self.run_hr("docs: quick", light=True)[0], 0)
        self.assertEqual(self.run_hr("priority: training")[0], 0)
    def test_a_held_job_starts_when_the_decision_is_removed(self):
        self.decide("pause_heavy")
        p = subprocess.Popen([os.path.join(ROOT, "heavyrun"), "docs: job", "true"], env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sleepers.append(p)
        with self.assertRaises(subprocess.TimeoutExpired): p.wait(timeout=3)
        os.remove(os.path.join(self.home, "usage_decision.json"))
        self.assertEqual(p.wait(timeout=30), 0)
        log = open(os.path.join(self.home, "heavy_queue.log")).read(); self.assertIn("HELD", log); self.assertIn("UNHELD", log)
    def test_expired_decision_runs(self): self.decide("pause_all", reset=ug.iso(time.time() - 5)); self.assertEqual(self.run_hr("docs: job")[0], 0)
    def test_symlinked_heavyrun_finds_the_gate(self):
        link = os.path.join(self.home, "bin"); os.makedirs(link); os.symlink(os.path.join(ROOT, "heavyrun"), os.path.join(link, "heavyrun"))
        self.decide("pause_all")
        r = subprocess.run([os.path.join(link, "heavyrun"), "docs: job", "true"], env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 75)


class Statusline(unittest.TestCase):
    def setUp(self): self.d = tempfile.mkdtemp()
    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)
    def run_hook(self, payload, **env):
        return subprocess.run(["/usr/bin/python3", "-I", HOOK], input=payload if isinstance(payload, str) else json.dumps(payload),
                              text=True, capture_output=True, env=dict(os.environ, MEMGUARD_USAGE_DIR=self.d, **env))
    RL = {"rate_limits": {"five_hour": {"used_percentage": 3, "resets_at": 1792600000}, "seven_day": {"used_percentage": 96.1, "resets_at": 1792670400}}}

    def test_hook_writes_usage_json_and_keeps_extra_fields(self):
        json.dump({"extra_spent": 5.0, "extra_enabled": True, "currency": "EUR"}, open(os.path.join(self.d, "usage.json"), "w"))
        r = self.run_hook(self.RL); self.assertEqual((r.returncode, r.stdout), (0, ""))
        u = json.load(open(os.path.join(self.d, "usage.json")))
        self.assertEqual((u["weekly_pct"], u["extra_spent"], u["currency"], u["source"], u["five_hour_pct"]), (96.1, 5.0, "EUR", "statusline", 3.0))
        self.assertEqual(u["weekly_resets_at"], "2026-10-22T12:00:00Z")
    def test_hook_creates_the_file_and_folder(self):
        shutil.rmtree(self.d); self.run_hook(self.RL); self.assertTrue(os.path.exists(os.path.join(self.d, "usage.json")))
    def test_bad_input_never_fails(self):
        for bad in ("", "not json", "{}", '{"rate_limits": null}', json.dumps({"rate_limits": {"seven_day": {"used_percentage": None}}})):
            r = self.run_hook(bad); self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse(os.path.exists(os.path.join(self.d, "usage.json")))
    def test_show_option_prints_the_weekly_percent(self):
        self.assertEqual(self.run_hook(self.RL, USAGE_STATUSLINE_SHOW="1").stdout, "wk 96%")
    def test_next_command_keeps_an_existing_statusline(self):
        self.assertEqual(self.run_hook(self.RL, USAGE_STATUSLINE_NEXT="echo my-line").stdout.strip(), "my-line")
    def test_hook_output_feeds_the_prompt_end_to_end(self):
        self.run_hook(dict(self.RL, rate_limits={"seven_day": {"used_percentage": 97, "resets_at": time.time() + 86400}}))
        self.assertEqual(ug.check(self.d, dry=True), "95")


if __name__ == "__main__":
    unittest.main()
