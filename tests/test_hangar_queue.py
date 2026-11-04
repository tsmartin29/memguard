"""Hangar Queue tab parser (Nov 4, 2026). Run: python3 -m unittest discover -s tests
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
    def parse(self, text=None, path=FIX):
        if text is not None:
            path = os.path.join(self.d, "q.md")
            with open(path, "w") as fh: fh.write(text)
        out = subprocess.run([self.exe], env={**os.environ, "HANGAR_QUEUE_PARSE": path}, capture_output=True, text=True, check=True).stdout
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

if __name__ == "__main__":
    unittest.main()
