#!/bin/sh
# Claude Code hook for CTally: records this session's state where CTally looks.
#
#   ctally.sh working | done | waiting | end
#
# Writes ~/.claude/ctally.d/<session id> as "<state> <claude pid> <project folder>";
# "end" removes it. A "waiting" never overwrites "done": Claude Code also sends its idle
# notification after a turn has finished, and the turn being finished is what matters.
# Inside tmux, a change of state also redraws tmux's status lines, so CTally's counts
# there change at once. Silent, and always exits 0, so it never gets in Claude Code's way.

state="$1"
dir="$HOME/.claude/ctally.d"

session="${CLAUDE_CODE_SESSION_ID:-}"
if [ -z "$session" ]; then
  # Otherwise the session id is in the JSON Claude Code passes on stdin.
  session=$(sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
fi
case "$session" in
  "" | */* | .*) session=default ;;
esac
file="$dir/$session"
before=$(cat "$file" 2>/dev/null)

# In the background, with nothing left attached to Claude Code's pipes.
redraw_tmux() {
  [ -n "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1 || return 0
  (tmux list-clients -F '#{client_name}' | while read -r client; do
     tmux refresh-client -S -t "$client"
   done) </dev/null >/dev/null 2>&1 &
}

case "$state" in
  end) rm -f "$file"; redraw_tmux; exit 0 ;;
  working | done | waiting) ;;
  *) exit 0 ;;
esac

if [ "$state" = waiting ]; then
  case "$before" in done*) exit 0 ;; esac
fi

mkdir -p "$dir" 2>/dev/null
printf '%s %s %s\n' "$state" "${CLAUDE_PID:-$PPID}" "$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")" \
  > "$file" 2>/dev/null
[ "${before%% *}" = "$state" ] || redraw_tmux
exit 0
