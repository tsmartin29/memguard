#!/usr/bin/env python3
"""PreToolUse hook for Bash: written-down rules, enforced (agentgate). All but the first are opt-in through the config.
  no_coauthor      commits carry no Co-Authored-By trailer
  no_keychain      no macOS Keychain reads or writes (security find-*-password, dump-keychain, export, unlock-keychain, add-*)
  no_chip_burn     no irreversible chip operations: espefuse / espsecure, esptool erase, nrfjprog / pyocd / openocd recover, rbp, approtect, uicr, erase
  protected_ports  [ports]: no kill / pkill / killall of the processes listening on them (the owner's own running services)
  deploy_patterns  [regex]: a matching command needs OWNER_OK=deploy in it (typed only after the owner said "deploy")
Heredoc bodies are treated as data. Exit 2 + stderr = block. Internal errors allow."""
import json, os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agentgate import cfg

def pids_on(port):
    try: return set(subprocess.run(["lsof", "-nP", "-tiTCP:%d" % port, "-sTCP:LISTEN"], capture_output=True, text=True, timeout=5).stdout.split())
    except Exception: return set()

def check(cmd, c):
    body = re.sub(r"<<-?\s*'?(\w+)'?.*?\n\1\b", " ", cmd, flags=re.S)
    if c.get("no_coauthor", True) and re.search(r"\bgit\b[^\n]*\bcommit\b", body) and re.search(r"co-authored-by", cmd, re.I):
        return "commits carry no Co-Authored-By trailer"
    if c.get("no_keychain") and re.search(r"\bsecurity\s+(find-(generic|internet|certificate)-password|find-key|dump-keychain|export|unlock-keychain|(add|delete)-(generic|internet)-password)\b", body):
        return "no Keychain access"
    if c.get("no_chip_burn") and re.search(r"(^|[\s;&|/])(espefuse|espsecure)(\.py)?\s|\besptool(\.py)?\b[^\n]*\berase[-_](flash|region)\b|\b(nrfjprog|pyocd|openocd|nrfutil)\b[^\n]*(--recover|--rbp|--memwr|--eraseall|approtect|uicr|erase)", body, re.I):
        return "irreversible chip operation (eFuse, secure boot, flash encryption, erase, APPROTECT, UICR)"
    ports = c.get("protected_ports") or []
    if ports and re.search(r"\b(kill|pkill|killall)\b", body):
        own = set().union(*[pids_on(int(p)) for p in ports])
        if own & set(re.findall(r"\b\d+\b", body)):
            return "that process serves a protected port (%s): ask the owner to stop it" % ", ".join(map(str, ports))
    for rx in c.get("deploy_patterns") or []:
        if re.search(rx, body) and "OWNER_OK=deploy" not in body:
            return "deploys need the owner's 'deploy' first; then put OWNER_OK=deploy in the command"
    return ""

if __name__ == "__main__":
    try: why = check((json.load(sys.stdin).get("tool_input") or {}).get("command", ""), cfg())
    except Exception: sys.exit(0)
    if why: sys.stderr.write("BLOCKED (agentgate rule): " + why + "\n"); sys.exit(2)
    sys.exit(0)
