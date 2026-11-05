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

    def test_post_tool_use_after_finish_stays_finished(self):   # a fast background agent: SubagentStop lands before PostToolUse
        self.hook({"hook_event_name": "SubagentStart", "agent_id": "fast1234"})
        self.hook({"hook_event_name": "SubagentStop", "agent_id": "fast1234", "last_assistant_message": "OK"})
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_use_id": "t5", "tool_input": {"description": "fast"},
                   "tool_response": "Async agent launched. agentId: fast1234"})
        s = self.state(); self.assertEqual((s["running"], s["finished_today"]), (0, 1))
        self.assertIn("| OK |", self.md())

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

    # departments (Nov 5, 2026)
    def start(self, aid, desc, **kw):
        self.hook(dict({"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_use_id": "t_" + aid, "tool_input": {"description": desc},
                        "tool_response": "Async agent launched. agentId: " + aid}, **kw))
    def test_dept_prefix_parsed_and_stripped(self):
        self.start("qa000001", "[dept:QA] Run the scenario suite")
        s = self.state(); self.assertEqual(s["by_dept"], {"QA": 1})
        md = self.md(); self.assertIn("| R1 | Run the scenario suite |", md); self.assertIn("| QA |", md); self.assertNotIn("[dept:", md)
        self.assertIn("## Departments", md)
    def test_dept_from_subagent_meta(self):
        sub = os.path.join(self.d, "s2", "subagents"); os.makedirs(sub)
        with open(os.path.join(sub, "agent-sec00001.meta.json"), "w") as fh: json.dump({"description": "[ Dept : Security ] Review keys"}, fh)
        tp = os.path.join(sub, "agent-sec00001.jsonl"); open(tp, "w").close()
        self.hook({"hook_event_name": "SubagentStart", "agent_id": "sec00001", "transcript_path": tp})
        self.assertEqual(self.state()["by_dept"], {"Security": 1})
        self.assertIn("| R1 | Review keys |", self.md())
    def test_dept_from_env(self):
        self.env["AGENTQ_DEPT"] = "Platform"
        self.start("plat0001", "Build the app")
        self.start("plat0002", "[dept:Growth] Draft a post")    # the prefix wins over the env
        self.assertEqual(self.state()["by_dept"], {"Platform": 1, "Growth": 1})
    def test_untagged_looks_as_before(self):
        self.start("plain001", "Plain job")
        s = self.state(); self.assertEqual((s["by_dept"], s["needs_supervisor"]), ({"Unassigned": 1}, []))
        self.assertNotIn("## Departments", self.md())
        out = self.run_aq("list")[1]; self.assertIn("  R1  Plain job", out); self.assertNotIn("Unassigned", out)
    def test_needs_supervisor_over_ten(self):
        for i in range(11): self.start("radio%03d" % i, "[dept:Radio] job %d" % i)
        for i in range(10): self.start("qa%06d" % i, "[dept:QA] job %d" % i)
        s = self.state(); self.assertEqual(s["needs_supervisor"], ["Radio"])   # 11 > 10; 10 is not over
        self.assertIn("needs supervisor: Radio", self.run_aq("status")[1])
        d = self.run_aq("depts")[1]; self.assertRegex(d, r"Radio\s+11 running  NEEDS SUPERVISOR"); self.assertNotRegex(d, r"QA.*NEEDS")
        self.assertIn("| Radio | 11 | needs supervisor (over 10) |", self.md())
        self.hook({"hook_event_name": "SubagentStop", "agent_id": "radio000"})   # back to 10: flag clears
        self.assertEqual(self.state()["needs_supervisor"], [])
    def test_list_groups_and_filters(self):
        self.start("aa000001", "[dept:QA] one"); self.start("bb000001", "[dept:Research] two"); self.start("cc000001", "[dept:QA] three")
        self.run_aq("add", "later")
        out = self.run_aq("list")[1]
        self.assertLess(out.index("QA (2 running)"), out.index("Research (1 running)"))
        self.assertIn("Q1  later", out)
        f = self.run_aq("list", "research")[1]; self.assertIn("two", f); self.assertNotIn("one", f); self.assertNotIn("later", f)
        self.assertIn("no running agents in Growth", self.run_aq("list", "Growth")[1])
    def test_odd_dept_names_cleaned(self):
        self.start("odd00001", "[dept:  Q|A `x`  ] job")
        self.assertEqual(list(self.state()["by_dept"]), ["QA x"])
    def test_tag_running_agent(self):
        self.start("tg000001", "Head of QA"); self.start("tg000002", "[dept:Research] sweep")
        out = self.run_aq("list")[1]; self.assertIn("R1  sweep", out); self.assertIn("R2  Head of QA", out)   # Research first, Unassigned last
        rc, msg = self.run_aq("tag", "R2", "QA"); self.assertEqual(rc, 0); self.assertIn("QA", msg)
        self.assertEqual(self.state()["by_dept"], {"QA": 1, "Research": 1})
        self.run_aq("tag", "tg000002", "Growth")                      # by agent id; a later tag wins
        self.assertEqual(self.state()["by_dept"], {"QA": 1, "Growth": 1})
        self.assertEqual(self.run_aq("tag", "R9", "QA")[0], 1)
        self.assertEqual(self.run_aq("tag", "R1")[0], 2)

if __name__ == "__main__":
    unittest.main()
