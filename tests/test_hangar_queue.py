"""Hangar Queue tab parser (Nov 4-5, 2026: markdown view and the agentq log). Run: python3 -m unittest discover -s tests
Compiles hangar/Hangar.swift into a scratch folder and runs it in its HANGAR_QUEUE_PARSE mode (prints JSON, opens no window) on
fixtures; skipped when swiftc is not installed. HANGAR_SWIFT=<file> tests a different copy of the source."""
import json, os, shutil, subprocess, tempfile, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("HANGAR_SWIFT") or os.path.join(HERE, "hangar", "Hangar.swift")
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

if __name__ == "__main__":
    unittest.main()
