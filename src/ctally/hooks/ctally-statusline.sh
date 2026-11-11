#!/bin/sh
# Claude Code status line for CTally: keeps your plan's usage limits, and how full each
# session's context is, where CTally reads them.
#
#   ctally-statusline.sh ['<the status line you had before>']
#
# After every reply Claude Code hands its status line the session and week limits, as
# "rate_limits" in the JSON on stdin, and how full the session's context window is. This
# prints nothing of its own. Given the command of a status line you already had, it runs that
# with the same input, so it still shows. Always exits as that command does, or 0.
#
# Each session's latest is kept whole in ~/.claude/ctally.d/.status/<session id>: its context,
# and what it last said of the limits.
#
# The limits are an account's, and Claude Code doesn't say whose: each session hands over the
# numbers from its own last reply, on whichever account that was. So the latest is kept for
# each account, in ~/.claude/ctally.d/.usage/<account id>, and CTally shows the one for the
# account signed in now, which ~/.claude.json names. Sign in to another, or swap accounts
# under the sessions that are running, and its own numbers are there to show.
#
# A reading counts only when its session has had a reply since it last drew its status line:
# that's the one time its numbers are new, and Claude Code's count of what the session has
# had from the API so far, which comes in the same JSON, moves with every reply. An idle
# session redraws now and then (when its prompt cache goes cold, say) with the numbers of
# hours ago, for whichever account it was on then: those change nothing. The latest reply's
# numbers are kept as they are, more used than before or less (a limit reset where it stood).
#
# Whose they are, the week says: every account's week ends at its own time. Numbers for a
# week already kept go to the account it's kept for, whoever is signed in by now (a reply
# that was on its way as I changed accounts). For a week not seen before, they're the account
# signed in's.
#
# ~/.claude/ctally.d/.statusline holds the latest for the account signed in, as before: the
# hook reads it for when a limit resets.
#
# A usage limit the hook noted as hit (.limit) is over once its window has moved on: a limit
# reset early starts a new window, which ends later than the one that was used up.

dir="$HOME/.claude/ctally.d"

# Claude Code stops a status line still running when the next is due. Stopped between writing
# a file and renaming it into place, this leaves nothing half-made behind.
trap 'rm -f "$dir"/.statusline.$$ "$dir"/.usage/.*.$$ "$dir"/.status/.*.$$ 2>/dev/null; exit 0' HUP INT TERM

input=$(cat)

# The accounts kept so far.
kept=""
for file in "$dir/.usage"/*; do
  [ -f "$file" ] && kept="$kept ${file##*/}"
done

# What to do with this one, worked out in one go: "<session id, or - with none to name>
# <whether to keep its note> <whether to keep the limits> <the account they're for> <whether
# that's the one signed in> <when the session window ends> <when the week does>".
verdict=$(printf '%s\n' "$input" | CTALLY_DIR="$dir" CTALLY_KEPT="$kept" CTALLY_CONFIG="$HOME/.claude.json" \
  CTALLY_NOW="$(date +%s)" awk '
  function slurp(file,    line, text) {
    text = ""
    while ((getline line < file) > 0) text = text line " "
    close(file)
    return text
  }
  # In some JSON: the object a name is given, as far as its first closing brace; a number, digit
  # for digit and as a number; a string.
  function object(text, name) {
    return match(text, "\"" name "\"[ \t]*:[ \t]*[{][^}]*") ? substr(text, RSTART, RLENGTH) : ""
  }
  function digits(text, name,    found) {
    if (!match(text, "\"" name "\"[ \t]*:[ \t]*[0-9.]+")) return ""
    found = substr(text, RSTART, RLENGTH)
    sub(/^[^:]*:[ \t]*/, "", found)
    return found
  }
  function number(text, name) { return digits(text, name) + 0 }
  function string(text, name,    found) {
    if (!match(text, "\"" name "\"[ \t]*:[ \t]*\"[^\"]*\"")) return ""
    found = substr(text, RSTART, RLENGTH)
    sub(/^[^:]*:[ \t]*"/, "", found)
    sub(/"$/, "", found)
    return found
  }
  # The limits in a status line: when the session window ends, how much of it is used, and
  # the same for the week. A window that is over says nothing any more.
  function reading(text, r,    part) {
    part = object(text, "five_hour"); r[1] = number(part, "resets_at"); r[2] = number(part, "used_percentage")
    part = object(text, "seven_day"); r[3] = number(part, "resets_at"); r[4] = number(part, "used_percentage")
    if (r[1] <= now) r[1] = r[2] = 0
    if (r[3] <= now) r[3] = r[4] = 0
  }
  # Reset times a minute apart are the same window.
  function near(a, b) { return a - b <= 60 && b - a <= 60 }
  # The account signed in: "accountUuid" in "oauthAccount", in a file of many lines or of one.
  function signed_in(file,    line, at, end, id) {
    id = ""
    while ((getline line < file) > 0) {
      if (!at) {
        if (!(at = index(line, "\"oauthAccount\""))) continue
        line = substr(line, at)
        if (line !~ /^"oauthAccount"[ \t]*:[ \t]*[{]/ && line !~ /^"oauthAccount"[ \t]*:[ \t]*$/) break
      }
      end = index(line, "}")
      if (match(line, /"accountUuid"[ \t]*:[ \t]*"[^"]*"/) && (!end || RSTART < end)) {
        id = string(line, "accountUuid")
        break
      }
      if (end) break
    }
    close(file)
    return id ~ /^[A-Za-z0-9_-]+$/ ? id : "-"
  }
  # What the session has had from the API so far, as Claude Code counts it.
  function spent(text) {
    return digits(text, "total_api_duration_ms") "/" digits(text, "total_input_tokens") "/" digits(text, "total_output_tokens")
  }
  # Whether the session has had a reply since it last drew its status line, going by its note
  # of that. With nothing noted yet there is no telling. A Claude Code that keeps no such
  # count: whether the limits themselves have moved, their windows running out aside, which
  # it then leaves out.
  function replied(    text) {
    if (session == "-" || (text = slurp(dir "/.status/" session)) == "") return 0
    if (spent(input) != "//") return spent(input) != spent(text)
    reading(text, P)
    return (R[1] && (R[1] != P[1] || R[2] != P[2])) || (R[3] && (R[3] != P[3] || R[4] != P[4]))
  }
  BEGIN {
    now = ENVIRON["CTALLY_NOW"] + 0
    dir = ENVIRON["CTALLY_DIR"]
    input = ""
    while ((getline line) > 0) input = input line " "
    session = string(input, "session_id")
    if (session !~ "^[^./ \t][^/ \t]*$") session = "-"
    note = session != "-" && (index(input, "\"context_window\"") || index(input, "\"rate_limits\""))
    reading(input, R)
    current = signed_in(ENVIRON["CTALLY_CONFIG"])
    account = current
    keep = (R[1] || R[3]) && replied()
    if (keep) {
      count = split(ENVIRON["CTALLY_KEPT"], names, " ")
      matches = 0
      for (i = 1; i <= count; i++) {
        if (current != "-" && names[i] == "-") continue     # kept before anyone was signed in
        reading(slurp(dir "/.usage/" names[i]), S)
        if ((R[3] && S[3] && near(R[3], S[3])) || (!R[3] && !S[3] && S[1] && near(R[1], S[1]))) {
          matches++
          account = names[i]
          for (k = 1; k <= 4; k++) M[k] = S[k]
        }
      }
      # One week kept for one account, and no two session windows going at once in it, which
      # an account never has: that account. Otherwise the one signed in.
      if (matches != 1 || (R[1] && M[1] && !near(R[1], M[1]))) account = current
    }
    printf "%s %d %d %s %d %d %d\n", session, note, keep, account, (account == current), R[1], R[3]
  }' 2>/dev/null)
read -r session note keep account current hour week <<EOF
$verdict
EOF

# The JSON, written beside where it goes and renamed into place, so it's never read half-written:
# save <folder> <name>
save() {
  [ -d "$1" ] || mkdir -p "$1" 2>/dev/null
  printf '%s\n' "$input" 2>/dev/null > "$1/.$2.$$" && mv -f "$1/.$2.$$" "$1/$2" 2>/dev/null \
    || rm -f "$1/.$2.$$" 2>/dev/null
}

if [ "$keep" = 1 ]; then
  save "$dir/.usage" "$account"
  if [ "$current" = 1 ]; then
    tmp="$dir/.statusline.$$"
    printf '%s\n' "$input" 2>/dev/null > "$tmp" && mv -f "$tmp" "$dir/.statusline" 2>/dev/null \
      || rm -f "$tmp" 2>/dev/null
    if [ -f "$dir/.limit" ]; then
      # "<when it resets> <what Claude Code said>", then "account <whose it was>" if known.
      hit="" message="" whose=""
      { read -r hit message; read -r _ whose; } < "$dir/.limit" 2>/dev/null
      case "$message" in
        *"session limit"*) resets=$hour ;;
        *"weekly limit"*) resets=$week ;;
        *) resets=0 ;;
      esac
      if [ -z "$whose" ] || [ "$whose" = "$account" ]; then
        [ "${hit:-0}" -gt 0 ] 2>/dev/null && [ "${resets:-0}" -gt $((hit + 60)) ] 2>/dev/null \
          && rm -f "$dir/.limit"
      fi
    fi
  fi
fi

# Last, so that a run cut short before the limits were kept finds its reply news again next time.
[ "$note" = 1 ] && save "$dir/.status" "$session"

[ -n "$1" ] || exit 0
trap - HUP INT TERM
printf '%s\n' "$input" | sh -c "$1"
