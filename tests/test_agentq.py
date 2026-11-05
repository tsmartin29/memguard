"""agentq: queue tracking via Claude Code hooks. Run: python3 -m unittest discover -s tests
Feeds synthetic hook payloads to `agentq.py hook` in a scratch AGENTQ_DIR and checks the log, the folded state and the markdown view."""
import json, os, shutil, subprocess, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AQ = os.environ.get("AGENTQ_PY") or os.path.join(HERE, "agentq.py")


class AgentQ(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = dict(os.environ, AGENTQ_DIR=self.d, AGENTQ_MD_DIR=self.d, AGENTQ_BUSINESS_HOURS="0-24")
    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)
    def run_aq(self, *args, stdin=None):
        p = subprocess.run([sys.executable, AQ] + list(args), input=stdin, capture_output=True, text=True, env=self.env, timeout=30)
        return p.returncode, p.stdout
    def hook(self, payload): return self.run_aq("hook", stdin=payload if isinstance(payload, str) else json.dumps(payload))[0]
    def state(self): return json.loads(self.run_aq("json")[1])
    def md(self):
        f = [x for x in os.listdir(self.d) if x.startswith("AGENT_QUEUE_") and x.endswith(".md")]
        self.assertEqual(len(f), 1); return open(os.path.join(self.d, f[0])).read()

    def test_background_agent_start_then_finish(self):
        self.assertEqual(self.hook({"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_use_id": "toolu_1",
                                    "tool_input": {"description": "Write the docs", "prompt": "Write the docs\nmore", "model": "haiku"},
                                    "tool_response": "Async agent launched successfully. agentId: a1b2c3d4e5"}), 0)
        self.assertEqual(self.hook({"hook_event_name": "SubagentStart", "agent_id": "a1b2c3d4e5", "agent_type": "general-purpose"}), 0)
        s = self.state(); self.assertEqual((s["running"], s["finished_today"]), (1, 0))
        self.assertIn("| R1 | Write the docs |", self.md())
        self.hook({"hook_event_name": "SubagentStop", "agent_id": "a1b2c3d4e5", "last_assistant_message": "Done: docs written"})
        s = self.state(); self.assertEqual((s["running"], s["finished_today"]), (0, 1))
        self.assertIn("Done: docs written", self.md())

    def test_foreground_agent_finishes_from_post_tool_use(self):
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Task", "tool_use_id": "toolu_2", "tool_input": {"description": "Quick check"},
                   "tool_response": {"content": [{"type": "text", "text": "OK"}]}})
        s = self.state(); self.assertEqual((s["running"], s["finished_today"]), (0, 1))

    def test_tool_use_id_aliases_to_agent_id(self):   # PostToolUse without an agent id, finish carries both ids (from meta.json)
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_use_id": "toolu_3", "tool_input": {"description": "x"},
                   "tool_response": {"status": "async_launched"}})
        sub = os.path.join(self.d, "sess", "subagents"); os.makedirs(sub)
        with open(os.path.join(sub, "agent-abc123def.meta.json"), "w") as fh: json.dump({"toolUseId": "toolu_3", "description": "x"}, fh)
        open(os.path.join(sub, "agent-abc123def.jsonl"), "w").close()
        self.hook({"hook_event_name": "SubagentStop", "agent_id": "abc123def", "agent_transcript_path": os.path.join(sub, "agent-abc123def.jsonl")})
        s = self.state(); self.assertEqual((s["running"], s["finished_today"]), (0, 1))

    def test_fails_open(self):
        for bad in ("not json", "", "[]", json.dumps({"hook_event_name": "SubagentStop"}), json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash"})):
            self.assertEqual(self.hook(bad), 0)
        self.assertTrue(os.path.exists(os.path.join(self.d, "hook_errors.log")))   # the garbage was logged, not raised

    def test_pending_add_done(self):
        self.assertEqual(self.run_aq("add", "first task")[1].split()[0], "Q1")
        self.assertEqual(self.run_aq("add", "second task")[1].split()[0], "Q2")
        self.assertEqual(self.run_aq("done", "1", "shipped")[0], 0)
        self.assertEqual(self.run_aq("done", "Q9")[0], 1)
        s = self.state(); self.assertEqual(s["pending"], 1)
        md = self.md(); self.assertIn("| Q2 | second task |", md); self.assertIn("~~Q1~~", md)
        self.assertEqual(self.run_aq("add", "third")[1].split()[0], "Q3")   # ids are never reused

    def test_stale_only_in_business_hours(self):
        with open(os.path.join(self.d, "agents.jsonl"), "w") as fh:
            fh.write(json.dumps({"ev": "start", "id": "old", "ts": time.time() - 3 * 3600}) + "\n")
        self.assertTrue(self.state()["stale"])
        self.env["AGENTQ_BUSINESS_HOURS"] = "0-0"
        self.assertFalse(self.state()["stale"])

    def test_lost_after_hours(self):
        with open(os.path.join(self.d, "agents.jsonl"), "w") as fh:
            fh.write(json.dumps({"ev": "start", "id": "gone", "ts": time.time() - 13 * 3600}) + "\n")
        self.assertEqual(self.state()["running"], 0)

    def test_handwritten_view_is_kept(self):
        day = time.strftime("%Y-%m-%d")
        path = os.path.join(self.d, "AGENT_QUEUE_%s.md" % day)
        with open(path, "w") as fh: fh.write("# my notes\n")
        self.run_aq("add", "x")
        self.assertTrue(os.path.exists(path + ".handwritten.bak"))
        self.assertIn("generated by agentq", open(path).read())

    def test_backfill_from_transcripts(self):
        sub = os.path.join(self.d, "subagents"); os.makedirs(sub)
        now = time.time(); iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
        with open(os.path.join(sub, "agent-aaa111.meta.json"), "w") as fh: json.dump({"description": "Old job", "model": "sonnet", "toolUseId": "t1"}, fh)
        with open(os.path.join(sub, "agent-aaa111.jsonl"), "w") as fh:
            fh.write(json.dumps({"timestamp": iso(now - 600), "message": {"role": "user", "content": "Do the old job"}}) + "\n")
            fh.write(json.dumps({"timestamp": iso(now - 300), "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "all done"}]}}) + "\n")
        out = self.run_aq("backfill", sub)[1]
        self.assertIn("backfilled 1 agents (1 finished)", out)
        self.assertIn("backfilled 0 agents", self.run_aq("backfill", sub)[1])   # idempotent
        self.assertEqual(self.state()["finished_today"], 1)


if __name__ == "__main__":
    unittest.main()
