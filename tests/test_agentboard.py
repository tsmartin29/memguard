"""agentq work board, ideas and promotion. Run: python3 -m unittest discover -s tests
Feeds synthetic hook payloads to `agentq.py hook` in a scratch AGENTQ_DIR, against real git repos and worktrees in a temp folder."""
import json, os, shutil, stat, subprocess, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AQ = os.environ.get("AGENTQ_PY") or os.path.join(HERE, "agentq.py")


class Base(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp())
        self.q = os.path.join(self.d, "q")
        self.repo = os.path.join(self.d, "repo")
        os.makedirs(self.q); os.makedirs(os.path.join(self.repo, "src"))
        self.env = dict(os.environ, AGENTQ_DIR=self.q, AGENTQ_MD_DIR=self.q, AGENTQ_BUSINESS_HOURS="0-24", AGENTQ_SCRATCH="/nonexistent/")
        self.env.pop("AGENTQ_DEPT", None); self.env.pop("AGENTQ_AGENT", None)
        self.git("init", "-q", ".")
    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)
    def git(self, *a, cwd=None):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), cwd=cwd or self.repo, check=True, capture_output=True)
    def aq(self, *args, stdin=None):
        p = subprocess.run([sys.executable, AQ] + list(args), input=stdin, capture_output=True, text=True, env=self.env, timeout=30)
        return p.returncode, p.stdout
    def hook(self, payload):
        rc, out = self.aq("hook", stdin=payload if isinstance(payload, str) else json.dumps(payload))
        self.assertEqual(rc, 0)
        if not out: return ""
        j = json.loads(out)                                   # stdout is empty or one valid JSON object
        self.assertEqual(list(j), ["hookSpecificOutput"])     # no decision/continue key: it can never block
        return j["hookSpecificOutput"]["additionalContext"]
    def start(self, aid, desc="task", dept="Platform"):
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_use_id": "t" + aid, "session_id": "sess1234",
                   "tool_input": {"description": "[dept:%s] %s" % (dept, desc)}, "tool_response": "Async agent launched. agentId: " + aid})
        self.hook({"hook_event_name": "SubagentStart", "agent_id": aid, "session_id": "sess1234", "cwd": self.repo})
    def finish(self, aid): self.hook({"hook_event_name": "SubagentStop", "agent_id": aid, "session_id": "sess1234", "last_assistant_message": "done"})
    def edit(self, aid, path, tool="Edit", session="sess1234"):
        p = {"hook_event_name": "PostToolUse", "tool_name": tool, "session_id": session, "cwd": self.repo, "tool_input": {"file_path": path}}
        if aid: p["agent_id"] = aid
        return self.hook(p)
    def rows(self):
        try:
            with open(os.path.join(self.q, "touches.jsonl")) as fh: return [json.loads(l) for l in fh if l.strip()]
        except FileNotFoundError: return []
    def f(self, name="src/a.py"): return os.path.join(self.repo, name)


class Board(Base):
    def test_touch_records_repo_relative_file_and_who(self):
        self.start("aaaaaaaa1")
        self.assertEqual(self.edit("aaaaaaaa1", self.f()), "")
        r = self.rows()
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["agent"], r[0]["file"], r[0]["ev"]), ("aaaaaaaa1", "src/a.py", "touch"))
        self.assertTrue(r[0]["repo"].endswith("/repo"))

    def test_touch_is_deduped(self):
        self.start("aaaaaaaa1")
        for _ in range(3): self.edit("aaaaaaaa1", self.f())
        self.assertEqual(len(self.rows()), 1)
        self.edit("aaaaaaaa1", self.f("src/b.py"))
        self.assertEqual(len(self.rows()), 2)

    def test_old_touch_past_dedup_window_logs_again(self):
        self.env["AGENTQ_TOUCH_DEDUP_MIN"] = "0"
        self.start("aaaaaaaa1")
        self.edit("aaaaaaaa1", self.f()); self.edit("aaaaaaaa1", self.f())
        self.assertEqual(len(self.rows()), 2)

    def test_write_multiedit_notebook_and_relative_path(self):
        self.start("aaaaaaaa1")
        self.edit("aaaaaaaa1", self.f("w.py"), tool="Write")
        self.edit("aaaaaaaa1", self.f("m.py"), tool="MultiEdit")
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "NotebookEdit", "agent_id": "aaaaaaaa1", "cwd": self.repo, "tool_input": {"notebook_path": self.f("n.ipynb")}})
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Edit", "agent_id": "aaaaaaaa1", "cwd": self.repo, "tool_input": {"file_path": "src/rel.py"}})
        self.assertEqual({r["file"] for r in self.rows()}, {"w.py", "m.py", "n.ipynb", "src/rel.py"})

    def test_second_agent_is_warned_and_overlap_logged(self):
        self.start("aaaaaaaa1", "Build the board"); self.start("bbbbbbbb2", "Tune the hook", dept="QA")
        self.assertEqual(self.edit("aaaaaaaa1", self.f()), "")
        w = self.edit("bbbbbbbb2", self.f())
        for want in ("aaaaaaaa", "Platform", "Build the board", "src/a.py", "warn-only", "not an instruction"): self.assertIn(want, w)
        ov = [r for r in self.rows() if r["ev"] == "overlap"]
        self.assertEqual([(r["agent"], r["peer"]) for r in ov], [("bbbbbbbb2", "aaaaaaaa1")])

    def test_warning_repeats_only_after_the_quiet_period(self):
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2")
        self.edit("aaaaaaaa1", self.f())
        self.assertTrue(self.edit("bbbbbbbb2", self.f()))
        self.assertEqual(self.edit("bbbbbbbb2", self.f()), "")
        self.env["AGENTQ_WARN_EVERY_MIN"] = "0"
        self.assertTrue(self.edit("bbbbbbbb2", self.f()))

    def test_no_warning_for_finished_agent(self):
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2")
        self.edit("aaaaaaaa1", self.f()); self.finish("aaaaaaaa1")
        self.assertEqual(self.edit("bbbbbbbb2", self.f()), "")
        self.assertEqual([r for r in self.rows() if r["ev"] == "overlap"], [])

    def test_resumed_agent_counts_again(self):   # a touch after its finish means it woke up
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2")
        self.finish("aaaaaaaa1"); time.sleep(0.02); self.edit("aaaaaaaa1", self.f())
        self.assertTrue(self.edit("bbbbbbbb2", self.f()))

    def test_no_warning_past_the_window(self):
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2")
        self.edit("aaaaaaaa1", self.f())
        path = os.path.join(self.q, "touches.jsonl")
        with open(path) as fh: row = json.loads(fh.read())
        row["ts"] -= 3 * 3600                                  # same repo, same file, only the age differs
        with open(path, "w") as fh: fh.write(json.dumps(row) + "\n")
        self.assertEqual(self.edit("bbbbbbbb2", self.f()), "")
        self.assertEqual([r for r in self.rows() if r["ev"] == "overlap"], [])

    def test_same_name_other_repo_is_no_collision(self):
        other = os.path.join(self.d, "other"); os.makedirs(os.path.join(other, "src")); self.git("init", "-q", ".", cwd=other)
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2")
        self.edit("aaaaaaaa1", self.f())
        self.assertEqual(self.edit("bbbbbbbb2", os.path.join(other, "src", "a.py")), "")

    def test_worktree_counts_as_the_same_repo(self):
        with open(self.f("README"), "w") as fh: fh.write("x")
        self.git("add", "."); self.git("commit", "-qm", "x")
        wt = os.path.join(self.d, "wt-feature")
        self.git("worktree", "add", "-q", "-b", "feature", wt)
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2")
        self.edit("aaaaaaaa1", self.f())
        w = self.edit("bbbbbbbb2", os.path.join(wt, "src", "a.py"))
        self.assertIn("aaaaaaaa", w)
        rows = self.rows()
        self.assertEqual(rows[0]["repo"], rows[1]["repo"])
        self.assertEqual(rows[1]["wt"], "wt-feature")
        self.assertEqual(rows[1]["file"], "src/a.py")
        w2 = self.edit("aaaaaaaa1", self.f())   # the main checkout's agent hears it is a different worktree
        self.assertIn("in worktree wt-feature", w2)

    def test_two_worktrees_warn_each_other(self):
        with open(self.f("README"), "w") as fh: fh.write("x")
        self.git("add", "."); self.git("commit", "-qm", "x")
        w1, w2 = os.path.join(self.d, "w1"), os.path.join(self.d, "w2")
        self.git("worktree", "add", "-q", "-b", "one", w1); self.git("worktree", "add", "-q", "-b", "two", w2)
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2")
        self.edit("aaaaaaaa1", os.path.join(w1, "x.py"))
        self.assertIn("in worktree w1", self.edit("bbbbbbbb2", os.path.join(w2, "x.py")))

    def test_file_outside_any_repo_is_tracked_by_full_path(self):
        d = os.path.join(self.d, "plain"); os.makedirs(d)
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2")
        self.edit("aaaaaaaa1", os.path.join(d, "doc.md"))
        self.assertTrue(self.edit("bbbbbbbb2", os.path.join(d, "doc.md")))
        self.assertEqual(self.rows()[0]["repo"], "(no repo)")

    def test_scratch_files_are_ignored_outside_a_repo(self):
        d = os.path.join(self.d, "plain"); os.makedirs(d)
        self.env["AGENTQ_SCRATCH"] = self.d + "/"
        self.start("aaaaaaaa1")
        self.edit("aaaaaaaa1", os.path.join(d, "tmp.txt"))
        self.assertEqual(self.rows(), [])
        self.edit("aaaaaaaa1", self.f())   # a real repo under a scratch prefix still counts: agents work in worktrees there
        self.assertEqual(len(self.rows()), 1)

    def test_main_session_is_an_actor_but_not_a_collision_with_its_own_subagent(self):
        self.start("aaaaaaaa1")
        self.edit("", self.f(), session="sess1234abcdef")
        self.assertEqual(self.rows()[0]["agent"], "s:sess1234")
        p = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "agent_id": "aaaaaaaa1", "session_id": "sess1234abcdef", "cwd": self.repo, "tool_input": {"file_path": self.f()}}
        self.assertEqual(self.hook(p), "")
        self.edit("", self.f("src/z.py"), session="other123")   # someone else's main session does count
        self.assertIn("main session other123", self.edit("aaaaaaaa1", self.f("src/z.py"), session="sess1234abcdef"))

    def test_board_shows_dept_task_files(self):
        self.start("aaaaaaaa1", "Build the board"); self.start("bbbbbbbb2", "Tune the hook", dept="QA"); self.start("cccccccc3", "No files yet")
        self.edit("aaaaaaaa1", self.f()); self.edit("bbbbbbbb2", self.f("src/b.py"))
        rc, out = self.aq("board")
        self.assertEqual(rc, 0)
        for want in ("3 running", "2 with files", "Platform", "QA", "Build the board", "Tune the hook", "No files yet", "src/a.py", "src/b.py"): self.assertIn(want, out)

    def test_board_drops_finished_agents_and_filters_by_repo(self):
        self.start("aaaaaaaa1", "Gone now"); self.start("bbbbbbbb2", "Here")
        self.edit("aaaaaaaa1", self.f()); self.edit("bbbbbbbb2", self.f("src/b.py")); self.finish("aaaaaaaa1")
        out = self.aq("board")[1]
        self.assertNotIn("Gone now", out); self.assertIn("Here", out)
        self.assertNotIn("Here", self.aq("board", "nosuchrepo")[1])
        self.assertIn("Here", self.aq("board", "repo")[1])

    def test_collisions_lists_and_counts(self):
        self.assertIn("0 overlaps", self.aq("collisions")[1])
        self.start("aaaaaaaa1", "Alpha"); self.start("bbbbbbbb2", "Beta")
        self.edit("aaaaaaaa1", self.f()); self.edit("bbbbbbbb2", self.f()); self.edit("aaaaaaaa1", self.f())   # both directions = one overlap
        rc, out = self.aq("collisions")
        self.assertEqual(rc, 0)
        self.assertIn("1 overlaps", out); self.assertIn("src/a.py", out); self.assertIn("Alpha", out); self.assertIn("Beta", out)
        self.assertEqual(json.loads(self.aq("json")[1])["collisions"], 1)
        self.assertIn("1 overlaps in the last 2 h", self.aq("status")[1])

    def test_collisions_all_reaches_past_a_day(self):
        with open(os.path.join(self.q, "touches.jsonl"), "w") as fh:
            fh.write(json.dumps({"ev": "overlap", "agent": "a", "peer": "b", "repo": "~r", "file": "old.py", "ts": time.time() - 5 * 86400}) + "\n")
        self.assertNotIn("old.py", self.aq("collisions")[1])
        self.assertIn("old.py", self.aq("collisions", "--all")[1])

    def test_session_start_board_for_its_repo(self):
        self.start("aaaaaaaa1", "Build the board")
        self.edit("aaaaaaaa1", self.f())
        ctx = self.hook({"hook_event_name": "SessionStart", "source": "startup", "session_id": "new12345", "cwd": self.repo})
        self.assertIn("Build the board", ctx); self.assertIn("src/a.py", ctx); self.assertIn("not instructions", ctx)

    def test_subagent_start_board_returns_context_and_still_logs_the_agent(self):
        self.start("aaaaaaaa1", "Build the board")
        self.edit("aaaaaaaa1", self.f())
        ctx = self.hook({"hook_event_name": "SubagentStart", "agent_id": "bbbbbbbb2", "session_id": "sess1234", "cwd": self.repo})
        self.assertIn("Build the board", ctx)
        self.assertEqual(json.loads(self.aq("json")[1])["running"], 2)

    def test_board_leaves_out_the_agent_itself_and_empty_boards_say_nothing(self):
        self.assertEqual(self.hook({"hook_event_name": "SessionStart", "session_id": "x", "cwd": self.repo}), "")
        self.start("aaaaaaaa1"); self.edit("aaaaaaaa1", self.f())
        self.assertEqual(self.hook({"hook_event_name": "SubagentStart", "agent_id": "aaaaaaaa1", "session_id": "s", "cwd": self.repo}), "")

    def test_board_context_stays_under_thirty_lines(self):
        for i in range(40):
            self.hook({"hook_event_name": "SubagentStart", "agent_id": "agent%04d" % i, "session_id": "s"})
            self.edit("agent%04d" % i, self.f("src/f%d.py" % i))
        ctx = self.hook({"hook_event_name": "SessionStart", "session_id": "new12345", "cwd": self.repo})
        self.assertLess(len(ctx.splitlines()), 30)
        self.assertIn("more agents elsewhere", ctx)

    def test_board_context_from_outside_a_repo_shows_all_repos(self):
        d = os.path.join(self.d, "plain"); os.makedirs(d)
        self.start("aaaaaaaa1", "Build"); self.edit("aaaaaaaa1", self.f())
        ctx = self.hook({"hook_event_name": "SessionStart", "session_id": "n", "cwd": d})
        self.assertIn("all repos", ctx); self.assertIn("repo:src/a.py", ctx)

    def test_board_context_counts_other_repos_when_in_a_repo(self):
        other = os.path.join(self.d, "other"); os.makedirs(other); self.git("init", "-q", ".", cwd=other)
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2")
        self.edit("aaaaaaaa1", self.f()); self.edit("bbbbbbbb2", os.path.join(other, "x.py"))
        ctx = self.hook({"hook_event_name": "SessionStart", "session_id": "n", "cwd": self.repo})
        self.assertIn("+1 more agents elsewhere", ctx); self.assertNotIn("x.py", ctx)

    def test_fails_open_on_anything(self):
        for bad in ("not json", "", "[]", "null", json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Edit"}),
                    json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": "x"}),
                    json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": "/nonexistent/\u0000bad"}}),
                    json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": 5}}),
                    json.dumps({"hook_event_name": "SessionStart", "cwd": 7}), json.dumps({"hook_event_name": "SubagentStart"})):
            rc, out = self.aq("hook", stdin=bad)
            self.assertEqual((rc, out), (0, ""), bad)

    def test_fails_open_when_the_folder_cannot_be_written(self):
        self.env["AGENTQ_DIR"] = "/nonexistent/agentq"
        self.env["AGENTQ_MD_DIR"] = "/nonexistent/agentq"
        self.assertEqual(self.edit("aaaaaaaa1", self.f()), "")
        self.assertEqual(self.hook({"hook_event_name": "SubagentStart", "agent_id": "a1", "cwd": self.repo}), "")

    def test_fails_open_on_a_garbled_log(self):
        self.start("aaaaaaaa1"); self.edit("aaaaaaaa1", self.f())
        with open(os.path.join(self.q, "touches.jsonl"), "ab") as fh: fh.write(b'{"ev": "touch", "file": "src/a.py"\n\xff\xfe nonsense\n[]\n')
        self.start("bbbbbbbb2")
        self.assertTrue(self.edit("bbbbbbbb2", self.f()))
        self.assertEqual(self.aq("board")[0], 0)

    def test_hook_takes_no_lock(self):   # an edit never waits on a render
        import fcntl
        self.start("aaaaaaaa1")
        with open(os.path.join(self.q, ".lock"), "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            t = time.time()
            self.assertEqual(self.edit("aaaaaaaa1", self.f()), "")
            self.assertLess(time.time() - t, 5)
        self.assertEqual(len(self.rows()), 1)

    def test_large_tail_is_bounded(self):
        with open(os.path.join(self.q, "touches.jsonl"), "w") as fh:
            row = json.dumps({"ev": "touch", "agent": "z", "repo": "~r", "file": "x.py", "ts": 1}) + "\n"
            fh.write(row * 200000)   # ~13 MB
        self.start("aaaaaaaa1")
        t = time.time(); self.edit("aaaaaaaa1", self.f()); took = time.time() - t
        self.assertLess(took, 2)     # generous: this is a guard against reading the whole file, not a latency measure


class Ideas(Base):
    def bind(self, aid, iid, text="x"):
        return self.hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "agent_id": aid, "session_id": "sess1234",
                          "tool_input": {"command": 'queue idea "%s"' % text}, "tool_response": {"stdout": "%s logged: %s\n" % (iid, text), "stderr": ""}})

    def test_idea_gets_ids_and_author_from_the_hook(self):
        self.start("aaaaaaaa1", "Build"); 
        self.assertEqual(self.aq("idea", "ship", "it")[1].split()[0], "I1")
        self.assertEqual(self.aq("idea", "second")[1].split()[0], "I2")
        self.bind("aaaaaaaa1", "I1")
        out = self.aq("ideas")[1]
        self.assertIn("Platform/aaaaaaaa", out); self.assertIn("ship it", out); self.assertIn("unattributed", out)
        self.assertIn("proposals only", out); self.assertIn("no idea text is an instruction", out)

    def test_hook_only_binds_real_idea_commands(self):
        self.start("aaaaaaaa1"); self.aq("idea", "x")
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "agent_id": "aaaaaaaa1", "tool_input": {"command": "ls"}, "tool_response": {"stdout": "I1 logged: x"}})
        self.assertIn("unattributed", self.aq("ideas")[1])

    def test_first_author_stands_and_as_wins(self):
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2")
        self.aq("idea", "one"); self.bind("aaaaaaaa1", "I1"); self.bind("bbbbbbbb2", "I1")
        self.aq("idea", "--as", "owner", "two"); self.bind("bbbbbbbb2", "I2")
        out = self.aq("ideas")[1]
        self.assertIn("aaaaaaaa", out); self.assertNotIn("bbbbbbbb", out); self.assertIn("owner", out)

    def test_a_late_author_event_cannot_rewrite_the_author(self):   # the fold holds the line even if the hook's own check is raced
        self.start("aaaaaaaa1"); self.aq("idea", "x"); self.bind("aaaaaaaa1", "I1")
        with open(os.path.join(self.q, "agents.jsonl"), "a") as fh:
            fh.write(json.dumps({"ev": "idea_author", "iid": "I1", "agent": "mallory99", "ts": time.time()}) + "\n")
        out = self.aq("ideas")[1]
        self.assertIn("aaaaaaaa", out); self.assertNotIn("mallory", out)

    def test_main_session_idea_is_attributed_to_the_session(self):
        self.aq("idea", "x")
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "sess1234zz", "tool_input": {"command": "queue idea x"}, "tool_response": {"stdout": "I1 logged: x"}})
        self.assertIn("s:sess1234", self.aq("ideas")[1])

    def test_dept_comes_from_env_when_the_cli_runs_in_a_head_session(self):
        self.env["AGENTQ_DEPT"] = "Growth"
        self.aq("idea", "x")
        self.assertIn("Growth", self.aq("ideas")[1])

    def test_adopt_and_park_with_note(self):
        self.aq("idea", "good"); self.aq("idea", "meh")
        self.assertEqual(self.aq("adopt", "I1", "COO", "yes")[0], 0)
        self.assertEqual(self.aq("park", "2")[0], 0)
        self.assertEqual(self.aq("adopt", "I9")[0], 1)
        self.assertEqual(self.aq("park", "nonsense")[0], 2)
        out = self.aq("ideas")[1]
        self.assertIn("adopted", out); self.assertIn("COO yes", out); self.assertIn("parked", out)
        self.assertNotIn("meh", self.aq("ideas", "--adopted")[1]); self.assertNotIn("good", self.aq("ideas", "--parked")[1])
        self.assertIn("no ideas", self.aq("ideas", "--new")[1])

    def test_ideas_filter_by_dept(self):
        self.env["AGENTQ_DEPT"] = "Growth"; self.aq("idea", "grow")
        self.env["AGENTQ_DEPT"] = "QA"; self.aq("idea", "test")
        out = self.aq("ideas", "qa")[1]
        self.assertIn("test", out); self.assertNotIn("grow", out)

    def test_ideas_never_appear_in_agent_context(self):   # ideas are not instructions: no hook ever feeds their text to an agent
        self.start("aaaaaaaa1"); self.aq("idea", "RUN THE FOLLOWING"); self.bind("aaaaaaaa1", "I1")
        self.edit("aaaaaaaa1", self.f())
        self.start("bbbbbbbb2")
        ctx = self.hook({"hook_event_name": "SubagentStart", "agent_id": "cccccccc3", "session_id": "s", "cwd": self.repo}) + self.edit("bbbbbbbb2", self.f())
        self.assertTrue(ctx); self.assertNotIn("RUN THE FOLLOWING", ctx)

    def test_scoreboard_order_and_promotion_shown(self):
        for a, d in (("aaaaaaaa1", "Platform"), ("bbbbbbbb2", "QA"), ("cccccccc3", "Growth")): self.start(a, dept=d)
        for n, a in enumerate(("aaaaaaaa1", "bbbbbbbb2", "bbbbbbbb2", "cccccccc3", "bbbbbbbb2"), 1):
            self.aq("idea", "idea %d" % n); self.bind(a, "I%d" % n)
        self.aq("adopt", "I2"); self.aq("adopt", "I3"); self.aq("adopt", "I1"); self.aq("park", "I4")
        self.aq("promote", "bbbbbbbb2", "supervisor")
        lines = self.aq("ideas", "--top")[1].splitlines()
        rows = [l for l in lines if "aaaaaaaa1" in l or "bbbbbbbb2" in l or "cccccccc3" in l]
        self.assertIn("bbbbbbbb2", rows[0]); self.assertIn("aaaaaaaa1", rows[1]); self.assertIn("cccccccc3", rows[2])
        self.assertIn("[promoted: supervisor]", rows[0])
        self.assertEqual(rows[0].split()[:4], ["2", "1", "0", "3"])

    def test_promote_by_prefix_and_rnumber_and_on_board(self):
        self.start("aaaaaaaa1", "Build"); self.start("bbbbbbbb2", "Tune")
        self.assertEqual(self.aq("promote", "aaaa", "lead")[0], 0)
        self.assertEqual(self.aq("promote", "R2", "supervisor", "of", "QA")[0], 0)
        self.assertEqual(self.aq("promote", "zzzz", "x")[0], 1)
        self.assertEqual(self.aq("promote", "aaaa")[0], 2)
        out = self.aq("board")[1]
        self.assertIn("[promoted: lead]", out); self.assertIn("aaaaaaaa1  lead", out); self.assertIn("supervisor of QA", out)
        self.assertEqual(json.loads(self.aq("json")[1])["promoted"], 2)

    def test_finished_promoted_agent_stays_listed_for_resume(self):
        self.start("aaaaaaaa1", "Build"); self.aq("promote", "aaaa", "lead"); self.finish("aaaaaaaa1")
        out = self.aq("board")[1]
        self.assertIn("Promoted", out); self.assertIn("aaaaaaaa1", out)

    def test_ambiguous_prefix_refused(self):
        self.start("aaaaaaaa1"); self.start("aaaaaaaa2")
        self.assertEqual(self.aq("promote", "aaaa", "x")[0], 1)

    def test_promoted_marker_in_collision_warning(self):
        self.start("aaaaaaaa1"); self.start("bbbbbbbb2"); self.aq("promote", "aaaa", "lead")
        self.edit("aaaaaaaa1", self.f())
        self.assertIn("[promoted: lead]", self.edit("bbbbbbbb2", self.f()))

    def test_status_counts_new_ideas(self):
        self.aq("idea", "x"); self.aq("idea", "y"); self.aq("park", "1")
        self.assertIn("1 new ideas", self.aq("status")[1])


if __name__ == "__main__":
    unittest.main()
