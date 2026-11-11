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
# said>", and under it "account <id>", the one signed in, when ~/.claude.json names one. A
# prompt sent before then goes nowhere, so it leaves the session limited too, unless I've
# changed accounts since: the limit was the other one's.
#
# What a session waiting on me asks, a tool to allow, questions or a plan, is kept as
# Claude Code put it in ~/.claude/ctally.d/.asks/<session id>, until that's settled: the
# tool has run (or failed), whoever asked has gone on to another, the turn is over, or I've
# sent a new prompt. Until then the session stays waiting, whatever else of it carries on:
# its agents, or a tool started alongside the one it asked about.
#
# Subagents, a workflow's included, each get a file in ~/.claude/ctally.d/.agents/<session
# id>/ while they run. A turn that ends with agents or a workflow still running in the
# background isn't done: the session stays "working" until they finish and Claude has
# taken in their results. Meanwhile a .background file there says Claude itself is idle, so
# CTally can tell when those agents have all been stopped (killed agents get no hook).
#
# For `ctally stats`, ~/.claude/ctally.d/.history/<date> gets a line, "<Unix time> <session
# id> <state>", when the state changes, for each prompt (with "prompt" after it), and every
# few minutes while a state lasts and Claude Code keeps calling, so a session killed without
# a word leaves no endless stretch behind; "end" when the session ends.
#
# Inside tmux, a change of state also redraws tmux's status lines, so CTally's counts
# there change at once. Silent, and always exits 0, so it never gets in Claude Code's way.

state="$1"
dir="$HOME/.claude/ctally.d"

# Claude Code passes the event's details as one line of JSON on stdin.
input=""
[ -t 0 ] || input=$(cat)

# A string field of that JSON, by name. The first one: a tool's input, later in the same
# JSON, can carry fields of its own by the same names ("session_id", say). Given a file, the
# field from the JSON in that instead.
field() {
  if [ -n "${2:-}" ]; then cat "$2" 2>/dev/null; else printf '%s\n' "$input"; fi \
    | grep -o "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n 1 | sed 's/.*"\([^"]*\)"$/\1/'
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
ask="$dir/.asks/$session"
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

# The account signed in, as ~/.claude.json names it ("accountUuid" in "oauthAccount"), or
# nothing. The limits are an account's, and I may have more than one.
signed_in() {
  tr -d '\n' 2>/dev/null < "$HOME/.claude.json" | grep -o '"oauthAccount"[[:space:]]*:[[:space:]]*{[^}]*' \
    | grep -o '"accountUuid"[[:space:]]*:[[:space:]]*"[A-Za-z0-9_-]*"' | head -n 1 | sed 's/.*"\([^"]*\)"$/\1/'
}

# Notes the usage limit a turn just ran into: which one, as Claude Code put it ("You've hit
# your session limit · resets 7:30pm (Australia/Sydney)"), when that window resets, from what
# CTally's status line saw last of this account (0 when that's not known), and whose it is.
note_limit() {
  message=$(field last_assistant_message)
  case "$message" in
    *"session limit"*) window=five_hour ;;
    *"weekly limit"*) window=seven_day ;;
    *) window="" ;;
  esac
  account=$(signed_in)
  # The status line keeps each account's latest; one from before it did, only the latest of any.
  seen="$dir/.statusline"
  [ -d "$dir/.usage" ] && seen="$dir/.usage/${account:--}"
  resets=0
  if [ -n "$window" ]; then
    resets=$(grep -o "\"$window\"[[:space:]]*:[[:space:]]*{[^}]*}" "$seen" 2>/dev/null \
      | grep -o '"resets_at"[[:space:]]*:[[:space:]]*[0-9]*' | head -n 1 | sed 's/.*:[[:space:]]*//')
    [ "${resets:-0}" -gt "$(date +%s)" ] 2>/dev/null || resets=0
  fi
  mkdir -p "$dir" 2>/dev/null
  {
    printf '%s %s\n' "$resets" "$message"
    if [ -n "$account" ]; then printf 'account %s\n' "$account"; fi
  } 2>/dev/null > "$limit.$$" && mv -f "$limit.$$" "$limit" 2>/dev/null || rm -f "$limit.$$" 2>/dev/null
}

# Whether that limit still holds: until it resets, or, not knowing when, until something
# shows it has. Hit on another account than the one signed in now, it holds nothing up.
limit_holds() {
  [ -f "$limit" ] || return 1
  resets="" whose=""
  { read -r resets _; read -r _ whose; } < "$limit" 2>/dev/null
  case "$resets" in "" | *[!0-9]*) resets=0 ;; esac
  if [ -n "$whose" ]; then
    account=$(signed_in)
    [ -n "$account" ] && [ "$account" != "$whose" ] && return 1
  fi
  [ "$resets" -eq 0 ] || [ "$resets" -gt "$(date +%s)" ]
}

# Notes in the history what just happened: the state, and "prompt" for a prompt. Beside it,
# .<session id> holds when the session's last line was written, and how many calls since.
history="$dir/.history"
BEAT=300            # a lasting state is noted again after five minutes,
CALLS=20            # looked into every twenty calls: the clock costs a process
log_history() {
  stamp=${stamp:-$(date '+%s %Y-%m-%d')}
  mkdir -p "$history" 2>/dev/null
  printf '%s %s %s\n' "${stamp% *}" "$session" "$1" 2>/dev/null >> "$history/${stamp#* }"
  printf '%s 0\n' "${stamp% *}" 2>/dev/null > "$history/.$session"
}

# Keeps what's being asked, whole: a tool and its input, the questions and their options, or
# the plan. CTally reads it as JSON, which this script can't.
keep_ask() {
  mkdir -p "$dir/.asks" 2>/dev/null
  printf '%s\n' "$input" 2>/dev/null > "$dir/.asks/.$session.$$" \
    && mv -f "$dir/.asks/.$session.$$" "$ask" 2>/dev/null || rm -f "$dir/.asks/.$session.$$" 2>/dev/null
}

# Whether Claude carrying on shows that what it asked is settled: a new prompt from me; or,
# from whoever asked (Claude itself, or the agent of its that did), the tool it asked about
# having run or failed, or its next tool starting, which is what "no, do this instead"
# leads to. The rest of the session settles nothing: another of its agents at work, or a
# tool started alongside the one asked about and finishing while the question is still up.
settled() {
  event=$(field hook_event_name)
  [ "$event" = UserPromptSubmit ] && return 0
  [ "$(field agent_id)" = "$(field agent_id "$ask")" ] || return 1
  case "$event" in
    PreToolUse) return 0 ;;
    PostToolUse | PostToolUseFailure) [ "$(field tool_name)" = "$(field tool_name "$ask")" ] ;;
    *) return 1 ;;
  esac
}

case "$state" in
  end)
    rm -f "$file" "$ask"
    rm -rf "$agents"
    [ -f "$history/.$session" ] && log_history end
    rm -f "$history/.$session"
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
    rm -f "$ask"                 # and nothing it asked is still open
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
    rm -f "$ask"
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
  asking)
    state=waiting
    keep_ask
    ;;
  working)
    # A prompt sent while the limit holds goes nowhere. Anything Claude does shows it has
    # got through: a tool, a permission, the end of the turn.
    case "$before" in
      limited*) [ "$(field hook_event_name)" = UserPromptSubmit ] && limit_holds && exit 0 ;;
    esac
    # What it asked is on screen until that's settled, and it waits on me till then.
    if [ -f "$ask" ]; then
      settled || exit 0
      rm -f "$ask"
    fi
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

# The history: a change, a prompt, a session not in it yet (working when this hook came), or
# a state that has lasted since the last line about it.
case "$input" in
  *'"hook_event_name":"UserPromptSubmit"'* | *'"hook_event_name": "UserPromptSubmit"'*) prompt=" prompt" ;;
esac
if [ "${before%% *}" != "$state" ] || [ -n "${prompt:-}" ] || [ ! -f "$history/.$session" ]; then
  log_history "$state${prompt:-}"
else
  last=0 calls=0
  [ -f "$history/.$session" ] && read -r last calls < "$history/.$session" 2>/dev/null
  case "$last" in "" | *[!0-9]*) last=0 ;; esac
  case "$calls" in "" | *[!0-9]*) calls=0 ;; esac
  calls=$((calls + 1))
  if [ "$last" -eq 0 ]; then
    log_history "$state"               # a mark with no time in it: as good as none
  elif [ "$calls" -ge "$CALLS" ]; then
    stamp=$(date '+%s %Y-%m-%d')
    if [ $((${stamp% *} - last)) -ge "$BEAT" ]; then
      log_history "$state"
    else
      printf '%s 0\n' "$last" 2>/dev/null > "$history/.$session"
    fi
  else
    printf '%s %s\n' "$last" "$calls" 2>/dev/null > "$history/.$session"
  fi
fi
[ "${before%% *}" = "$state" ] || redraw_tmux
exit 0
