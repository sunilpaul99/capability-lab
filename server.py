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
DATA = os.path.join(APP, "data")
PACKS = os.path.join(APP, "packs")
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
    for mdir in sorted(glob.glob(HOME + "/.claude/projects/*/memory")):
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
            elif low.endswith(".md") and (os.sep + "agents" in os.sep + rel) and fn != "README.md":
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


def do_import(body):
    iid = uuid.uuid4().hex[:8]
    idir = os.path.join(DATA, "imports", iid)
    src = os.path.join(idir, "src")
    os.makedirs(idir)
    kind, value = body.get("kind"), (body.get("value") or "").strip()
    if kind == "git":
        if not re.match(r"^https://[\w.-]+/[\w./-]+$", value):
            raise ValueError("Use an https:// git URL, e.g. https://github.com/owner/repo")
        url = value
        sub = ""
        m = re.match(r"^(https://github\.com/[^/]+/[^/]+)/tree/[^/]+/(.+)$", value)
        if m:
            url, sub = m.group(1), m.group(2)
        r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "--depth", "1",
                            "--no-recurse-submodules", url, src], capture_output=True, text=True, timeout=180, cwd=DATA)
        if r.returncode != 0:
            raise ValueError("git clone failed: " + r.stderr[-400:])
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
             "skills": skills, "agents": agents, "has_rules": bool(rules), "created": time.strftime("%Y-%m-%d %H:%M")}
    jsave(os.path.join(cdir, "meta.json"), cmeta)
    return cmeta


def list_candidates():
    out = []
    for m in sorted(glob.glob(os.path.join(DATA, "candidates", "*", "meta.json")), key=os.path.getmtime, reverse=True):
        c = jload(m, {})
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
    return {"path": p, "label": p.replace(HOME, "~"), "text": read_text(p, 400_000)}


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
    files = sorted(glob.glob(HOME + "/.claude/projects/*/*.jsonl"), key=os.path.getmtime, reverse=True)
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
        verdict = "Adopt: the new capability made answers better more often than worse."
    elif net <= -0.25:
        verdict = "Don't adopt: your current setup did better."
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
        out.append({"id": rid, "candidate": cfg.get("candidate", {}).get("name"), "tasks": len(cfg.get("tasks", [])),
                    "summary": s, "state": (RUNS.get(rid) or {}).get("state", "done" if s else "stopped")})
    return out


# ---------- step 6: adopt / share ----------

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
        done = []
        plug = os.path.join(cdir, "plugin")
        for sk in glob.glob(os.path.join(plug, "skills", "*")):
            dest = os.path.join(HOME, ".claude", "skills", os.path.basename(sk))
            if os.path.exists(dest):
                raise ValueError("A skill named %s already exists in ~/.claude/skills" % os.path.basename(sk))
            shutil.copytree(sk, dest)
            done.append(dest.replace(HOME, "~"))
        for ag in glob.glob(os.path.join(plug, "agents", "*.md")):
            dest = os.path.join(HOME, ".claude", "agents", os.path.basename(ag))
            if os.path.exists(dest):
                raise ValueError("An agent file %s already exists" % os.path.basename(ag))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy(ag, dest)
            done.append(dest.replace(HOME, "~"))
        rules = read_text(os.path.join(cdir, "rules.md"))
        return {"ok": True, "installed": done, "rules_to_copy": rules}
    raise ValueError("Unknown mode")


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
            if path == "/api/candidates":
                return self.send(list_candidates())
            if path == "/api/imports":
                return self.send([jload(m, {}) for m in sorted(glob.glob(os.path.join(DATA, "imports", "*", "meta.json")), key=os.path.getmtime, reverse=True)])
            if path == "/api/tasks":
                return self.send(jload(os.path.join(DATA, "tasks.json"), []))
            if path == "/api/tasks/suggest":
                return self.send(suggest_tasks())
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
