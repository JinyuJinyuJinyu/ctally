#!/bin/sh
# Claude Code hook for Claude Pet: records this session's state where the pet looks.
#
#   claude-pet.sh working | done | waiting | end
#
# Writes ~/.claude/claude-pet.d/<session id> as "<state> <claude pid> <project folder>";
# "end" removes it. A "waiting" never overwrites "done": Claude Code also sends its idle
# notification after a turn has finished, and the turn being finished is what matters.
# Silent, and always exits 0, so it can never get in Claude Code's way.

state="$1"
dir="$HOME/.claude/claude-pet.d"

session="${CLAUDE_CODE_SESSION_ID:-}"
if [ -z "$session" ]; then
  # Otherwise the session id is in the JSON Claude Code passes on stdin.
  session=$(sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
fi
case "$session" in
  "" | */* | .*) session=default ;;
esac
file="$dir/$session"

case "$state" in
  end) rm -f "$file"; exit 0 ;;
  working | done | waiting) ;;
  *) exit 0 ;;
esac

if [ "$state" = waiting ]; then
  case "$(cat "$file" 2>/dev/null)" in done*) exit 0 ;; esac
fi

mkdir -p "$dir" 2>/dev/null
printf '%s %s %s\n' "$state" "${CLAUDE_PID:-$PPID}" "$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")" \
  > "$file" 2>/dev/null
exit 0
