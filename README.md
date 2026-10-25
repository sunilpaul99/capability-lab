# Capability Lab

Find out whether a new Claude skill, agent, or set of rules actually improves *your* Claude, before you install it.

Capability Lab runs the same tasks twice: once with your current setup, once with the new capability added. A separate Claude call compares each pair of answers blind and judges which one serves you better. You get a verdict, the answers side by side, and a one-click install or a shareable pack.

Built for the [Bell AI Fellowship](https://bellaifellow.com) Homebrew AI Club.

## Requirements

- macOS (the launcher is a `.command` file; the server itself is plain Python and runs anywhere)
- Python 3.9 or later (the version that ships with macOS works; no packages to install)
- [Claude Code](https://claude.com/claude-code), signed in to a Claude account. The Claude desktop app includes it; the app's first step finds it and shows you how to sign in.
- `git`, for importing capabilities from GitHub

## Start

```bash
git clone https://github.com/sunilpaul99/capability-lab.git
```

Then double-click **Start Capability Lab** in the folder. It opens in your browser at `http://127.0.0.1:8765`.

Or from Terminal: `python3 server.py`

## How it works

**Home** is where you start: one ranked to-do list that mixes fixes to your setup, capabilities waiting for a keep-or-skip decision, and new things to try, with three tiles (kept from the lab, to do, done). The setup check, what Claude reads, finished items and run history are one click away.

To try a capability step by step:

1. **Connect.** Finds Claude Code and checks that you're signed in.
2. **Your current setup.** Your memory files, CLAUDE.md, voice and style guides: the "before" in each comparison.
3. **Add a new capability.** Pick one of eleven vetted capabilities with **Try it** (Anthropic's skills, obra/superpowers, VoltAgent agents), each with matching test tasks. Or bring your own: a GitHub link (repo, folder, or single file), a local folder, or pasted text.
4. **Pick test tasks.** Real things you ask Claude to do, each with an optional line on what good looks like. You can pull tasks from your past Claude Code sessions.
5. **Run.** Each task runs with and without the capability. You see the worst-case cost before starting.
6. **Results and decision.** Verdict, better/worse/tie counts, whether the capability was actually used, cost, and both answers side by side. Keep or skip it, or export a zip pack with the results to share.

**Check my setup** (on Home): one button that inventories what Claude reads and when, finds mechanical problems (no CLAUDE.md, broken links, pages nothing points to), reads your past Claude Code sessions for what you ask for and where you correct Claude, then asks Claude (one Sonnet call, usually under 40 cents) for a plain assessment and the best few improvements. Each suggestion has a button: test a catalog capability, draft a skill for testing, save a memory note, create a CLAUDE.md, open a file, or copy a prompt. Home also shows what's installed in your Claude now (checked against the files on disk), what's waiting for a decision, every capability's latest result, and every run.

## Safety

- Every test runs in an empty scratch folder with shell, web, and connectors turned off. Imported scripts never execute.
- MCP servers and hooks in an imported repo are listed but cannot be selected, since they run programs on your machine.
- Imported repos are cloned with git hooks disabled.
- The server listens only on `127.0.0.1`.
- Your data (setup choices, tasks, runs, packs) stays in `data/` and `packs/`, which are not committed.

## Limits

- Tests run without your connectors (email, calendar, Sheets), so tasks that need them won't work. Writing and reasoning tasks work well.
- Rules are tested but not installed automatically; the app shows you the text to add to your memory or CLAUDE.md.
- Both sides of a comparison load whatever is already in your `~/.claude` folder, so the test measures what the new capability adds.
- LLM output varies. Use 2 or 3 runs per task and 5 to 10 tasks before trusting a verdict.

## Testing without Claude

`tests/fake_claude.py` stands in for the Claude CLI so you can exercise the whole pipeline offline:

```bash
CLAUDE_CLI=$PWD/tests/fake_claude.py PORT=8799 python3 server.py
```
