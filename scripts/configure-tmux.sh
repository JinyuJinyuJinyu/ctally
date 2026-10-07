#!/bin/bash
# Add CTally to tmux, or take it out again.
#
#   configure-tmux.sh install   <path to the ctally command>
#   configure-tmux.sh uninstall
#
# Installing writes a marked block to your tmux config (~/.tmux.conf, or
# ~/.config/tmux/tmux.conf if that's the one you use), backed up first:
#   prefix + J  jumps to the Claude Code session that needs you
#   the status line shows CTally's counts, e.g. "!1 ▶2 ✓5"
# A running tmux server picks the change up straight away. Uninstalling removes the block
# and undoes both in the running server.
set -euo pipefail

action="${1:-}"
if [ -f "$HOME/.tmux.conf" ] || [ ! -f "$HOME/.config/tmux/tmux.conf" ]; then
  conf="$HOME/.tmux.conf"
else
  conf="$HOME/.config/tmux/tmux.conf"
fi
begin="# >>> ctally >>>"
end="# <<< ctally <<<"

# The config without CTally's block.
without_block() {
  [ -f "$conf" ] || return 0
  awk -v begin="$begin" -v end="$end" '
    index($0, begin) == 1 { skip = 1 }
    !skip { print }
    index($0, end) == 1 { skip = 0 }' "$conf"
}

backup() {
  [ -f "$conf" ] || return 0
  local stamp backup n=1
  stamp=$(date +%Y%m%d-%H%M%S)
  backup="$conf.bak.$stamp"
  while [ -e "$backup" ]; do n=$((n + 1)); backup="$conf.bak.$stamp-$n"; done
  cp -p "$conf" "$backup"
  echo "Backed up $conf to $backup"
}

server_running() {
  command -v tmux >/dev/null && tmux list-sessions >/dev/null 2>&1
}

case "$action" in
  install)
    ctally="${2:?path to the ctally command}"
    block="$begin  (added by CTally's install.sh --tmux; ./uninstall.sh removes it)
# prefix + J: jump to the Claude Code session that needs you
bind-key J run-shell -b \"'$ctally' jump '#{pane_id}' '#{client_name}' '#{socket_path}'\"
# CTally's counts at the end of the status line, added only once
if-shell -F \"#{m:*ctally status*,#{status-right}}\" \"\" \"set -ag status-right ' #('$ctally' status --tmux)' ; set -g status-right-length 80\"
$end"
    updated=$(without_block; printf '%s\n' "$block")
    if [ -f "$conf" ] && [ "$updated" = "$(cat "$conf")" ]; then
      echo "tmux already set up in $conf"
    else
      backup
      mkdir -p "$(dirname "$conf")"
      printf '%s\n' "$updated" > "$conf"
      echo "tmux set up in $conf: prefix + J jumps, the status line shows the counts"
    fi
    if server_running; then
      tmux source-file "$conf"
    fi
    ;;
  uninstall)
    if [ -f "$conf" ] && grep -qF "$begin" "$conf"; then
      backup
      updated=$(without_block)
      if [ -z "$(printf '%s' "$updated" | tr -d '[:space:]')" ]; then
        rm -f "$conf"   # it only ever held CTally's block
      else
        printf '%s\n' "$updated" > "$conf"
      fi
      echo "Removed CTally from $conf"
    fi
    if server_running; then
      if tmux list-keys -T prefix | awk '$4 == "J"' | grep -q "ctally"; then tmux unbind-key J; fi
      right=$(tmux show -gv status-right)
      cleaned=$(printf '%s' "$right" | sed 's/ #([^)]*ctally[^)]* status --tmux)//')
      if [ "$cleaned" != "$right" ]; then tmux set -g status-right "$cleaned"; fi
    fi
    ;;
  *)
    sed -n '2,/^set /s/^# \{0,1\}//p' "$0"
    exit 2
    ;;
esac
