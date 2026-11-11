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
# Whose a reading is, its week says: every account's week ends at its own time. A reading for
# a week already kept belongs with it, whoever is signed in by now: a reply that was on its
# way as I changed accounts, or an idle session redrawing its status line (when its prompt
# cache goes cold, say) with numbers hours old. It replaces what's kept only if it's newer:
# more used in the same session window, or a session window where none was still going. One
# for a week not seen before belongs to the account signed in, if it's news: not what its
# session said last time.
#
# ~/.claude/ctally.d/.statusline holds the latest for the account signed in, as before: the
# hook reads it for when a limit resets.
#
# A usage limit the hook noted as hit (.limit) is over once its window has moved on: a limit
# reset early starts a new window, which ends later than the one that was used up.

input=$(cat)
dir="$HOME/.claude/ctally.d"

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
  # In some JSON: the object a name is given, as far as its first closing brace; a number; a string.
  function object(text, name) {
    return match(text, "\"" name "\"[ \t]*:[ \t]*[{][^}]*") ? substr(text, RSTART, RLENGTH) : ""
  }
  function number(text, name,    found) {
    if (!match(text, "\"" name "\"[ \t]*:[ \t]*[0-9.]+")) return 0
    found = substr(text, RSTART, RLENGTH)
    sub(/^[^:]*:[ \t]*/, "", found)
    return found + 0
  }
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
  # Whether the reading is newer than the one kept for its week (M): more used in the session
  # window still going, or a session window where the one kept has ended.
  function newer() {
    if (M[1]) return R[1] && (R[2] > M[2] || (R[2] == M[2] && R[4] > M[4]))
    return R[1] > 0 || R[4] > M[4]
  }
  # Whether it differs from what the session said last. Its windows running out, which Claude
  # Code then leaves out, is no news; nor is a session with nothing said before to go by.
  function news(    text) {
    if (session == "-" || (text = slurp(dir "/.status/" session)) == "") return 0
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
    keep = 0
    if (R[1] || R[3]) {
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
      # an account never has: that account. Otherwise the one signed in, on news alone.
      if (matches == 1 && !(R[1] && M[1] && !near(R[1], M[1]))) {
        keep = newer()
      } else {
        account = current
        keep = news()
      }
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

# Last, so that a run cut short before the limits were kept finds them news again next time.
[ "$note" = 1 ] && save "$dir/.status" "$session"

[ -n "$1" ] || exit 0
printf '%s\n' "$input" | sh -c "$1"
