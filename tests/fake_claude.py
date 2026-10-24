#!/usr/bin/env python3
"""Stand-in for the claude CLI so the run/judge/report pipeline can be tested offline."""
import json, random, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("0.0.0 (fake)"); sys.exit()
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "fake"})); sys.exit()
prompt = sys.stdin.read()
if "--tools" in args:  # judge call
    print(json.dumps({"result": json.dumps({"better": random.choice("AAB"), "confidence": "medium",
                      "reason": "fake judgment", "issues_A": [], "issues_B": ["uses an intensifier"]}),
                      "total_cost_usd": 0.002}))
    sys.exit()
with_plugin = "--plugin-dir" in args
if with_plugin:
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "doc-coauthoring:doc-coauthoring"}}]}}))
    open("draft.md", "w").write("# Draft\n\nwritten with the skill")
print(json.dumps({"type": "result", "result": ("WITH: " if with_plugin else "BASE: ") + prompt[:80],
                  "total_cost_usd": 0.03 if with_plugin else 0.02, "num_turns": 2, "is_error": False}))
