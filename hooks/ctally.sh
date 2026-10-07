#!/bin/sh
# Claude Code hook for CTally: records this session's state where CTally looks.
#
#   ctally.sh working | done | waiting | end | agent-start | agent-stop
#
# Writes ~/.claude/ctally.d/<session id> as "<state> <claude pid> <project folder>";
# "end" removes it. A "waiting" never overwrites "done": Claude Code also sends its idle
# notification after a turn has finished, and the turn being finished is what matters.
#
# Subagents, a workflow's included, each get a file in ~/.claude/ctally.d/.agents/<session
# id>/ while they run. A turn that ends with agents or a workflow still running in the
# background isn't done: the session stays "working" until they finish and Claude has
# taken in their results.
#
# Inside tmux, a change of state also redraws tmux's status lines, so CTally's counts
# there change at once. Silent, and always exits 0, so it never gets in Claude Code's way.

state="$1"
dir="$HOME/.claude/ctally.d"

# Claude Code passes the event's details as one line of JSON on stdin.
input=""
[ -t 0 ] || input=$(cat)

# A string field of that JSON, by name.
field() {
  printf '%s\n' "$input" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -n 1
}

# A name that is safe to use as a file name, or nothing.
safe() {
  case "$1" in
    "" | */* | .*) ;;
    *) printf '%s' "$1" ;;
  esac
}

session=$(safe "${CLAUDE_CODE_SESSION_ID:-$(field session_id)}")
session="${session:-default}"
file="$dir/$session"
agents="$dir/.agents/$session"
before=$(cat "$file" 2>/dev/null)

# In the background, with nothing left attached to Claude Code's pipes.
redraw_tmux() {
  [ -n "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1 || return 0
  (tmux list-clients -F '#{client_name}' | while read -r client; do
     tmux refresh-client -S -t "$client"
   done) </dev/null >/dev/null 2>&1 &
}

# Whether a turn that just ended left subagents or a workflow running: Stop lists the
# session's background tasks, one JSON object each.
background_running() {
  printf '%s\n' "$input" | tr '{' '\n' | grep -E '"status" *: *"(running|pending)"' \
    | grep -qE '"type" *: *"(subagent|workflow)"'
}

case "$state" in
  end)
    rm -f "$file"
    rm -rf "$agents"
    redraw_tmux
    exit 0
    ;;
  agent-start)
    agent=$(safe "$(field agent_id)")
    [ -n "$agent" ] || exit 0
    mkdir -p "$agents" 2>/dev/null
    field agent_type > "$agents/$agent" 2>/dev/null
    exit 0
    ;;
  agent-stop)
    agent=$(safe "$(field agent_id)")
    [ -n "$agent" ] && rm -f "$agents/$agent"
    rmdir "$agents" 2>/dev/null
    exit 0
    ;;
  done)
    if background_running; then
      # Not done yet: the marker says so to the idle notification that follows.
      state=working
      mkdir -p "$agents" 2>/dev/null && : > "$agents/.background"
    else
      rm -rf "$agents"           # nothing of this session's is running any more
    fi
    ;;
  waiting)
    case "$before" in done*) exit 0 ;; esac
    case "$(field notification_type)" in
      # Waiting on its own agents, not on me.
      idle_prompt) [ -d "$agents" ] && exit 0 ;;
      # News, not a question.
      agent_completed | auth_success | elicitation_complete | elicitation_response | \
      computer_use_enter | computer_use_exit) exit 0 ;;
    esac
    ;;
  working) ;;
  *) exit 0 ;;
esac

mkdir -p "$dir" 2>/dev/null
printf '%s %s %s\n' "$state" "${CLAUDE_PID:-$PPID}" "$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")" \
  > "$file" 2>/dev/null
[ "${before%% *}" = "$state" ] || redraw_tmux
exit 0
