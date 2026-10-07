#!/bin/sh
# Claude Code status line for CTally: keeps your plan's usage limits where CTally reads them.
#
#   ctally-statusline.sh ['<the status line you had before>']
#
# After every reply Claude Code hands its status line the session and week limits, as
# "rate_limits" in the JSON on stdin. This saves that JSON to ~/.claude/ctally.d/.statusline,
# the latest from any session (the limits are your account's, not a session's), and prints
# nothing of its own. Given the command of a status line you already had, it runs that with
# the same input, so it still shows. Always exits as that command does, or 0.
#
# A usage limit the hook noted as hit (.limit) is over once its window has moved on: a limit
# reset early starts a new window, which ends later than the one that was used up.

input=$(cat)

case "$input" in
  *'"rate_limits"'*)
    dir="$HOME/.claude/ctally.d"
    mkdir -p "$dir" 2>/dev/null
    tmp="$dir/.statusline.$$"
    printf '%s\n' "$input" 2>/dev/null > "$tmp" && mv -f "$tmp" "$dir/.statusline" 2>/dev/null \
      || rm -f "$tmp" 2>/dev/null
    if [ -f "$dir/.limit" ]; then
      read -r hit message < "$dir/.limit" 2>/dev/null
      case "$message" in
        *"session limit"*) window=five_hour ;;
        *"weekly limit"*) window=seven_day ;;
        *) window="" ;;
      esac
      resets=""
      [ -n "$window" ] && resets=$(printf '%s\n' "$input" | grep -o "\"$window\"[[:space:]]*:[[:space:]]*{[^}]*}" \
        | grep -o '"resets_at"[[:space:]]*:[[:space:]]*[0-9]*' | head -n 1 | sed 's/.*:[[:space:]]*//')
      [ "${hit:-0}" -gt 0 ] 2>/dev/null && [ "${resets:-0}" -gt $((hit + 60)) ] 2>/dev/null \
        && rm -f "$dir/.limit"
    fi
    ;;
esac

[ -n "$1" ] || exit 0
printf '%s\n' "$input" | sh -c "$1"
