#!/bin/sh
# Start a throwaway Capability Lab on port 8797 with demo data, using the fake Claude CLI.
# Copies your memory notes and session files into tests/tmp so nothing real is touched.
# Stop with: kill $(lsof -ti :8797); rm -r tests/tmp
cd "$(dirname "$0")/.."
B="$PWD/tests/tmp"; mkdir -p "$B/vault/pages" "$B/projects/p1/memory" "$B/data"
cp "$HOME"/.claude/projects/*Icloud-Vault*/memory/*.md "$B/projects/p1/memory/" 2>/dev/null
cp "$HOME"/.claude/projects/*Icloud-Vault*/*.jsonl "$B/projects/p1/" 2>/dev/null
for s in morning deep-research docx pdf learn; do mkdir -p "$B/claude/skills/synced/acct/$s"; printf -- "---\nname: $s\ndescription: x\n---\n" > "$B/claude/skills/synced/acct/$s/SKILL.md"; done
printf -- "---\nname: writing-voice\n---\n# Voice\n" > "$B/vault/pages/writing-voice.md"
printf -- "# Procedures\n" > "$B/vault/pages/bell-ai-operating-procedures.md"
echo "[\"$B/vault\"]" > "$B/data/roots.json"
CAPLAB_DATA="$B/data" CAPLAB_PROJECTS="$B/projects" CAPLAB_CLAUDE_DIR="$B/claude" CLAUDE_CLI="$PWD/tests/fake_claude.py" PORT=8797 nohup python3 server.py > "$B/log" 2>&1 &
sleep 1
q(){ curl -s -XPOST "localhost:8797/api/$1" -d "$2"; }
id(){ python3 -c "import sys,json;print(json.load(sys.stdin)['candidate']['id'])"; }
D=$(q quick '{"id":"discernment-nudge"}' | id); I=$(q quick '{"id":"internal-comms"}' | id); F=$(q quick '{"id":"first-principles"}' | id)
q quick '{"id":"brainstorming"}' >/dev/null
for c in $D $I $F; do q run "{\"candidate_id\":\"$c\",\"reps\":1}" >/dev/null; sleep 4; done
q adopt "{\"candidate_id\":\"$I\",\"mode\":\"install\"}" >/dev/null; q adopt "{\"candidate_id\":\"$F\",\"mode\":\"reject\"}" >/dev/null
q check '{}' >/dev/null; sleep 3
echo "Demo running at http://localhost:8797"
