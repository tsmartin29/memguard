#!/usr/bin/env python3
"""PreToolUse hook for Write|Edit|MultiEdit: claims you never make in public material, enforced (agentgate).
Config: public_paths = [regex on the file path], banned_phrases = [{"rx": regex, "why": reason}]. Files outside public_paths are not checked.
Exit 2 + stderr = block. Internal errors allow."""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agentgate import cfg

def hits(path, text, c):
    if not any(re.search(rx, path) for rx in c.get("public_paths") or []): return []
    return [b["why"] for b in c.get("banned_phrases") or [] if re.search(b["rx"], text, re.I)]

if __name__ == "__main__":
    try:
        ti = json.load(sys.stdin).get("tool_input") or {}
        text = ti.get("content") or ti.get("new_string") or "".join(e.get("new_string", "") for e in ti.get("edits", []) or [])
        h = hits(ti.get("file_path", ""), text, cfg())
    except Exception: sys.exit(0)
    if h: sys.stderr.write("BLOCKED (agentgate wording rule): " + "; ".join(h) + "\n"); sys.exit(2)
    sys.exit(0)
