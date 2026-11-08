"""agentgate: launch gate, Bash rules, wording rules (pure functions; no hooks, no network)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agentgate"))
import agentgate as G, rules_bash as B, rules_wording as Wd

C = dict(G.DEFAULTS, require_file="AGENT_STANDARD.md", no_keychain=True, no_chip_burn=True,
         deploy_patterns=[r"\bwrangler\b[^\n]*\bpages\s+deploy"], public_paths=["/site/src/"],
         banned_phrases=[{"rx": r"\bguarantee(d|s)?\b", "why": "no guarantees"}])
GOOD = ("Department: QA\nRole: senior QA engineer for life-safety systems; reproducible, severity-ranked findings\n"
        "Head brief: run scenario 1 on the ready-made test instance, write findings as you go, no fixes\nCap: $3\nRead AGENT_STANDARD.md")

def test_gate_accepts_complete_prompt(): assert G.problems(GOOD, C, []) == []
def test_gate_lists_every_missing_line(): assert len(G.problems("do stuff", C, [])) == 5
def test_gate_unknown_department(): assert any("Department" in e for e in G.problems(GOOD.replace("QA", "Vibes", 1), C, []))
def test_gate_short_role_refused(): assert any("Role" in e for e in G.problems(GOOD.replace("senior QA engineer for life-safety systems; reproducible, severity-ranked findings", "tester"), C, []))
def test_gate_one_at_a_time(): assert any("running" in e for e in G.problems(GOOD, C, [{"id": "a1", "state": "running"}]))
def test_gate_parallel_with_owner_ok(): assert G.problems(GOOD + "\nParallel: owner-approved", C, [{"id": "a1", "state": "running"}]) == []
def test_gate_blocks_while_one_is_over_cap(): assert any("over its cap" in e for e in G.problems(GOOD + "\nParallel: owner-approved", C, [{"id": "a1", "state": "running", "over": True}]))

def test_bash_coauthor(): assert B.check("git commit -m 'x\n\nCo-Authored-By: A <a@b.c>'", C)
def test_bash_plain_commit(): assert not B.check("git commit -m fix", C)
def test_bash_keychain(): assert B.check("security find-generic-password -s x -w", C)
def test_bash_chip(): assert B.check("espefuse.py --port /dev/x burn_efuse K", C) and B.check("esptool --chip esp32s3 erase-flash", C)
def test_bash_app_flash_allowed(): assert not B.check("esptool --chip esp32s3 write-flash 0x10000 firmware.bin", C)
def test_bash_heredoc_is_data(): assert not B.check("cat >> notes.md <<'EOF'\nwe never run espefuse or touch APPROTECT\nEOF", C)
def test_bash_deploy_needs_ok(): assert B.check("npx wrangler@4 pages deploy public", C) and not B.check("OWNER_OK=deploy npx wrangler@4 pages deploy public", C)

def test_wording_public_blocked(): assert Wd.hits("/x/site/src/index.html", "Alerts are guaranteed", C)
def test_wording_internal_free(): assert not Wd.hits("/x/docs/notes.md", "guaranteed", C)
