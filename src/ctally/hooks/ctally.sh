#!/bin/sh
# Claude Code hook for CTally: records this session's state where CTally looks.
#
#   ctally.sh working | asking | done | failed | waiting | end | agent-start | agent-stop
#
# Writes ~/.claude/ctally.d/<session id> as "<state> <claude pid> <project folder>";
# "end" removes it. A "waiting" never overwrites "done": Claude Code also sends its idle
# notification after a turn has finished, and the turn being finished is what matters.
# "asking" is a permission, a question or a plan put to me, the moment it's on screen.
#
# A turn stopped by the plan's usage limit leaves the session "limited", and notes the
# limit in ~/.claude/ctally.d/.limit as "<Unix time it resets, or 0> <what Claude Code
# said>". A prompt sent before then goes nowhere, so it leaves the session limited too.
#
# Subagents, a workflow's included, each get a file in ~/.claude/ctally.d/.agents/<session
# id>/ while they run. A turn that ends with agents or a workflow still running in the
# background isn't done: the session stays "working" until they finish and Claude has
# taken in their results. Meanwhile a .background file there says Claude itself is idle, so
# CTally can tell when those agents have all been stopped (killed agents get no hook).
#
# Inside tmux, a change of state also redraws tmux's status lines, so CTally's counts
# there change at once. Silent, and always exits 0, so it never gets in Claude Code's way.

state="$1"
dir="$HOME/.claude/ctally.d"

# Claude Code passes the event's details as one line of JSON on stdin.
input=""
[ -t 0 ] || input=$(cat)

# A string field of that JSON, by name. The first one: a tool's input, later in the same
# JSON, can carry fields of its own by the same names ("session_id", say).
field() {
  printf '%s\n' "$input" | grep -o "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n 1 \
    | sed 's/.*"\([^"]*\)"$/\1/'
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
limit="$dir/.limit"
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

# Notes the usage limit a turn just ran into: which one, as Claude Code put it ("You've hit
# your session limit · resets 7:30pm (Australia/Sydney)"), and when that window resets, from
# what CTally's status line saw last. 0 when that's not known.
note_limit() {
  message=$(field last_assistant_message)
  case "$message" in
    *"session limit"*) window=five_hour ;;
    *"weekly limit"*) window=seven_day ;;
    *) window="" ;;
  esac
  resets=0
  if [ -n "$window" ]; then
    resets=$(grep -o "\"$window\":{[^}]*}" "$dir/.statusline" 2>/dev/null | grep -o '"resets_at":[0-9]*' \
      | head -n 1 | sed 's/.*://')
    [ "${resets:-0}" -gt "$(date +%s)" ] 2>/dev/null || resets=0
  fi
  mkdir -p "$dir" 2>/dev/null
  printf '%s %s\n' "$resets" "$message" 2>/dev/null > "$limit.$$" && mv -f "$limit.$$" "$limit" 2>/dev/null \
    || rm -f "$limit.$$" 2>/dev/null
}

# Whether that limit still holds: until it resets, or, not knowing when, until something
# shows it has.
limit_holds() {
  [ -f "$limit" ] || return 1
  read -r resets _ < "$limit" 2>/dev/null
  case "$resets" in "" | *[!0-9]*) resets=0 ;; esac
  [ "$resets" -eq 0 ] || [ "$resets" -gt "$(date +%s)" ]
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
    field agent_type 2>/dev/null > "$agents/.$agent" && mv -f "$agents/.$agent" "$agents/$agent" 2>/dev/null
    exit 0
    ;;
  agent-stop)
    agent=$(safe "$(field agent_id)")
    [ -n "$agent" ] && rm -f "$agents/$agent"
    rmdir "$agents" 2>/dev/null
    exit 0
    ;;
  done)
    rm -f "$limit"               # a turn got through, so no limit is in the way now
    if background_running; then
      # Not done yet: the marker says so to the idle notification that follows.
      state=working
      mkdir -p "$agents" 2>/dev/null && : > "$agents/.background"
    else
      rm -rf "$agents"           # nothing of this session's is running any more
    fi
    ;;
  failed)
    # Claude Code gave up on the turn, and says why.
    case "$(field error)" in
      rate_limit)
        state=limited
        note_limit
        ;;
      # Mine to sort out.
      authentication_failed | oauth_org_not_allowed | verification_required | account_on_hold | billing_error)
        state=waiting ;;
      # Overloaded, a server error, and so on: the turn is over, the error on screen.
      *) state=done ;;
    esac
    ;;
  waiting)
    case "$(field notification_type)" in
      # The usage limit is over: Claude carries on by itself, or waits for me to say so.
      quota_auto_resume_fired)
        rm -f "$limit"
        state=working
        ;;
      quota_auto_resume_stale) rm -f "$limit" ;;
      # Idle at the prompt: nothing new after a finished turn, one waiting on its own agents,
      # or a question still open. After a turn that never said it was over, which is what
      # Esc leaves, the turn is done.
      idle_prompt)
        case "$before" in done* | limited* | waiting*) exit 0 ;; esac
        [ -d "$agents" ] && exit 0
        state=done
        ;;
      # News, not a question.
      agent_completed | auth_success | elicitation_complete | elicitation_response | \
      computer_use_enter | computer_use_exit | quota_auto_resume_disabled | push_notification | \
      model_refusal_fallback | auth_storage_failure) exit 0 ;;
      *) case "$before" in done* | limited*) exit 0 ;; esac ;;
    esac
    ;;
  asking) state=waiting ;;
  working)
    # A prompt sent while the limit holds goes nowhere. Anything Claude does shows it has
    # got through: a tool, a permission, the end of the turn.
    case "$before" in
      limited*) [ "$(field hook_event_name)" = UserPromptSubmit ] && limit_holds && exit 0 ;;
    esac
    # Claude itself at work again, not just one of its agents.
    [ -z "$(field agent_id)" ] && rm -f "$agents/.background" 2>/dev/null
    ;;
  *) exit 0 ;;
esac

mkdir -p "$dir" 2>/dev/null
# Write beside the file and rename it into place, so CTally never reads it half-written.
# The dot keeps CTally from taking the temporary file for a session.
tmp="$dir/.$session.$$"
printf '%s %s %s\n' "$state" "${CLAUDE_PID:-$PPID}" "$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")" \
  2>/dev/null > "$tmp" && mv -f "$tmp" "$file" 2>/dev/null || rm -f "$tmp" 2>/dev/null
[ "${before%% *}" = "$state" ] || redraw_tmux
exit 0
