#!/usr/bin/env python3
"""Dollar cost of one agent's transcript (Claude Code task .output / .jsonl). Usage: agentcost.py <file>
Prices per million tokens (input, output, cache read): edit PRICES for your models. Cache writes: 1.25x input (5 min), 2x input (1 h).
Each API request is counted once (deduplicated by request id). Prints '$12.34 {model: requests}'."""
import collections, json, sys
PRICES = {"claude-sonnet-5-5": (2, 10, .20), "claude-opus-5-5": (4, 20, .20), "claude-haiku-5-5": (.10, .50, .01), "claude-fable-5-1": (10, 50, .25)}
seen = {}
for line in open(sys.argv[1]):
    try: d = json.loads(line)
    except ValueError: continue
    m = d.get("message")
    if isinstance(m, dict) and m.get("usage"):
        seen[d.get("requestId") or m.get("id") or d.get("uuid")] = (m.get("model"), m["usage"])
tot, n = 0.0, collections.Counter()
for model, u in seen.values():
    if model not in PRICES: print("NO PRICE for", model); continue
    i, o, r = PRICES[model]
    cc = u.get("cache_creation") or {}
    m5, h1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if m5 is None and h1 is None: m5, h1 = 0, u.get("cache_creation_input_tokens", 0) or 0
    tot += ((u.get("input_tokens", 0) or 0) * i + (u.get("output_tokens", 0) or 0) * o + (u.get("cache_read_input_tokens", 0) or 0) * r
            + (m5 or 0) * i * 1.25 + (h1 or 0) * i * 2) / 1e6
    n[model] += 1
print(f"${tot:.2f}", dict(n))
