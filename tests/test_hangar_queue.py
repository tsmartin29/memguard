"""Hangar Queue tab parser (Nov 4-5, 2026: markdown view and the agentq log). Run: python3 -m unittest discover -s tests
Compiles hangar/Hangar.swift into a scratch folder and runs it in its HANGAR_QUEUE_PARSE mode (prints JSON, opens no window) on
fixtures; skipped when swiftc is not installed. HANGAR_SWIFT=<file> tests a different copy of the source."""
import json, os, shutil, subprocess, tempfile, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("HANGAR_SWIFT") or os.path.join(HERE, "hangar", "Hangar.swift")
if not os.path.exists(SRC): SRC = os.path.join(HERE, "..", "hangar", "Hangar.swift")   # the private layout: memguard/ and hangar/ side by side
FIX = os.path.join(HERE, "tests", "fixtures", "agent_queue_sample.md")

@unittest.skipUnless(shutil.which("swiftc"), "swiftc not installed")
class QueueParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(); cls.exe = os.path.join(cls.d, "hangar_parse")
        subprocess.run(["swiftc", "-o", cls.exe, SRC], check=True, capture_output=True)
    @classmethod
    def tearDownClass(cls): shutil.rmtree(cls.d, ignore_errors=True)
    def parse(self, text=None, path=FIX, name="q.md", now=None):
        if text is not None:
            path = os.path.join(self.d, name)
            with open(path, "w") as fh: fh.write(text)
        env = {**os.environ, "HANGAR_QUEUE_PARSE": path, "HANGAR_BUSINESS_HOURS": "0-24"}
        if now is not None: env["HANGAR_QUEUE_NOW"] = str(now)
        out = subprocess.run([self.exe], env=env, capture_output=True, text=True, check=True).stdout
        return json.loads(out.strip().splitlines()[-1])

    def test_sample_counts(self):
        r = self.parse()
        self.assertEqual(r["running"], 3)       # R4 struck through, the "—" row and the On hold section are not counted
        self.assertEqual(r["queued"], 5)        # Q3 struck through, Q9 under On hold not counted
        self.assertEqual(r["ids"], ["Q1", "Q2", "Q4", "Q5", "QA"])
    def test_top_three_in_order_and_markdown_stripped(self):
        r = self.parse()
        self.assertEqual(r["ids"][:3], ["Q1", "Q2", "Q4"])
        self.assertTrue(r["top"][0].startswith("Crash triage: group the last week"))   # ** removed
        self.assertEqual(len(r["top"]), 3)
    def test_letter_ids_count(self):
        self.assertEqual(self.parse("## Queued\n| # | Job |\n|---|---|\n| QA | one |\n| Q10 | two |\n")["ids"], ["QA", "Q10"])
    def test_wrong_letter_for_section_ignored(self):
        r = self.parse("## Running\n| R1 | a |\n| Q1 | b |\n## Queued\n| Q2 | c |\n| R2 | d |\n")
        self.assertEqual((r["running"], r["ids"]), (1, ["Q2"]))
    def test_struck_through_skipped(self):
        r = self.parse("## Queued\n| ~~Q1~~ | gone |\n| Q2 | ~~gone too~~ |\n| Q3 | kept |\n")
        self.assertEqual(r["ids"], ["Q3"])
    def test_empty_and_missing(self):
        self.assertEqual(self.parse("")["queued"], 0)
        r = self.parse(path=os.path.join(self.d, "does_not_exist.md")); self.assertEqual((r["running"], r["queued"]), (0, 0))
    def test_no_tables_no_counts(self):
        self.assertEqual(self.parse("R1 and Q1 in prose\n| R1 | not under a heading |\n")["running"], 0)

    # the agentq log (agents.jsonl), Nov 5, 2026
    def log(self, events, now):
        return self.parse("\n".join(json.dumps(e) for e in events) + "\n", name="agents.jsonl", now=now)
    def test_jsonl_running_finished_pending(self):
        t = 1_800_000_000.0
        r = self.log([{"ev": "start", "id": "a1", "desc": "one", "ts": t - 600},
                      {"ev": "start", "id": "a2", "tuid": "toolu_2", "ts": t - 500},
                      {"ev": "finish", "id": "a2", "ts": t - 100},
                      {"ev": "start", "id": "toolu_3", "ts": t - 400},                     # PostToolUse without an agent id ...
                      {"ev": "finish", "id": "a3", "tuid": "toolu_3", "ts": t - 50},       # ... finished under its agent id
                      {"ev": "pending_add", "pid": "Q1", "text": "first", "ts": t - 900},
                      {"ev": "pending_add", "pid": "Q2", "text": "second", "ts": t - 800},
                      {"ev": "pending_done", "pid": "Q1", "ts": t - 10}], now=t)
        self.assertEqual((r["running"], r["finished"], r["queued"], r["ids"]), (1, 2, 1, ["Q2"]))
        self.assertFalse(r["stale"])
    def test_jsonl_stale_after_two_hours(self):
        t = 1_800_000_000.0
        self.assertTrue(self.log([{"ev": "start", "id": "a1", "ts": t - 2.5 * 3600}], now=t)["stale"])
        self.assertFalse(self.log([{"ev": "start", "id": "a1", "ts": t - 1.5 * 3600}], now=t)["stale"])
    def test_jsonl_lost_agents_not_running(self):
        t = 1_800_000_000.0
        self.assertEqual(self.log([{"ev": "start", "id": "a1", "ts": t - 13 * 3600}], now=t)["running"], 0)
    def test_jsonl_torn_line_ignored(self):
        r = self.parse('{"ev": "start", "id": "a1", "ts": 1}\n{"ev": "sta\n', name="agents.jsonl", now=2)
        self.assertEqual(r["running"], 1)
    def test_markdown_pending_and_finished_sections(self):   # the view agentq writes
        r = self.parse("## Running (1)\n| R1 | a |\n## PENDING (queued, top first) (2)\n| Q1 | x |\n| Q2 | y |\n## Finished (3)\n| F1 | a |\n| F2 | b |\n| F3 | c |\n## PENDING done today (1)\n| Q0 | z |\n")
        self.assertEqual((r["running"], r["queued"], r["finished"]), (1, 2, 3))
    # departments (Nov 5, 2026)
    def test_jsonl_departments_and_supervisor_flag(self):
        t = 1_800_000_000.0
        ev = [{"ev": "start", "id": "p%d" % i, "dept": "Platform", "ts": t - 600 + i} for i in range(11)]
        ev += [{"ev": "start", "id": "q%d" % i, "dept": "QA", "ts": t - 500 + i} for i in range(10)]
        ev += [{"ev": "start", "id": "s1", "desc": "[dept:Security] review keys", "ts": t - 400},   # prefix only (older log rows)
               {"ev": "start", "id": "u1", "desc": "untagged", "ts": t - 300},
               {"ev": "finish", "id": "p0", "ts": t - 10}]                                             # Platform back to 10
        r = self.log(ev, now=t)
        self.assertEqual(r["depts"], {"Platform": 10, "QA": 10, "Security": 1, "Unassigned": 1})
        self.assertEqual(r["needs_supervisor"], [])
        self.assertIn("review keys", r["running_top"])
        r = self.log(ev[:-1], now=t)
        self.assertEqual((r["depts"]["Platform"], r["needs_supervisor"]), (11, ["Platform"]))
    def test_jsonl_retag_wins(self):
        t = 1_800_000_000.0
        r = self.log([{"ev": "start", "id": "a1", "dept": "QA", "ts": t - 100},
                      {"ev": "info", "id": "a1", "dept": "Growth", "retag": True, "ts": t - 50}], now=t)
        self.assertEqual(r["depts"], {"Growth": 1})
    def test_jsonl_untagged_has_only_unassigned(self):
        r = self.log([{"ev": "start", "id": "a1", "ts": 1_799_999_990.0}], now=1_800_000_000.0)
        self.assertEqual((r["depts"], r["needs_supervisor"]), ({"Unassigned": 1}, []))

    # Next up stays current (Nov 5, 2026): priority first, then newest first; [Qn] agents; age tags
    def test_next_up_priority_then_newest_first(self):
        t = 1_800_000_000.0
        adds = [{"ev": "pending_add", "pid": "Q%d" % i, "text": "item %d" % i, "ts": t - 5000 + i} for i in range(1, 6)]
        r = self.log(adds, now=t)
        self.assertEqual(r["ids"], ["Q5", "Q4", "Q3"])                       # newest first, never oldest first
        r = self.log(adds + [{"ev": "pending_add", "pid": "Q6", "text": "urgent", "top": True, "ts": t - 100}], now=t)
        self.assertEqual((r["ids"], r["tops"]), (["Q6", "Q5", "Q4"], [True, False, False]))
        r = self.log(adds + [{"ev": "pending_top", "pid": "Q1", "ts": t - 50}, {"ev": "pending_top", "pid": "Q2", "ts": t - 40}], now=t)
        self.assertEqual(r["ids"], ["Q2", "Q1", "Q5"])                       # promoted later comes first among priority items
    def test_q_tag_in_progress_then_done(self):
        t = 1_800_000_000.0
        base = [{"ev": "pending_add", "pid": "Q1", "text": "a", "ts": t - 900}, {"ev": "pending_add", "pid": "Q2", "text": "b", "ts": t - 800}]
        run = base + [{"ev": "start", "id": "g1", "qid": "Q2", "ts": t - 300}]
        r = self.log(run, now=t); self.assertEqual((r["ids"], r["queued"], r["in_progress"]), (["Q1"], 1, 1))
        r = self.log(run + [{"ev": "finish", "id": "g1", "ts": t - 10}], now=t); self.assertEqual((r["ids"], r["queued"], r["in_progress"]), (["Q1"], 1, 0))
        # the finish row alone (no qid on it) still closes the item: the start row carried the tag
        r = self.log(base + [{"ev": "start", "id": "g2", "tuid": "tu", "qid": "Q1", "ts": t - 300}, {"ev": "finish", "id": "g2", "ts": t - 5}], now=t)
        self.assertEqual(r["ids"], ["Q2"])
        r = self.log(base + [{"ev": "start", "id": "gone", "qid": "Q1", "ts": t - 13 * 3600}], now=t)   # lost agent: the item is pending again
        self.assertEqual((r["ids"], r["in_progress"]), (["Q2", "Q1"], 0))
        self.assertEqual(self.log(base + [{"ev": "start", "id": "g3", "qid": "Q9", "ts": t - 5}], now=t)["in_progress"], 0)
    def test_age_tag_after_24_hours(self):
        t = 1_800_000_000.0
        r = self.log([{"ev": "pending_add", "pid": "Q1", "text": "old", "ts": t - 30 * 3600}, {"ev": "pending_add", "pid": "Q2", "text": "newer", "ts": t - 100}], now=t)
        self.assertEqual((r["ids"], r["aged"], r["ages"]), (["Q2", "Q1"], 1, [100, 30 * 3600]))
    def board(self, touches, now):
        with open(os.path.join(self.d, "touches.jsonl"), "w") as fh: fh.write("".join(json.dumps(e) + "\n" for e in touches) + "garbled {\n")
        try: return self.log([{"ev": "start", "id": "a1", "ts": now - 60}], now=now)
        finally: os.remove(os.path.join(self.d, "touches.jsonl"))
    def test_overlap_count_matches_agentq(self):   # distinct repo + file + pair, both directions are one, past 2 h does not count
        t = 1_800_000_000.0
        o = lambda a, p, f, age=60, r="~/r": {"ev": "overlap", "agent": a, "peer": p, "file": f, "repo": r, "ts": t - age}
        self.assertEqual(self.board([o("a", "b", "x"), o("b", "a", "x"), o("a", "b", "x", 120), o("a", "c", "x"), o("a", "b", "y"), o("a", "b", "x", 60, "~/s"),
                                     o("d", "e", "z", 3 * 3600), {"ev": "touch", "agent": "a", "file": "overlap", "repo": "~/r", "ts": t}], t)["collisions"], 4)
        self.assertEqual(self.log([{"ev": "start", "id": "a1", "ts": t - 60}], now=t)["collisions"], 0)   # no touches file yet
    def test_promoted_marker(self):
        t = 1_800_000_000.0
        r = self.log([{"ev": "start", "id": "a1", "desc": "Build the board", "ts": t - 60}, {"ev": "start", "id": "a2", "desc": "Old work", "ts": t - 600},
                      {"ev": "finish", "id": "a2", "ts": t - 500}, {"ev": "promote", "agent": "a2", "role": "lead", "ts": t - 40}, {"ev": "promote", "agent": "a1", "role": "wrong", "ts": t - 30},
                      {"ev": "promote", "agent": "a1", "role": "supervisor", "ts": t - 20}, {"ev": "promote", "agent": "unknownagent1", "role": "x", "ts": t - 10}], now=t)
        self.assertEqual([(p["role"], p["desc"], p["running"]) for p in r["promoted"]],
                         [("x", "unknowna", False), ("supervisor", "Build the board", True), ("lead", "Old work", False)])
    def test_idea_and_promote_rows_do_not_disturb_the_queue(self):
        t = 1_800_000_000.0
        r = self.log([{"ev": "start", "id": "a1", "ts": t - 60}, {"ev": "idea_add", "iid": "I1", "text": "x", "ts": t - 50}, {"ev": "idea_adopt", "iid": "I1", "ts": t - 40}], now=t)
        self.assertEqual((r["running"], r["queued"], r["promoted"]), (1, 0, []))

if __name__ == "__main__":
    unittest.main()
