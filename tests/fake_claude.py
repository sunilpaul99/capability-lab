#!/usr/bin/env python3
"""Stand-in for the claude CLI so the run/judge/report pipeline can be tested offline."""
import json, random, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("0.0.0 (fake)"); sys.exit()
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "fake"})); sys.exit()
prompt = sys.stdin.read()
if "--tools" in args and prompt.startswith("SETUP CHECK"):
    import re as _re
    vault = (_re.findall(r"\[high\] No CLAUDE.md in .*?\((.*?)\)", prompt) or ["~/vault"])[0]
    rep = {"summary": "Fake summary: memory carries your writing rules; nothing points Claude at your procedures.",
           "pieces": [{"name": "MEMORY.md", "what_it_does": "Index of six notes.", "assessment": "Working well."}],
           "opportunities": [
             {"title": "Add a short CLAUDE.md", "why": "No standing instructions.", "evidence": ["No CLAUDE.md"], "impact": "high", "effort": "low",
              "action": {"type": "create_claude_md", "folder": vault, "body": "# Vault\nRead pages/writing-voice.md before drafting prose."}},
             {"title": "Try the discernment nudge", "why": "You act on drafts.", "evidence": ["emails"], "impact": "medium", "effort": "low",
              "action": {"type": "test_catalog", "id": "discernment-nudge"}},
             {"title": "Make a seminar-prose skill", "why": "Repeated workflow.", "evidence": ["Build a prose version of this talk"], "impact": "high", "effort": "medium",
              "action": {"type": "draft_skill", "name": "seminar-prose", "description": "Use when drafting seminar prose", "instructions": "Write readable prose with footnotes."}},
             {"title": "Remember: outline before timing", "why": "You said it twice.", "evidence": ["Don't worry about time yet."], "impact": "low", "effort": "low",
              "action": {"type": "save_memory", "name": "outline-before-timing", "description": "Outline first, timing later", "body": "Outline first.\n\n**Why:** asked.\n\n**How to apply:** skip timing."}},
             {"title": "Fix the broken link", "why": "Dangling.", "evidence": ["lecture-prose-citations.md"], "impact": "low", "effort": "low",
              "action": {"type": "edit_file", "path": "lecture-prose-citations.md", "suggestion": "Remove [[shaping-course-fact-checking]]."}},
             {"title": "Ask Claude to tidy memory", "why": "Overlap.", "evidence": [], "impact": "low", "effort": "low",
              "action": {"type": "ask_claude", "prompt": "Review my memory notes for overlaps."}}]}
    print(json.dumps({"result": json.dumps(rep), "total_cost_usd": 0.12})); sys.exit()
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
