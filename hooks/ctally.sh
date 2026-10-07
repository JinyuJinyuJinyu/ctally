#!/bin/sh
# Claude Code hook for CTally: records this session's state where CTally looks.
#
#   ctally.sh working | done | waiting | end
#
# Writes ~/.claude/ctally.d/<session id> as "<state> <claude pid> <project folder>";
# "end" removes it. A "waiting" never overwrites "done": Claude Code also sends its idle
# notification after a turn has finished, and the turn being finished is what matters.
# Silent, and always exits 0, so it can never get in Claude Code's way.

state="$1"
dir="$HOME/.claude/ctally.d"

session="${CLAUDE_CODE_SESSION_ID:-}"
if [ -z "$session" ]; then
  # Otherwise the session id is in the JSON Claude Code passes on stdin. Take the first
  # one: a tool's input, later in the same JSON, can carry a "session_id" of its own.
  session=$(grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n 1 \
    | sed 's/.*"\([^"]*\)"$/\1/')
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
# Write beside the file and rename it into place, so CTally never reads it half-written.
# The dot keeps CTally from taking the temporary file for a session.
tmp="$dir/.$session.$$"
printf '%s %s %s\n' "$state" "${CLAUDE_PID:-$PPID}" "$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")" \
  2>/dev/null > "$tmp" && mv -f "$tmp" "$file" 2>/dev/null || rm -f "$tmp" 2>/dev/null
exit 0
