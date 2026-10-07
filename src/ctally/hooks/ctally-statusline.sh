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
# Each session hands over the numbers from its own last reply, and an idle session redraws
# its status line now and then (when its prompt cache goes cold, say) with numbers hours old.
# So a reading replaces the saved one only if it isn't older: usage only grows within a
# window, and a later reset time means a later window.
#
# A usage limit the hook noted as hit (.limit) is over once its window has moved on: a limit
# reset early starts a new window, which ends later than the one that was used up.

input=$(cat)

# One field of one window in the JSON in $1: window_field "$json" five_hour resets_at
window_field() {
  printf '%s\n' "$1" | grep -o "\"$2\"[[:space:]]*:[[:space:]]*{[^}]*}" \
    | grep -o "\"$3\"[[:space:]]*:[[:space:]]*[0-9.]*" | head -n 1 | sed 's/.*:[[:space:]]*//'
}

# "<session resets> <session used> <week resets> <week used>", 0 for anything missing.
reading() {
  a=$(window_field "$1" five_hour resets_at)
  b=$(window_field "$1" five_hour used_percentage)
  c=$(window_field "$1" seven_day resets_at)
  d=$(window_field "$1" seven_day used_percentage)
  printf '%s %s %s %s\n' "${a:-0}" "${b:-0}" "${c:-0}" "${d:-0}"
}

# Whether reading $1 is no older than reading $2: the session window first, then the week;
# reset times a minute apart are the same window. Once the saved session window is over,
# anything goes.
no_older() {
  awk -v new="$1" -v old="$2" -v now="$(date +%s)" 'BEGIN {
    split(new, n, " "); split(old, o, " ")
    if (o[1] + 0 < now) exit 0
    for (i = 1; i <= 4; i++) {
      d = n[i] - o[i]
      if (i % 2 == 1 && d >= -60 && d <= 60) continue
      if (d > 0) exit 0
      if (d < 0) exit 1
    }
    exit 0
  }'
}

case "$input" in
  *'"rate_limits"'*)
    dir="$HOME/.claude/ctally.d"
    mkdir -p "$dir" 2>/dev/null
    saved=$(cat "$dir/.statusline" 2>/dev/null)
    case "$saved" in
      *'"rate_limits"'*) no_older "$(reading "$input")" "$(reading "$saved")" || older=1 ;;
    esac
    if [ -z "${older:-}" ]; then
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
        [ -n "$window" ] && resets=$(window_field "$input" "$window" resets_at)
        [ "${hit:-0}" -gt 0 ] 2>/dev/null && [ "${resets:-0}" -gt $((hit + 60)) ] 2>/dev/null \
          && rm -f "$dir/.limit"
      fi
    fi
    ;;
esac

[ -n "$1" ] || exit 0
printf '%s\n' "$input" | sh -c "$1"
