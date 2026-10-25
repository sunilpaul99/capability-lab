#!/usr/bin/env python3
"""Capability Lab: try a new Claude skill/agent/rule against your current setup.

Runs locally on http://127.0.0.1:8765. Python 3.9 standard library only.
Every test run happens in an empty scratch folder with shell and web tools
turned off, so imported capabilities cannot touch your files or the network.
"""
import glob
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOME = os.path.expanduser("~")
APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("CAPLAB_DATA") or os.path.join(APP, "data")
PACKS = os.path.join(APP, "packs")
PROJECTS = os.environ.get("CAPLAB_PROJECTS") or os.path.join(HOME, ".claude", "projects")  # memory + session history
CLAUDE_DIR = os.environ.get("CAPLAB_CLAUDE_DIR") or os.path.join(HOME, ".claude")  # where Install puts things
PORT = int(os.environ.get("PORT", "8765"))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
CODE_EXT = {".py", ".sh", ".js", ".ts", ".rb", ".go", ".mjs", ".cjs", ".bash", ".zsh"}

os.makedirs(DATA, exist_ok=True)
os.makedirs(PACKS, exist_ok=True)
LOCK = threading.Lock()


# ---------- small helpers ----------

def jload(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def jsave(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def read_text(path, limit=200_000):
    try:
        with open(path, errors="replace") as f:
            return f.read(limit)
    except Exception:
        return ""


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:48] or "capability"


def frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    out = {}
    if m:
        key = None
        for line in m.group(1).splitlines():
            if ":" in line and not line[:1].isspace():
                k, v = line.split(":", 1)
                key, v = k.strip(), v.strip()
                out[key] = "" if v in (">", "|", ">-", "|-", ">+", "|+") else v.strip('"').strip("'")
            elif key and line[:1].isspace():
                out[key] = (out[key] + " " + line.strip()).strip()
    return out


def under_home(path):
    p = os.path.realpath(os.path.expanduser(path))
    return p if p.startswith(HOME + os.sep) else None


# ---------- step 1: connect ----------

def find_cli():
    env = os.environ.get("CLAUDE_CLI")
    cands = [env] if env else []
    cands += [shutil.which("claude") or ""]
    cands += sorted(glob.glob(HOME + "/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"), reverse=True)
    cands += sorted(glob.glob(HOME + "/Library/Application Support/Claude/claude-code-vm/*/claude"), reverse=True)
    cands += [HOME + "/.local/bin/claude", HOME + "/.claude/local/claude"]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def connect_status():
    cli = find_cli()
    if not cli:
        return {"cli": None, "logged_in": False, "message": "Claude Code CLI not found."}
    try:
        ver = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        st = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=20).stdout
        st = json.loads(st) if st.strip().startswith("{") else {}
    except Exception as e:
        return {"cli": cli, "logged_in": False, "message": str(e)}
    logged = bool(st.get("loggedIn")) or bool(os.environ.get("ANTHROPIC_API_KEY"))
    return {"cli": cli, "version": ver, "logged_in": logged, "auth": st.get("authMethod"),
            "login_command": '"%s" auth login' % cli}


# ---------- step 2: your setup (baseline) ----------

def scan_setup():
    """Find the pieces of the user's current configuration."""
    items = []

    def add(kind, path, label=None, default=True):
        items.append({"kind": kind, "path": path, "label": label or path.replace(HOME, "~"),
                      "size": os.path.getsize(path) if os.path.isfile(path) else 0, "default": default})

    for p in [HOME + "/.claude/CLAUDE.md"]:
        if os.path.isfile(p):
            add("instructions", p)
    for mdir in sorted(glob.glob(os.path.join(PROJECTS, "*", "memory"))):
        files = glob.glob(os.path.join(mdir, "*.md"))
        index = os.path.join(mdir, "MEMORY.md")
        order = []
        if os.path.isfile(index):
            add("memory", index, default=True)
            items[-1]["role"] = "index"
            for href in re.findall(r"\]\(([^)#\s]+\.md)\)", read_text(index)):
                p = os.path.normpath(os.path.join(mdir, href))
                if p in files and p not in order:
                    order.append(p)
        for mem in order:
            add("memory", mem)
            items[-1]["role"] = "listed"
        for mem in sorted(f for f in files if f != index and f not in order):
            add("memory", mem)
            items[-1]["role"] = "unlisted" if os.path.isfile(index) else ""
    for sk in glob.glob(HOME + "/.claude/skills/*/SKILL.md"):
        add("skill (always loaded)", sk, default=True)
    for ag in glob.glob(HOME + "/.claude/agents/*.md"):
        add("agent (always loaded)", ag, default=True)
    # Project-level instruction files and likely "rules" pages in known folders.
    for root in jload(os.path.join(DATA, "roots.json"), []):
        for name in ("CLAUDE.md", "AGENTS.md", "SCHEMA.md"):
            p = os.path.join(root, name)
            if os.path.isfile(p):
                add("instructions", p, default=False)
        try:
            os.listdir(root)
        except OSError:
            items.append({"kind": "blocked", "path": root, "label": root.replace(HOME, "~"), "size": 0, "default": False})
            continue
        for p in glob.glob(os.path.join(root, "pages", "*.md")) + glob.glob(os.path.join(root, "*.md")):
            base = os.path.basename(p).lower()
            if re.search(r"(^|-)((writing-)?voice|style(-guide)?|rules|procedures|preferences)\.md$", base):
                add("rules page", p, default=False)
    for p in jload(os.path.join(DATA, "setup.json"), {}).get("extra", []):
        if os.path.isfile(p):
            add("added by you", p, default=True)
    seen, out = set(), []
    for it in items:
        if it["path"] not in seen:
            seen.add(it["path"])
            out.append(it)
    return out


def baseline_context(selected_paths, extra_text=""):
    parts = ["# My standing instructions and preferences\n"
             "These are the user's own rules. Follow them when relevant.\n"]
    for p in selected_paths:
        if "/.claude/skills/" in p or "/.claude/agents/" in p:
            continue  # loaded natively by Claude Code
        txt = read_text(p, 60_000).strip()
        if txt:
            parts.append("\n\n## From %s\n\n%s" % (os.path.basename(p), txt))
    if extra_text.strip():
        parts.append("\n\n## Additional notes\n\n" + extra_text.strip())
    return "".join(parts)


# ---------- step 3: import a capability ----------

def scan_import(src):
    """Walk an imported folder and list skills, agents, commands, rules, and code-running parts."""
    found = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = os.path.relpath(dirpath, src)
        if rel.count(os.sep) > 8:
            continue
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rp = os.path.relpath(p, src)
            low = fn.lower()
            if fn == "SKILL.md":
                fm = frontmatter(read_text(p))
                files = []
                for d2, _, f2 in os.walk(dirpath):
                    files += [os.path.relpath(os.path.join(d2, x), dirpath) for x in f2]
                code = [f for f in files if os.path.splitext(f)[1] in CODE_EXT]
                found.append({"type": "skill", "name": fm.get("name") or os.path.basename(dirpath),
                              "description": fm.get("description", ""), "path": rp, "dir": os.path.relpath(dirpath, src),
                              "files": len(files), "has_code": bool(code),
                              "warning": ("Includes %d script file(s). Shell is off in tests, so scripts will not run." % len(code)) if code else ""})
            elif low.endswith(".md") and fn != "README.md" and (
                    (os.sep + "agents" in os.sep + rel)
                    or (lambda f: f.get("name") and f.get("description") and ("tools" in f or "model" in f))(frontmatter(read_text(p, 4000)))):
                fm = frontmatter(read_text(p))
                if fm.get("description") or fm.get("name"):
                    found.append({"type": "agent", "name": fm.get("name") or fn[:-3],
                                  "description": fm.get("description", ""), "path": rp})
            elif low.endswith(".md") and (os.sep + "commands" in os.sep + rel) and fn != "README.md":
                fm = frontmatter(read_text(p))
                found.append({"type": "command", "name": fn[:-3], "description": fm.get("description", ""), "path": rp})
            elif fn in ("CLAUDE.md", "AGENTS.md"):
                found.append({"type": "rule", "name": "%s (%s)" % (fn, rel if rel != "." else "top level"),
                              "description": read_text(p, 300).strip().replace("\n", " ")[:200], "path": rp})
            elif fn in (".mcp.json", "hooks.json") or (fn == "settings.json" and '"hooks"' in read_text(p, 50_000)):
                found.append({"type": "runs-code", "name": fn, "path": rp, "blocked": True,
                              "description": "MCP servers or hooks run programs on your machine. Not included in v1 tests."})
    for i, f in enumerate(found):
        f["id"] = "i%d" % i
    return found


def normalize_git(value):
    """Accept the link shapes people actually paste: with or without https://, .git, trailing slash,
    ?query or #anchor, and /tree/ or /blob/ links to a folder or a single file in it."""
    v = value.strip().strip('"').strip("'").strip("<>")
    v = re.sub(r"[?#].*$", "", v).rstrip("/")
    if v.startswith("git@github.com:"):
        v = "https://github.com/" + v.split(":", 1)[1]
    if re.match(r"^(www\.)?github\.com/", v):
        v = "https://" + v
    v = v.replace("http://", "https://", 1).replace("://www.github.com/", "://github.com/")
    m = re.match(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/(?:tree|blob)/([^/]+)(?:/(.*))?)?$", v)
    if m:
        owner, repo, branch, sub = m.groups()
        sub = sub or ""
        fname = ""
        if sub and re.search(r"\.[A-Za-z0-9]+$", os.path.basename(sub)):
            sub, fname = os.path.dirname(sub), os.path.basename(sub)  # a link to one file: scan its folder
        return "https://github.com/%s/%s.git" % (owner, repo), sub, branch, fname
    if re.match(r"^https://[\w.-]+/[\w./~-]+$", v):
        return v, "", None, ""  # another git host
    raise ValueError("That doesn't look like a GitHub link. I received: %s" % (value.strip()[:200] or "(nothing)"))


def do_import(body):
    if body.get("kind") == "git":
        normalize_git(body.get("value") or "")  # reject bad links before creating anything
    iid = uuid.uuid4().hex[:8]
    idir = os.path.join(DATA, "imports", iid)
    src = os.path.join(idir, "src")
    os.makedirs(idir)
    kind, value = body.get("kind"), (body.get("value") or "").strip()
    fname = ""
    if kind == "git":
        url, sub, branch, fname = normalize_git(value)
        cmd = ["git", "-c", "core.hooksPath=/dev/null", "clone", "--depth", "1", "--no-recurse-submodules"]
        if branch:
            cmd += ["--branch", branch]
        r = subprocess.run(cmd + [url, src], capture_output=True, text=True, timeout=180, cwd=DATA)
        if r.returncode != 0:
            shutil.rmtree(idir, ignore_errors=True)
            msg = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown error"
            if "not found" in msg.lower() or "could not read" in msg.lower():
                msg = "GitHub couldn't find %s. Check the link, and that the repo is public." % url
            raise ValueError("Download failed: " + msg)
        if sub and not os.path.isdir(os.path.join(src, sub)):
            shutil.rmtree(idir, ignore_errors=True)
            raise ValueError("The repo downloaded, but it has no folder named %s." % sub)
        scan_root = os.path.join(src, sub) if sub else src
    elif kind == "folder":
        p = under_home(value)
        if not p or not os.path.isdir(p):
            raise ValueError("Folder not found (must be inside your home folder).")
        shutil.copytree(p, src, ignore=shutil.ignore_patterns(*SKIP_DIRS))
        scan_root = src
    elif kind == "text":
        ttype = body.get("text_type", "skill")
        name = slug(body.get("name") or "pasted")
        text = value
        if ttype == "skill":
            if not text.startswith("---"):
                desc = body.get("description") or "Use when the user's request matches: " + name.replace("-", " ")
                text = "---\nname: %s\ndescription: %s\n---\n\n%s" % (name, desc, text)
            os.makedirs(os.path.join(src, "skills", name))
            open(os.path.join(src, "skills", name, "SKILL.md"), "w").write(text)
        elif ttype == "agent":
            if not text.startswith("---"):
                text = "---\nname: %s\ndescription: %s\n---\n\n%s" % (name, body.get("description") or name, text)
            os.makedirs(os.path.join(src, "agents"))
            open(os.path.join(src, "agents", name + ".md"), "w").write(text)
        else:
            os.makedirs(src)
            open(os.path.join(src, "CLAUDE.md"), "w").write(text)
        scan_root = src
    else:
        raise ValueError("Unknown import kind")
    items = scan_import(scan_root)
    if kind == "git" and fname and fname != "SKILL.md":
        # A link to one agent or command file: offer just that file.
        items = [i for i in items if os.path.basename(i["path"]) == fname] or items
    meta = {"id": iid, "kind": kind, "source": value if kind != "text" else "(pasted %s)" % body.get("text_type"),
            "root": scan_root, "items": items, "created": time.strftime("%Y-%m-%d %H:%M")}
    jsave(os.path.join(idir, "meta.json"), meta)
    return meta


def build_candidate(body):
    meta = jload(os.path.join(DATA, "imports", body["import_id"], "meta.json"), None)
    if not meta:
        raise ValueError("Import not found")
    chosen = [i for i in meta["items"] if i["id"] in set(body.get("selected", [])) and not i.get("blocked")]
    if not chosen:
        raise ValueError("Pick at least one item.")
    name = slug(body.get("name") or chosen[0]["name"])
    cid = name + "-" + uuid.uuid4().hex[:4]
    cdir = os.path.join(DATA, "candidates", cid)
    plug = os.path.join(cdir, "plugin")
    os.makedirs(os.path.join(plug, ".claude-plugin"))
    jsave(os.path.join(plug, ".claude-plugin", "plugin.json"),
          {"name": name, "version": "0.1.0", "description": "Imported by Capability Lab from " + meta["source"]})
    rules, skills, agents = [], [], []
    root = meta["root"]
    for it in chosen:
        p = os.path.join(root, it["path"])
        if it["type"] == "skill":
            sname = slug(it["name"])
            shutil.copytree(os.path.join(root, it["dir"]), os.path.join(plug, "skills", sname))
            skills.append(sname)
        elif it["type"] == "agent":
            os.makedirs(os.path.join(plug, "agents"), exist_ok=True)
            shutil.copy(p, os.path.join(plug, "agents", os.path.basename(p)))
            agents.append(it["name"])
        elif it["type"] == "command":
            os.makedirs(os.path.join(plug, "commands"), exist_ok=True)
            shutil.copy(p, os.path.join(plug, "commands", os.path.basename(p)))
        elif it["type"] == "rule":
            rules.append(read_text(p, 60_000))
    open(os.path.join(cdir, "rules.md"), "w").write("\n\n".join(rules))
    cmeta = {"id": cid, "name": name, "source": meta["source"], "items": chosen,
             "skills": skills, "agents": agents, "has_rules": bool(rules), "created": time.strftime("%Y-%m-%d %H:%M"),
             "status": "testing", "history": [{"status": "testing", "at": time.strftime("%Y-%m-%d %H:%M")}]}
    jsave(os.path.join(cdir, "meta.json"), cmeta)
    return cmeta


A = "https://github.com/anthropics/skills/tree/main/skills/"
SP = "https://github.com/obra/superpowers/tree/main/skills/"
VA = "https://github.com/VoltAgent/awesome-claude-code-subagents/blob/main/categories/"
CATALOG = [
    {"group": "Writing and communication", "id": "doc-coauthoring", "title": "Document co-author", "by": "Anthropic", "kind": "skill",
     "link": A + "doc-coauthoring",
     "what": "Writes proposals, plans and decision docs with you: gathers context first, drafts section by section, then checks the doc reads well to someone new.",
     "good_for": "Program proposals, funder memos, the operating procedures.",
     "tasks": [{"prompt": "Help me write a one-page proposal to run a winter cohort of the Homebrew AI Club for 15 people.", "rubric": "Clear purpose, audience, format, dates and cost; asks for missing facts instead of inventing them; plain voice."},
               {"prompt": "Draft a decision memo for my co-founder: should the fellowship keep $180 per path or move to a sliding scale?", "rubric": "States the decision, options, trade-offs and a recommendation in under a page; no invented data."}]},
    {"group": "Writing and communication", "id": "internal-comms", "title": "Team updates and newsletters", "by": "Anthropic", "kind": "skill",
     "link": A + "internal-comms",
     "what": "House formats for status updates, newsletters, FAQs and leadership updates.",
     "good_for": "Weekly notes to Tenni and Jamie, participant updates.",
     "tasks": [{"prompt": "Write a short weekly update to the Bell AI team (Tenni and Jamie): 3 new applicants, the Thursday session moves to 5:30, and two payments still to chase.", "rubric": "Scannable, every item has an owner or next step, under 150 words."},
               {"prompt": "Write a mid-term newsletter to fellowship participants: what's coming in weeks 4 to 6 and one ask for feedback.", "rubric": "Warm and specific, one clear ask, no hype words."}]},
    {"group": "Thinking and judgment", "id": "discernment-nudge", "title": "Discernment nudge", "by": "Anthropic", "kind": "skill",
     "link": A + "discernment-nudge",
     "what": "After a draft, advice or estimate you might act on, adds a short note on what to double-check first.",
     "good_for": "Fits the fellowship's judgment theme; a safety net on emails and estimates.",
     "tasks": [{"prompt": "Estimate how many applicants we need to fill four cohorts of 12 if about 60% of applicants enroll. Then draft a two-line note to Tenni with the number.", "rubric": "Correct arithmetic (about 80), states its assumptions, flags what to verify."},
               {"prompt": "Draft an email asking past participants to each recommend the fellowship to two friends.", "rubric": "Short, personal, one clear ask; points out anything I should check before sending."}]},
    {"group": "Thinking and judgment", "id": "brainstorming", "title": "Brainstorming before planning", "by": "obra/superpowers", "kind": "skill",
     "link": SP + "brainstorming",
     "what": "Before any creative work or plan, asks questions one at a time and lays out options before settling on one. Meant for talks, programs and businesses as well as software.",
     "good_for": "New paths, workshop designs, talks.",
     "tasks": [{"prompt": "I want to add a spring path to the fellowship. Help me plan it.", "rubric": "Asks about goals and audience before proposing; offers two or three distinct options with trade-offs."},
               {"prompt": "Help me design a 90-minute workshop on how to judge AI output.", "rubric": "Clarifies audience first; timed outline; at least one hands-on exercise."}]},
    {"group": "Thinking and judgment", "id": "first-principles", "title": "First-principles thinker", "by": "VoltAgent", "kind": "agent",
     "link": VA + "10-research-analysis/first-principles-thinking.md",
     "what": "A helper agent that breaks a problem down to basics and rebuilds the answer, instead of reasoning from habit.",
     "good_for": "Pricing, program format, strategy questions.",
     "tasks": [{"prompt": "Should the fellowship stay small and in person, or grow online? Think it through from first principles.", "rubric": "Separates facts from assumptions; reaches a reasoned recommendation; names what would change it."}]},
    {"group": "Thinking and judgment", "id": "assumption-mapping", "title": "Assumption mapper", "by": "VoltAgent", "kind": "agent",
     "link": VA + "08-business-product/assumption-mapping.md",
     "what": "A helper agent that lists the riskiest assumptions in an idea and what to test first.",
     "good_for": "New programs and ventures before you commit.",
     "tasks": [{"prompt": "Map the riskiest assumptions in a plan to run the Homebrew AI Club as a paid online course for 100 people.", "rubric": "Ranks assumptions by risk and uncertainty; proposes a cheap test for the top two."}]},
    {"group": "Design", "id": "frontend-design", "title": "Distinctive web design", "by": "Anthropic", "kind": "skill",
     "link": A + "frontend-design",
     "what": "Guidance for web pages with a clear visual point of view rather than generic templates.",
     "good_for": "bellaifellow.com pages, event pages.",
     "tasks": [{"prompt": "Write a one-section landing page announcing a winter Homebrew AI Club cohort. Save it as index.html.", "rubric": "Looks considered rather than templated; readable on a phone; one clear call to action."}]},
    {"group": "Design", "id": "theme-factory", "title": "Ready-made themes", "by": "Anthropic", "kind": "skill",
     "link": A + "theme-factory",
     "what": "Ten color-and-font themes for slides, docs and pages, or a new one made to order.",
     "good_for": "Seminar handouts, one-pagers.",
     "tasks": [{"prompt": "Make a one-page HTML handout for session 1 of How Society Shapes Technology, with a clean theme. Save it as handout.html.", "rubric": "Consistent fonts and colors; easy to read printed; content stays the focus."}]},
    {"group": "For builders (Homebrew AI Club)", "id": "skill-creator", "title": "Skill creator", "by": "Anthropic", "kind": "skill",
     "link": A + "skill-creator",
     "what": "Helps you write a new skill and tighten its description so it fires at the right time.",
     "good_for": "Fellows turning their own workflow into a skill.",
     "note": "Some of its steps run programs, which tests switch off, so expect a partial picture.",
     "tasks": [{"prompt": "Help me write a skill that drafts acceptance emails for the Bell AI Fellowship in my voice.", "rubric": "Produces a SKILL.md with a specific description and clear steps; asks for voice samples."}]},
    {"group": "For builders (Homebrew AI Club)", "id": "systematic-debugging", "title": "Systematic debugging", "by": "obra/superpowers", "kind": "skill",
     "link": SP + "systematic-debugging",
     "what": "Finds the root cause of a bug before proposing a fix, instead of guessing.",
     "good_for": "Fellows building their own tools.",
     "tasks": [{"prompt": "This Python function should return the median of a list but is wrong for even-length lists:\n\ndef median(xs):\n    xs = sorted(xs)\n    return xs[len(xs)//2]\n\nFind the cause and fix it.", "rubric": "Explains the cause, fixes the even case correctly, mentions the empty-list edge case."}]},
    {"group": "For builders (Homebrew AI Club)", "id": "writing-plans", "title": "Plan before building", "by": "obra/superpowers", "kind": "skill",
     "link": SP + "writing-plans",
     "what": "Turns a goal into a step-by-step build plan before any code is written.",
     "good_for": "Fellows' projects; turning an idea into tasks.",
     "tasks": [{"prompt": "I want a small tool that emails me each morning with new fellowship applicants from a Google Sheet. Write the build plan.", "rubric": "Ordered small steps, each checkable; names the pieces needed; no code yet."}]},
]


def quick_add(body):
    entry = next((c for c in CATALOG if c["id"] == body.get("id")), None)
    if not entry:
        raise ValueError("Unknown catalog item")
    meta = do_import({"kind": "git", "value": entry["link"]})
    usable = [i["id"] for i in meta["items"] if not i.get("blocked")]
    if not usable:
        raise ValueError("Nothing usable was found at that link.")
    cand = build_candidate({"import_id": meta["id"], "selected": usable, "name": entry["id"]})
    tasks = jload(os.path.join(DATA, "tasks.json"), [])
    have = {t["prompt"] for t in tasks}
    added = 0
    for t in entry.get("tasks", []):
        if t["prompt"] not in have:
            tasks.append({"id": "t" + uuid.uuid4().hex[:6], "prompt": t["prompt"], "rubric": t["rubric"],
                          "enabled": True, "from": entry["id"]})
            added += 1
    jsave(os.path.join(DATA, "tasks.json"), tasks)
    return {"candidate": cand, "tasks_added": added}


def list_candidates():
    out = []
    for m in sorted(glob.glob(os.path.join(DATA, "candidates", "*", "meta.json")), key=os.path.getmtime, reverse=True):
        c = jload(m, {})
        c.setdefault("status", "testing")
        cdir = os.path.dirname(m)
        files = sorted(glob.glob(os.path.join(cdir, "plugin", "**", "*.md"), recursive=True))
        if read_text(os.path.join(cdir, "rules.md")).strip():
            files.append(os.path.join(cdir, "rules.md"))
        c["files"] = [{"path": f, "label": os.path.relpath(f, os.path.join(cdir, "plugin")) if "/plugin/" in f else "rules.md"} for f in files]
        out.append(c)
    return out


READABLE = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml"}


def resolve_links(src, links):
    """Map links found in a file to real files: relative paths and Obsidian [[wiki links]]."""
    src = under_home(src or "")
    if not src:
        return []
    here = os.path.dirname(src)
    roots = [here, os.path.dirname(here)] + jload(os.path.join(DATA, "roots.json"), [])
    out = []
    for ln in links[:300]:
        target = (ln.get("target") or "").split("#")[0].split("|")[0].strip()
        found = None
        if target and ln.get("kind") == "rel":
            from urllib.parse import unquote
            p = os.path.normpath(os.path.join(here, unquote(target)))
            found = p if os.path.isfile(p) else None
        elif target:
            name = target if target.endswith(".md") else target + ".md"
            for root in roots:
                for c in (os.path.join(root, name), os.path.join(root, "pages", name)):
                    if os.path.isfile(c):
                        found = c
                        break
                if found:
                    break
            if not found:
                # Memory-style links use the file's frontmatter name, which can differ from its filename.
                for c in glob.glob(os.path.join(here, "*.md")):
                    if frontmatter(read_text(c, 4000)).get("name") == target:
                        found = c
                        break
            if not found:
                for root in roots[2:]:
                    hits = glob.glob(os.path.join(root, "**", os.path.basename(name)), recursive=True)
                    if hits:
                        found = hits[0]
                        break
        ok = found and under_home(found) and os.path.splitext(found)[1].lower() in READABLE
        out.append(os.path.realpath(found) if ok else None)
    return out


def read_file(path):
    p = under_home(path or "")
    if not p or not os.path.isfile(p) or os.path.splitext(p)[1].lower() not in READABLE:
        raise ValueError("Can't open that file.")
    return {"path": p, "label": p.replace(HOME, "~"), "text": read_text(p, 400_000), "mtime": os.path.getmtime(p),
            "editable": p.lower().endswith((".md", ".markdown")) and os.access(p, os.W_OK)}


def save_file(body):
    """Write an edited markdown file, refusing if it changed on disk since it was opened. Keeps a backup."""
    p = under_home(body.get("path") or "")
    if not p or not os.path.isfile(p) or not p.lower().endswith((".md", ".markdown")):
        raise ValueError("Only existing markdown files can be edited.")
    if abs(os.path.getmtime(p) - float(body.get("mtime") or 0)) > 0.001:
        raise ValueError("This file changed on disk since you opened it (Obsidian or Claude may have edited it). "
                         "Close and reopen it, then make your edit again.")
    bdir = os.path.join(DATA, "backups", time.strftime("%Y-%m-%d"))
    os.makedirs(bdir, exist_ok=True)
    backup = os.path.join(bdir, time.strftime("%H%M%S-") + os.path.basename(p))
    shutil.copy2(p, backup)
    tmp = p + ".capability-lab.tmp"
    with open(tmp, "w") as f:
        f.write(body.get("text", ""))
    os.replace(tmp, p)
    return {"ok": True, "backup": backup.replace(HOME, "~"), "mtime": os.path.getmtime(p)}


# ---------- step 4: tasks ----------

BELL_STARTERS = [
    {"prompt": "Draft a short LinkedIn post announcing a new Homebrew AI Club cohort for people who want to build their own AI tools. Under 150 words.",
     "rubric": "Plain voice, no hype words or intensifiers, at most one em-dash, a concrete reason to join, a clear next step."},
    {"prompt": "Write a warm two-paragraph acceptance email to an applicant admitted to the AI and Civilization seminar. Tuition is $180; mention that financial assistance is available. Sign as the program director.",
     "rubric": "Specific and warm without gushing; includes price, what happens next, and who to contact; no 'not X, but Y' constructions."},
    {"prompt": "Summarize the main argument of the Asilomar 1975 recombinant DNA conference in 200 words for a seminar handout, with sources.",
     "rubric": "Accurate, cites sources with URLs, readable by a non-specialist, no invented facts."},
    {"prompt": "I have 40 minutes before a seminar. Give me a plan for the opening 10 minutes of a discussion on whether AI should be governed like nuclear technology.",
     "rubric": "Practical, timed, includes one opening question that invites disagreement, fits 10 minutes."},
]


def suggest_tasks(limit=40):
    rows = []
    files = sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")), key=os.path.getmtime, reverse=True)
    for f in files[:200]:
        try:
            with open(f, errors="replace") as fh:
                for n, line in enumerate(fh):
                    if n > 400:
                        break
                    if '"type":"user"' not in line:
                        continue
                    d = json.loads(line)
                    c = d.get("message", {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                    if not isinstance(c, str):
                        continue
                    c = c.strip()
                    if 15 <= len(c) <= 1500 and not c.startswith("<") and "tool_result" not in c:
                        rows.append({"prompt": c, "from": time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(f)))})
                        break
        except Exception:
            continue
        if len(rows) >= limit:
            break
    return {"history": rows, "starters": BELL_STARTERS}


# ---------- step 5: run ----------

RUNS = {}  # run_id -> live progress


def parse_stream(stdout, cand):
    tools, used, final = [], [], {}
    names = set(cand.get("skills", [])) | set(slug(a) for a in cand.get("agents", []))
    for line in stdout.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "assistant":
            for c in d.get("message", {}).get("content", []) or []:
                if c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    tag = c.get("name")
                    target = inp.get("skill") or inp.get("command") or inp.get("subagent_type") or ""
                    if target:
                        tag += ":" + str(target)
                    tools.append(tag)
                    short = slug(str(target).split(":")[-1]) if target else ""
                    if short and short in names:
                        used.append(target)
        elif d.get("type") == "result":
            final = d
    return tools, used, final


def run_one(cli, task, variant, rep, cand, ctx_path, cfg, run_dir):
    wd = os.path.join(run_dir, "work", "%s_%s_%d" % (task["id"], variant, rep))
    os.makedirs(wd, exist_ok=True)
    cmd = [cli, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--permission-mode", "acceptEdits",
           "--allowedTools", "Read,Write,Edit,Glob,Grep,Skill,Agent,TodoWrite",
           "--disallowedTools", "Bash,WebFetch,WebSearch" if not cfg.get("allow_web") else "Bash",
           "--append-system-prompt-file", ctx_path,
           "--max-budget-usd", str(cfg.get("budget", 1.0))]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    if variant == "with":
        cmd += ["--plugin-dir", os.path.join(DATA, "candidates", cand["id"], "plugin")]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, input=task["prompt"], capture_output=True, text=True, cwd=wd,
                           timeout=cfg.get("timeout", 600))
        tools, used, final = parse_stream(p.stdout, cand)
        err = None if final and not final.get("is_error") else (final.get("result") or p.stderr[-500:] or "failed")
    except subprocess.TimeoutExpired:
        tools, used, final, err = [], [], {}, "timed out"
    # Any files the run wrote count as part of its answer.
    files = {}
    for d2, _, fs in os.walk(wd):
        for fn in fs:
            fp = os.path.join(d2, fn)
            files[os.path.relpath(fp, wd)] = read_text(fp, 20_000)
    return {"task_id": task["id"], "variant": variant, "rep": rep, "output": final.get("result", ""),
            "files": files, "tools": tools, "used_capability": used, "error": err,
            "cost": final.get("total_cost_usd") or 0, "turns": final.get("num_turns"),
            "seconds": round(time.time() - t0, 1)}


JUDGE_PROMPT = """You are judging two answers to the same request, written for a specific user.
Judge which answer better serves this user, using the task rubric and the user's standing preferences.
Ignore length unless it matters to the task. Do not favor an answer because it is first.

## User's standing preferences (excerpt)
{prefs}

## The request
{task}

## Rubric (what good looks like)
{rubric}

## Answer A
{a}

## Answer B
{b}

Reply with ONLY a JSON object:
{{"better": "A" or "B" or "tie", "confidence": "low" or "medium" or "high",
  "reason": "one or two sentences", "issues_A": ["..."], "issues_B": ["..."]}}"""


def render_answer(r):
    s = r["output"] or ""
    for k, v in r["files"].items():
        s += "\n\n[file written: %s]\n%s" % (k, v[:6000])
    if r["error"]:
        s += "\n\n[run error: %s]" % r["error"]
    return s[:24_000]


def judge(cli, task, base, withc, prefs, cfg):
    flip = random.random() < 0.5
    a, b = (withc, base) if flip else (base, withc)
    prompt = JUDGE_PROMPT.format(prefs=prefs[:12_000], task=task["prompt"], rubric=task.get("rubric") or "(none given)",
                                 a=render_answer(a), b=render_answer(b))
    cmd = [cli, "-p", "--output-format", "json", "--no-session-persistence", "--tools", "",
           "--max-budget-usd", "0.5"]
    if cfg.get("judge_model"):
        cmd += ["--model", cfg["judge_model"]]
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300, cwd=DATA)
        res = json.loads(p.stdout).get("result", "")
        m = re.search(r"\{.*\}", res, re.S)
        v = json.loads(m.group(0)) if m else {}
        cost = json.loads(p.stdout).get("total_cost_usd") or 0
    except Exception as e:
        v, cost = {"better": "tie", "reason": "judge failed: %s" % e}, 0
    raw = (v.get("better") or "tie").strip().upper()
    if raw in ("A", "B"):
        winner = "with" if (raw == "A") == flip else "baseline"
    else:
        winner = "tie"
    ia, ib = v.get("issues_A", []), v.get("issues_B", [])
    return {"task_id": task["id"], "rep": base["rep"], "winner": winner, "confidence": v.get("confidence"),
            "reason": v.get("reason", ""), "issues_with": ia if flip else ib, "issues_baseline": ib if flip else ia,
            "cost": cost}


def summarize(results, judgments, cand):
    wins = sum(1 for j in judgments if j["winner"] == "with")
    losses = sum(1 for j in judgments if j["winner"] == "baseline")
    ties = len(judgments) - wins - losses

    def avg(var, key):
        xs = [r[key] or 0 for r in results if r["variant"] == var]
        return round(sum(xs) / len(xs), 4) if xs else 0

    w = [r for r in results if r["variant"] == "with"]
    trig = sum(1 for r in w if r["used_capability"])
    n = len(judgments) or 1
    net = (wins - losses) / n
    if not judgments:
        verdict = "No comparisons completed."
    elif net >= 0.25:
        verdict = "Worth keeping: answers were better with it more often than without."
    elif net <= -0.25:
        verdict = "Better to skip: your current setup did better without it."
    else:
        verdict = "No clear difference. Try different tasks, or skip it."
    trigger_note = ""
    if cand.get("skills") or cand.get("agents"):
        trigger_note = "%d of %d runs actually used it." % (trig, len(w))
        if w and trig == 0:
            trigger_note += " It never fired; its description may not match these tasks."
    return {"wins": wins, "losses": losses, "ties": ties, "verdict": verdict, "trigger": trigger_note,
            "cost_baseline": avg("baseline", "cost"), "cost_with": avg("with", "cost"),
            "secs_baseline": avg("baseline", "seconds"), "secs_with": avg("with", "seconds"),
            "errors": sum(1 for r in results if r["error"])}


def start_run(body):
    st = connect_status()
    if not st.get("logged_in"):
        raise ValueError("Step 1 first: Claude is not signed in.")
    cand = jload(os.path.join(DATA, "candidates", body["candidate_id"], "meta.json"), None)
    if not cand:
        raise ValueError("Pick a capability in step 3.")
    tasks = [t for t in jload(os.path.join(DATA, "tasks.json"), []) if t.get("enabled", True)]
    if not tasks:
        raise ValueError("Add at least one task in step 4.")
    setup = jload(os.path.join(DATA, "setup.json"), {"selected": [], "notes": ""})
    rid = time.strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(DATA, "runs", rid)
    os.makedirs(run_dir)
    base_ctx = baseline_context(setup.get("selected", []), setup.get("notes", ""))
    cand_rules = read_text(os.path.join(DATA, "candidates", cand["id"], "rules.md"))
    open(os.path.join(run_dir, "ctx_baseline.md"), "w").write(base_ctx)
    open(os.path.join(run_dir, "ctx_with.md"), "w").write(
        base_ctx + ("\n\n# Additional rules being tested\n\n" + cand_rules if cand_rules.strip() else ""))
    cfg = {"reps": max(1, min(5, int(body.get("reps", 1)))), "model": body.get("model") or "",
           "judge_model": body.get("judge_model") or "", "budget": float(body.get("budget", 1.0)),
           "allow_web": bool(body.get("allow_web"))}
    jsave(os.path.join(run_dir, "config.json"), {"candidate": cand, "tasks": tasks, "cfg": cfg, "started": rid})
    jobs = [(t, v, r) for t in tasks for r in range(cfg["reps"]) for v in ("baseline", "with")]
    RUNS[rid] = {"id": rid, "total": len(jobs) + len(tasks) * cfg["reps"], "done": 0, "state": "running", "log": []}
    threading.Thread(target=_run_thread, args=(st["cli"], rid, run_dir, cand, tasks, jobs, cfg, base_ctx), daemon=True).start()
    return RUNS[rid]


def _run_thread(cli, rid, run_dir, cand, tasks, jobs, cfg, prefs):
    live = RUNS[rid]
    results = []

    def one(job):
        t, v, r = job
        ctx = os.path.join(run_dir, "ctx_%s.md" % v)
        res = run_one(cli, t, v, r, cand, ctx, cfg, run_dir)
        with LOCK:
            results.append(res)
            live["done"] += 1
            live["log"].append("%s · %s · run %d%s" % (t["prompt"][:50], v, r + 1, " (error)" if res["error"] else ""))
            jsave(os.path.join(run_dir, "results.json"), results)
        return res

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, jobs))
    judgments = []
    tmap = {t["id"]: t for t in tasks}
    pairs = []
    for t in tasks:
        for r in range(cfg["reps"]):
            b = next((x for x in results if x["task_id"] == t["id"] and x["rep"] == r and x["variant"] == "baseline"), None)
            w = next((x for x in results if x["task_id"] == t["id"] and x["rep"] == r and x["variant"] == "with"), None)
            if b and w:
                pairs.append((tmap[t["id"]], b, w))

    def j(pair):
        res = judge(cli, pair[0], pair[1], pair[2], prefs, cfg)
        with LOCK:
            judgments.append(res)
            live["done"] += 1
            live["log"].append("judged: %s → %s" % (pair[0]["prompt"][:50], res["winner"]))
        return res

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(j, pairs))
    summary = summarize(results, judgments, cand)
    jsave(os.path.join(run_dir, "judgments.json"), judgments)
    jsave(os.path.join(run_dir, "summary.json"), summary)
    live["state"] = "done"


def run_report(rid):
    d = os.path.join(DATA, "runs", rid)
    cfg = jload(os.path.join(d, "config.json"), {})
    return {"id": rid, "config": cfg, "results": jload(os.path.join(d, "results.json"), []),
            "judgments": jload(os.path.join(d, "judgments.json"), []),
            "summary": jload(os.path.join(d, "summary.json"), None),
            "live": RUNS.get(rid)}


def list_runs():
    out = []
    for c in sorted(glob.glob(os.path.join(DATA, "runs", "*", "config.json")), reverse=True):
        rid = os.path.basename(os.path.dirname(c))
        cfg = jload(c, {})
        s = jload(os.path.join(os.path.dirname(c), "summary.json"), None)
        c0 = cfg.get("candidate", {})
        live = RUNS.get(rid) or {}
        out.append({"id": rid, "candidate": c0.get("name"), "candidate_id": c0.get("id"), "tasks": len(cfg.get("tasks", [])),
                    "reps": cfg.get("cfg", {}).get("reps", 1), "model": cfg.get("cfg", {}).get("model") or "default",
                    "summary": s, "state": live.get("state", "done" if s else "stopped"),
                    "progress": [live.get("done"), live.get("total")] if live else None})
    return out


def dashboard():
    cands = list_candidates()
    runs = list_runs()
    by_cand = {}
    for r in runs:  # newest first
        by_cand.setdefault(r["candidate_id"], []).append(r)
    for c in cands:
        rs = by_cand.get(c["id"], [])
        c["runs"] = len(rs)
        done = [r for r in rs if r["summary"]]
        c["latest"] = {"run_id": done[0]["id"], **done[0]["summary"]} if done else None
        c["running"] = any(r["state"] == "running" for r in rs)
        if c.get("status") == "installed":
            c["files_ok"] = all(os.path.exists(p) for p in c.get("installed_paths", [])) if c.get("installed_paths") else True
    owned = {p for c in cands for p in c.get("installed_paths", [])}
    outside = []
    for sk in sorted(glob.glob(os.path.join(CLAUDE_DIR, "skills", "*", "SKILL.md"))):
        d = os.path.dirname(sk)
        if d not in owned:
            outside.append({"type": "skill", "name": os.path.basename(d), "path": sk})
    for ag in sorted(glob.glob(os.path.join(CLAUDE_DIR, "agents", "*.md"))):
        if ag not in owned:
            outside.append({"type": "agent", "name": os.path.basename(ag)[:-3], "path": ag})
    synced = sorted({os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(CLAUDE_DIR, "skills", "synced", "*", "*", "SKILL.md"))})
    return {"candidates": cands, "runs": runs, "outside": outside, "synced": synced,
            "claude_dir": CLAUDE_DIR.replace(HOME, "~"), "fixes": fixes_status()}


FIX_TYPES = {"edit_file", "create_claude_md", "save_memory", "ask_claude"}


def fixes_status():
    """Fixes = repairs to the existing setup: live mechanical issues plus fix-type suggestions, counted once."""
    inv = inventory()  # cheap and local, so always current
    live = inv["issues"]
    files = sorted(glob.glob(os.path.join(CHECKS, "*", "check.json")), reverse=True)
    chk = jload(files[0], None) if files else None
    ops = (chk or {}).get("report", {}).get("opportunities", [])

    def target(o):
        a = o.get("action") or {}
        return a.get("path") or a.get("folder") or ""
    fix_ops = [(i, o) for i, o in enumerate(ops) if (o.get("action") or {}).get("type") in FIX_TYPES]
    covered = {target(o) for _, o in fix_ops if target(o)}
    open_items, done_items = [], []
    for i, o in fix_ops:
        item = {"kind": "suggestion", "index": i, "title": o.get("title"), "impact": o.get("impact"), "path": target(o)}
        if o.get("state") == "done":
            done_items.append(item)
        elif o.get("state") == "open":
            open_items.append(item)
    for x in live:
        if x["path"] not in covered:
            open_items.append({"kind": "issue", "title": x["title"], "detail": x["detail"], "severity": x["severity"], "path": x["path"]})
    if chk:
        live_titles = {x["title"] for x in live}
        done_titles = {d["title"] for d in done_items}
        for x in chk["inventory"]["issues"]:
            if x["title"] not in live_titles and x["title"] not in done_titles and x["path"] not in covered:
                done_items.append({"kind": "issue", "title": x["title"], "detail": "Resolved since the check on %s." % chk["id"][:8], "path": x["path"]})
    return {"open": open_items, "done": done_items, "live_issues": live,
            "inv": {"always": len(inv["always"]), "on_demand": len(inv["on_demand"])}}


# ---------- step 6: adopt / share ----------

def set_status(cand, status, **extra):
    cand["status"] = status
    cand.setdefault("history", []).append({"status": status, "at": time.strftime("%Y-%m-%d %H:%M"),
                                           **({"run_id": extra["run_id"]} if extra.get("run_id") else {})})
    for k, v in extra.items():
        if k != "run_id":
            cand[k] = v
    cand["decided"] = time.strftime("%Y-%m-%d %H:%M")
    jsave(os.path.join(DATA, "candidates", cand["id"], "meta.json"), cand)


def adopt(body):
    cid = body["candidate_id"]
    cdir = os.path.join(DATA, "candidates", cid)
    cand = jload(os.path.join(cdir, "meta.json"), None)
    if not cand:
        raise ValueError("Capability not found")
    if body.get("mode") == "pack":
        zp = os.path.join(PACKS, cand["name"] + ".zip")
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for d2, _, fs in os.walk(cdir):
                for fn in fs:
                    fp = os.path.join(d2, fn)
                    z.write(fp, os.path.relpath(fp, cdir))
            rid = body.get("run_id")
            if rid:
                for fn in ("summary.json", "judgments.json", "config.json"):
                    fp = os.path.join(DATA, "runs", rid, fn)
                    if os.path.isfile(fp):
                        z.write(fp, "test-results/" + fn)
            z.writestr("README.md", "# %s\n\nCapability pack from Capability Lab.\nSource: %s\n\n"
                       "Install: unzip, then in Claude Code run with `--plugin-dir plugin`, or copy plugin/skills/* "
                       "to ~/.claude/skills/.\nTest results (if any) are in test-results/.\n" % (cand["name"], cand["source"]))
        return {"ok": True, "path": zp}
    if body.get("mode") == "install":
        if cand.get("status") == "installed":
            raise ValueError("Already installed.")
        plug = os.path.join(cdir, "plugin")
        plan = []
        for sk in glob.glob(os.path.join(plug, "skills", "*")):
            plan.append((sk, os.path.join(CLAUDE_DIR, "skills", os.path.basename(sk)), "dir"))
        for ag in glob.glob(os.path.join(plug, "agents", "*.md")):
            plan.append((ag, os.path.join(CLAUDE_DIR, "agents", os.path.basename(ag)), "file"))
        clash = [d for _, d, _ in plan if os.path.exists(d)]
        if clash:  # check everything before copying anything, so a clash never leaves a half install
            raise ValueError("Already exists, so nothing was installed: " + ", ".join(c.replace(HOME, "~") for c in clash))
        done = []
        for src, dest, kind in plan:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            (shutil.copytree if kind == "dir" else shutil.copy)(src, dest)
            done.append(dest)
        rules = read_text(os.path.join(cdir, "rules.md"))
        set_status(cand, "installed", run_id=body.get("run_id"), installed_paths=done)
        return {"ok": True, "installed": [d.replace(HOME, "~") for d in done], "rules_to_copy": rules, "candidate": cand}
    if body.get("mode") == "uninstall":
        removed = []
        for d in cand.get("installed_paths", []):
            # Only remove what this app installed, and only inside ~/.claude/skills or ~/.claude/agents.
            ok_root = any(d.startswith(os.path.join(CLAUDE_DIR, x) + os.sep) for x in ("skills", "agents"))
            if ok_root and os.path.lexists(d):
                (shutil.rmtree if os.path.isdir(d) else os.remove)(d)
                removed.append(d.replace(HOME, "~"))
        set_status(cand, "uninstalled", installed_paths=[])
        return {"ok": True, "removed": removed, "candidate": cand}
    if body.get("mode") in ("reject", "reconsider"):
        if cand.get("status") == "installed":
            raise ValueError("Uninstall it first.")
        set_status(cand, "rejected" if body["mode"] == "reject" else "testing", run_id=body.get("run_id"))
        return {"ok": True, "candidate": cand}
    raise ValueError("Unknown mode")


# ---------- setup check: inventory, evidence, review ----------

CHECKS = os.path.join(DATA, "checks")
CHECK_LIVE = {}
CORRECTION_RE = re.compile(r"(^|\b)(no[,.!]|nope|don't|do not|didn't|instead|wrong|not what|stop |never |always |i said|i asked|you forgot|please don't|that's not|not quite|again[,.]|actually[,]? )", re.I)


def tok(n_chars):
    return int(n_chars / 4)


def wikilinks(text):
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return [m.split("|")[0].split("#")[0].strip() for m in re.findall(r"\[\[([^\]]+)\]\]", text)]


def inventory():
    """Stage 1 (local): what Claude reads, when, and mechanical problems."""
    roots = jload(os.path.join(DATA, "roots.json"), [])
    setup = jload(os.path.join(DATA, "setup.json"), {"selected": []})
    always, on_demand, issues = [], [], []

    def entry(path, kind, note=""):
        text = read_text(path)
        return {"path": path, "label": path.replace(HOME, "~"), "kind": kind, "tokens": tok(len(text)),
                "lines": text.count("\n") + 1, "note": note, "text": text}

    user_md = os.path.join(CLAUDE_DIR, "CLAUDE.md")
    if os.path.isfile(user_md):
        always.append(entry(user_md, "instructions", "Read in every Claude Code session, in every folder."))
    for root in roots:
        p = os.path.join(root, "CLAUDE.md")
        if os.path.isfile(p):
            always.append(entry(p, "instructions", "Read in every session in this folder."))
        else:
            issues.append({"kind": "no-claude-md", "severity": "high", "path": root,
                           "title": "No CLAUDE.md in %s" % os.path.basename(root),
                           "detail": "Claude starts each session in this folder with no standing instructions. Rules pages here are only read when something points to them."})
    memory_notes = []
    for mdir in sorted(glob.glob(os.path.join(PROJECTS, "*", "memory"))):
        files = sorted(glob.glob(os.path.join(mdir, "*.md")))
        index = os.path.join(mdir, "MEMORY.md")
        if not files:
            continue
        if os.path.isfile(index):
            e = entry(index, "memory index", "First 200 lines read at the start of every session in this project.")
            always.append(e)
            if e["lines"] > 200:
                issues.append({"kind": "index-long", "severity": "medium", "path": index, "title": "Memory index is over 200 lines",
                               "detail": "Claude only reads the first 200 lines, so later entries are invisible."})
            listed = {os.path.normpath(os.path.join(mdir, h)) for h in re.findall(r"\]\(([^)#\s]+\.md)\)", e["text"])}
            for h in listed:
                if not os.path.isfile(h):
                    issues.append({"kind": "index-missing-file", "severity": "medium", "path": index,
                                   "title": "Index points to a missing note: %s" % os.path.basename(h),
                                   "detail": "MEMORY.md lists a note that doesn't exist."})
        else:
            listed = set()
        names = {}
        for f in files:
            if f == index:
                continue
            e = entry(f, "memory note", "Opened when it looks relevant.")
            fm = frontmatter(e["text"])
            e["name"], e["description"] = fm.get("name", ""), fm.get("description", "")
            names[os.path.basename(f)[:-3]] = f
            if fm.get("name"):
                names[fm["name"]] = f
            on_demand.append(e)
            memory_notes.append(e)
            if os.path.isfile(index) and f not in listed:
                issues.append({"kind": "not-in-index", "severity": "medium", "path": f, "title": "Memory note not in the index: %s" % os.path.basename(f),
                               "detail": "Claude finds notes through MEMORY.md; this one may never be read."})
        for e in memory_notes:
            for link in wikilinks(e["text"]):
                if link not in names and not any(os.path.isfile(os.path.join(r, "pages", link + ".md")) or os.path.isfile(os.path.join(r, link + ".md")) for r in roots):
                    issues.append({"kind": "broken-link", "severity": "low", "path": e["path"],
                                   "title": "Broken link [[%s]] in %s" % (link, os.path.basename(e["path"])),
                                   "detail": "It points to a note that was never written."})
        # near-duplicate notes by description
        for i in range(len(memory_notes)):
            for j in range(i + 1, len(memory_notes)):
                a = set(re.findall(r"\w{4,}", memory_notes[i]["description"].lower()))
                b = set(re.findall(r"\w{4,}", memory_notes[j]["description"].lower()))
                if a and b and len(a & b) / len(a | b) > 0.5:
                    issues.append({"kind": "duplicate", "severity": "low", "path": memory_notes[j]["path"],
                                   "title": "Possible duplicate notes: %s and %s" % (os.path.basename(memory_notes[i]["path"]), os.path.basename(memory_notes[j]["path"])),
                                   "detail": "Their descriptions overlap heavily."})
    reach_text = " ".join(e["text"] for e in always + memory_notes)
    seen = {e["path"] for e in always + on_demand}
    chosen = list(dict.fromkeys(setup.get("selected", []) + [f["path"] for f in scan_setup() if f["kind"] in ("rules page", "added by you", "instructions")]))
    for p in chosen:
        if p in seen or not os.path.isfile(p):
            continue
        e = entry(p, "rules page", "Read only when a task or another file points to it.")
        stem = os.path.basename(p)[:-3]
        refs = [os.path.basename(x["path"]) for x in always + memory_notes if stem in x["text"]]
        e["note"] = ("Pointed to by: %s." % ", ".join(refs)) if refs else "Nothing points to it; read only when named."
        on_demand.append(e)
        if not refs:
            issues.append({"kind": "unreachable", "severity": "high", "path": p,
                           "title": "%s is never pointed to" % os.path.basename(p),
                           "detail": "No CLAUDE.md or memory note mentions it, so Claude only reads it when you ask by name."})
    for e in always:
        if e["kind"] == "instructions" and e["tokens"] > 6000:
            issues.append({"kind": "long-instructions", "severity": "medium", "path": e["path"],
                           "title": "%s is long (about %d tokens)" % (os.path.basename(e["path"]), e["tokens"]),
                           "detail": "It's read every session. Long standing instructions dilute attention; move detail into pages it links to."})
    skills = [{"name": os.path.basename(os.path.dirname(s)), "path": s, "description": frontmatter(read_text(s, 4000)).get("description", "")}
              for s in sorted(glob.glob(os.path.join(CLAUDE_DIR, "skills", "*", "SKILL.md")))]
    agents = [{"name": os.path.basename(a)[:-3], "path": a, "description": frontmatter(read_text(a, 4000)).get("description", "")}
              for a in sorted(glob.glob(os.path.join(CLAUDE_DIR, "agents", "*.md")))]
    synced = sorted({os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(CLAUDE_DIR, "skills", "synced", "*", "*", "SKILL.md"))})
    for e in always + on_demand:
        e.pop("text", None)
    return {"always": always, "on_demand": on_demand, "skills": skills, "agents": agents, "synced": synced, "issues": issues,
            "always_tokens": sum(e["tokens"] for e in always)}


def evidence(days=120, max_files=300):
    """Stage 2 (local): what you ask for, where you corrected Claude, which skills fired."""
    cutoff = time.time() - days * 86400
    files = [f for f in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")) if os.path.getmtime(f) >= cutoff]
    files = sorted(files, key=os.path.getmtime, reverse=True)[:max_files]
    firsts, corrections, skills_fired, n_prompts, dates = [], [], {}, 0, []
    for f in files:
        first = None
        try:
            with open(f, errors="replace") as fh:
                for line in fh:
                    if '"type":"user"' in line:
                        d = json.loads(line)
                        c = d.get("message", {}).get("content")
                        if isinstance(c, list):
                            if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                                continue
                            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                        if not isinstance(c, str):
                            continue
                        c = c.strip()
                        if not c or c.startswith("<") or len(c) < 4:
                            continue
                        n_prompts += 1
                        if first is None:
                            first = c
                            dates.append(d.get("timestamp", "")[:10])
                        elif CORRECTION_RE.search(c[:400]):
                            corrections.append({"text": c[:260], "date": d.get("timestamp", "")[:10]})
                    elif '"name":"Skill"' in line:
                        d = json.loads(line)
                        for x in d.get("message", {}).get("content", []) or []:
                            if isinstance(x, dict) and x.get("type") == "tool_use" and x.get("name") == "Skill":
                                k = str((x.get("input") or {}).get("skill", "?"))
                                skills_fired[k] = skills_fired.get(k, 0) + 1
        except Exception:
            continue
        if first:
            firsts.append({"text": first[:300], "date": dates[-1] if dates else ""})
    ds = sorted(x for x in dates if x)
    return {"sessions": len(firsts), "prompts": n_prompts, "from": ds[0] if ds else "", "to": ds[-1] if ds else "",
            "session_openers": firsts[:80], "corrections": corrections[:40], "skills_fired": skills_fired}


REVIEW_PROMPT = """SETUP CHECK. You are reviewing one person's Claude configuration to tell them, plainly, what it does today and the few changes most likely to help.
Ground every claim in the evidence below: name the file, or quote the person's own words from their sessions. If the evidence is thin, say so rather than guessing.
Write for a smart non-programmer. Plain words, short sentences, no hype.

## Configuration inventory
{inventory}

## Mechanical issues already found (do not repeat these as opportunities unless one is the biggest problem)
{issues}

## Evidence from their Claude Code sessions ({sessions} sessions, {prompts} prompts, {span})
Session openers (what they asked for):
{openers}

Messages that look like corrections:
{corrections}

Skills that fired: {skills_fired}

## Capabilities available to test (catalog ids)
{catalog}

Reply with ONLY a JSON object:
{{
 "summary": "3-4 sentences: what this setup does for them today, and the overall picture",
 "pieces": [{{"name": "file or group", "what_it_does": "one sentence", "assessment": "one sentence: working well / weak spot"}}],
 "opportunities": [
   {{"title": "short imperative", "why": "1-2 sentences tied to the evidence", "evidence": ["file name or short quote", "..."],
     "impact": "high|medium|low", "effort": "low|medium|high",
     "action": one of
       {{"type": "test_catalog", "id": "<catalog id>"}}
       {{"type": "draft_skill", "name": "kebab-case", "description": "when Claude should use it", "instructions": "the skill body, markdown"}}
       {{"type": "save_memory", "name": "kebab-case", "description": "one line", "body": "the note, with **Why:** and **How to apply:** lines"}}
       {{"type": "edit_file", "path": "<exact path from the inventory>", "suggestion": "what to change"}}
       {{"type": "create_claude_md", "folder": "<exact folder path from the issues>", "body": "a short CLAUDE.md, under 40 lines, that points to their rules pages by file name"}}
       {{"type": "ask_claude", "prompt": "what to ask Claude to do"}}
   }}
 ]
}}
Give 3 to 7 opportunities, best first. Prefer small changes with clear payoff. Use test_catalog only when a catalog item fits the evidence."""


def review(inv, ev, model):
    cli = find_cli()
    cat = "\n".join("- %s: %s" % (c["id"], c["what"]) for c in CATALOG)

    def lines(xs, f):
        return "\n".join(f(x) for x in xs) or "(none)"
    inv_txt = "Always read at session start (about %d tokens):\n%s\n\nRead when relevant:\n%s\n\nInstalled skills: %s\nInstalled agents: %s\nSkills from their Claude account: %s" % (
        inv["always_tokens"],
        lines(inv["always"], lambda e: "- %s (%s, ~%d tokens): %s" % (e["label"], e["kind"], e["tokens"], e["note"])),
        lines(inv["on_demand"], lambda e: "- %s (%s): %s" % (e["label"], e["kind"], (e.get("description") + " " if e.get("description") else "") + e["note"])),
        ", ".join(s["name"] for s in inv["skills"]) or "none", ", ".join(a["name"] for a in inv["agents"]) or "none",
        ", ".join(inv["synced"]) or "none")
    # include the always-read files' text, which is what Claude actually sees every session
    for e in inv["always"]:
        inv_txt += "\n\n--- %s ---\n%s" % (e["label"], read_text(e["path"], 6000))
    prompt = REVIEW_PROMPT.format(
        inventory=inv_txt, issues=lines(inv["issues"], lambda i: "- [%s] %s: %s (%s)" % (i["severity"], i["title"], i["detail"], i["path"].replace(HOME, "~"))),
        sessions=ev["sessions"], prompts=ev["prompts"], span=("%s to %s" % (ev["from"], ev["to"])) if ev["from"] else "no dates",
        openers=lines(ev["session_openers"][:60], lambda x: "- (%s) %s" % (x["date"], x["text"].replace("\n", " "))),
        corrections=lines(ev["corrections"], lambda x: "- (%s) %s" % (x["date"], x["text"].replace("\n", " "))),
        skills_fired=", ".join("%s ×%d" % kv for kv in ev["skills_fired"].items()) or "none recorded", catalog=cat)
    cmd = [cli, "-p", "--output-format", "json", "--no-session-persistence", "--tools", "", "--max-budget-usd", "1.5"]
    if model:
        cmd += ["--model", model]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600, cwd=DATA)
    out = json.loads(p.stdout)
    if out.get("is_error"):
        raise ValueError(out.get("result") or "Claude returned an error")
    m = re.search(r"\{.*\}", out.get("result", ""), re.S)
    rep = json.loads(m.group(0))
    # keep actions honest: only known catalog ids and files that exist
    known = {c["id"] for c in CATALOG}
    paths = {e["path"] for e in inv["always"] + inv["on_demand"]}
    for o in rep.get("opportunities", []):
        a = o.get("action") or {}
        if a.get("type") == "test_catalog" and a.get("id") not in known:
            o["action"] = {"type": "ask_claude", "prompt": o.get("title", "")}
        if a.get("type") == "edit_file":
            hit = next((x for x in paths if x == a.get("path") or x.replace(HOME, "~") == a.get("path") or os.path.basename(x) == os.path.basename(a.get("path", ""))), None)
            if hit:
                a["path"] = hit
            else:
                o["action"] = {"type": "ask_claude", "prompt": a.get("suggestion") or o.get("title", "")}
        if a.get("type") == "create_claude_md":
            folder = under_home(a.get("folder", ""))
            roots = [os.path.realpath(r) for r in jload(os.path.join(DATA, "roots.json"), [])]
            a["folder"] = folder if folder in roots else (roots[0] if roots else "")
        o["state"] = "open"
    return rep, out.get("total_cost_usd") or 0


def start_check(body):
    if any(v.get("state") == "running" for v in CHECK_LIVE.values()):
        raise ValueError("A check is already running.")
    if not connect_status().get("logged_in"):
        raise ValueError("Step 1 first: Claude is not signed in.")
    cid = time.strftime("%Y%m%d-%H%M%S")
    CHECK_LIVE[cid] = {"id": cid, "state": "running", "stage": "Taking inventory"}
    threading.Thread(target=_check_thread, args=(cid, body.get("model") or "sonnet"), daemon=True).start()
    return CHECK_LIVE[cid]


def _check_thread(cid, model):
    live = CHECK_LIVE[cid]
    d = os.path.join(CHECKS, cid)
    try:
        inv = inventory()
        live["stage"] = "Reading your past sessions"
        ev = evidence()
        live["stage"] = "Claude is reviewing (about a minute)"
        rep, cost = review(inv, ev, model)
        jsave(os.path.join(d, "check.json"), {"id": cid, "inventory": inv, "evidence": {k: v for k, v in ev.items() if k != "session_openers"},
                                              "evidence_sample": ev["session_openers"][:10], "report": rep, "cost": cost, "model": model})
        live["state"] = "done"
    except Exception as e:
        live["state"], live["error"] = "error", str(e)[:400]


def latest_check():
    files = sorted(glob.glob(os.path.join(CHECKS, "*", "check.json")), reverse=True)
    cur = jload(files[0], None) if files else None
    prev = jload(files[1], None) if len(files) > 1 else None
    live = next((v for v in sorted(CHECK_LIVE.values(), key=lambda v: v["id"], reverse=True)), None)
    if cur and prev:
        cur["previous"] = {"id": prev["id"], "issues": len(prev["inventory"]["issues"]),
                           "always_tokens": prev["inventory"]["always_tokens"],
                           "done": sum(1 for o in prev["report"].get("opportunities", []) if o.get("state") == "done")}
    return {"check": cur, "live": live}


def mark_opportunity(body):
    p = os.path.join(CHECKS, body["check_id"], "check.json")
    c = jload(p, None)
    if not c:
        raise ValueError("Check not found")
    c["report"]["opportunities"][int(body["index"])]["state"] = body.get("state", "done")
    jsave(p, c)
    return {"ok": True}


def act_draft_skill(body):
    meta = do_import({"kind": "text", "text_type": "skill", "name": body.get("name"), "description": body.get("description"),
                      "value": body.get("instructions", "")})
    cand = build_candidate({"import_id": meta["id"], "selected": [i["id"] for i in meta["items"]], "name": body.get("name")})
    return {"candidate": cand}


def act_save_memory(body):
    mdirs = sorted(glob.glob(os.path.join(PROJECTS, "*", "memory")),
                   key=lambda d: len(glob.glob(os.path.join(d, "*.md"))), reverse=True)
    mdir = body.get("dir") or (mdirs[0] if mdirs else None)
    if not mdir or not os.path.isdir(mdir):
        raise ValueError("No memory folder found.")
    name = slug(body.get("name") or "note")
    path = os.path.join(mdir, name + ".md")
    if os.path.exists(path):
        raise ValueError("A memory note named %s already exists." % name)
    desc = (body.get("description") or "").replace("\n", " ").strip()
    with open(path, "w") as f:
        f.write("---\nname: %s\ndescription: %s\nmetadata:\n  type: feedback\n  source: capability-lab setup check\n---\n\n%s\n" % (name, desc, body.get("body", "").strip()))
    index = os.path.join(mdir, "MEMORY.md")
    title = body.get("title") or name.replace("-", " ").capitalize()
    with open(index, "a") as f:
        f.write(("" if read_text(index).endswith("\n") or not os.path.exists(index) else "\n") + "- [%s](%s.md) — %s\n" % (title, name, desc))
    return {"ok": True, "path": path, "index": index}


def act_create_claude_md(body):
    folder = under_home(body.get("folder", ""))
    if not folder or folder not in [os.path.realpath(r) for r in jload(os.path.join(DATA, "roots.json"), [])]:
        raise ValueError("That folder isn't one of the lab's project folders.")
    path = os.path.join(folder, "CLAUDE.md")
    if os.path.exists(path):
        raise ValueError("CLAUDE.md already exists there. Open it and edit instead.")
    with open(path, "w") as f:
        f.write(body.get("body", "").strip() + "\n")
    return {"ok": True, "path": path}


# ---------- HTTP ----------

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        path = urlparse(self.path).path
        if not self.host_ok():
            return self.send({"error": "bad host"}, 403)
        try:
            if path == "/api/file":
                from urllib.parse import parse_qs
                return self.send(read_file(parse_qs(urlparse(self.path).query).get("path", [""])[0]))
            if path in ("/", "/index.html"):
                return self.send(open(os.path.join(APP, "static", "index.html"), "rb").read(), ctype="text/html; charset=utf-8")
            if path == "/api/connect":
                return self.send(connect_status())
            if path == "/api/setup":
                return self.send({"found": scan_setup(), "saved": jload(os.path.join(DATA, "setup.json"), None),
                                  "roots": jload(os.path.join(DATA, "roots.json"), [])})
            if path == "/api/catalog":
                return self.send(CATALOG)
            if path == "/api/candidates":
                return self.send(list_candidates())
            if path == "/api/imports":
                return self.send([jload(m, {}) for m in sorted(glob.glob(os.path.join(DATA, "imports", "*", "meta.json")), key=os.path.getmtime, reverse=True)])
            if path == "/api/tasks":
                return self.send(jload(os.path.join(DATA, "tasks.json"), []))
            if path == "/api/tasks/suggest":
                return self.send(suggest_tasks())
            if path == "/api/check":
                return self.send(latest_check())
            if path == "/api/dashboard":
                return self.send(dashboard())
            if path == "/api/runs":
                return self.send(list_runs())
            if path.startswith("/api/run/"):
                return self.send(run_report(path.split("/")[-1]))
            return self.send({"error": "not found"}, 404)
        except Exception as e:
            return self.send({"error": str(e)}, 400)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.host_ok():
            return self.send({"error": "bad host"}, 403)
        try:
            b = self.body()
            if path == "/api/setup":
                extra = [under_home(p) for p in b.get("extra", []) if p.strip()]
                extra = [p for p in extra if p and os.path.isfile(p)]
                sel = [p for p in b.get("selected", []) if under_home(p)]
                sel += [p for p in extra if p not in sel]
                jsave(os.path.join(DATA, "setup.json"), {"selected": sel, "extra": extra, "notes": b.get("notes", "")})
                return self.send({"ok": True})
            if path == "/api/roots":
                roots = [under_home(r) for r in b.get("roots", [])]
                jsave(os.path.join(DATA, "roots.json"), [r for r in roots if r and os.path.isdir(r)])
                return self.send({"ok": True})
            if path == "/api/import":
                return self.send(do_import(b))
            if path == "/api/check":
                return self.send(start_check(b))
            if path == "/api/check/mark":
                return self.send(mark_opportunity(b))
            if path == "/api/act/draft_skill":
                return self.send(act_draft_skill(b))
            if path == "/api/act/save_memory":
                return self.send(act_save_memory(b))
            if path == "/api/act/create_claude_md":
                return self.send(act_create_claude_md(b))
            if path == "/api/quick":
                return self.send(quick_add(b))
            if path == "/api/candidate":
                return self.send(build_candidate(b))
            if path == "/api/tasks":
                ts = b.get("tasks", [])
                for t in ts:
                    t.setdefault("id", "t" + uuid.uuid4().hex[:6])
                jsave(os.path.join(DATA, "tasks.json"), ts)
                return self.send({"ok": True, "tasks": ts})
            if path == "/api/run":
                return self.send(start_run(b))
            if path == "/api/save":
                return self.send(save_file(b))
            if path == "/api/resolve":
                return self.send(resolve_links(b.get("from"), b.get("links", [])))
            if path == "/api/adopt":
                return self.send(adopt(b))
            if path == "/api/open":
                p = under_home(b.get("path", ""))
                if p:
                    subprocess.run(["open", "-R", p])
                return self.send({"ok": True})
            return self.send({"error": "not found"}, 404)
        except Exception as e:
            return self.send({"error": str(e)}, 400)


if __name__ == "__main__":
    os.chdir(APP)
    if not os.path.exists(os.path.join(DATA, "roots.json")):
        # Start with any Obsidian vaults in iCloud; users can add more folders in step 2.
        vaults = glob.glob(HOME + "/Library/Mobile Documents/iCloud~md~obsidian/Documents/*/")
        jsave(os.path.join(DATA, "roots.json"), [v.rstrip("/") for v in vaults])
    print("Capability Lab running at http://127.0.0.1:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
