#!/bin/bash
# The orchestrator's builder watcher: one line per builder state change, for a Monitor to read.
#   scripts/agents/watch_builders.sh
# Builders are the tmux panes whose cwd is under HAT_WORKTREES (default /data/workspace/wt/hat).
# Every WATCH_EVERY seconds (default 30) each pane is read and a line printed when it
#   - goes from busy to idle or asking (a question or permission prompt), with its last message;
#   - shows a new READY TO MERGE line;
#   - is gone.
# A pane whose own background job still runs (footer "N shell") counts as busy, not idle.
# Reads panes only (tmux capture-pane); never sends keys.
root=${HAT_WORKTREES:-/data/workspace/wt/hat}
every=${WATCH_EVERY:-30}
declare -A state ready
while true; do
  seen=""
  while read -r pane path; do
    case "$path" in "$root"/*) ;; *) continue ;; esac
    name=${path##*/}; seen="$seen $pane"
    txt=$(tmux capture-pane -p -S -40 -t "$pane" 2>/dev/null | grep -av '^\s*$' | grep -av '───')
    tail=$(printf '%s\n' "$txt" | grep -av '^ *[◯●├└] ' | tail -4)
    # Idle while its own background job runs (footer "N shell") is still working: not an event.
    if printf '%s' "$tail" | grep -aqE 'esc to interrupt|to manage|[0-9]+ shells?( still running)?'; then s=busy
    elif printf '%s' "$txt" | tail -12 | grep -aqE 'Do you want|❯ 1\.|\(y/n\)'; then s=asking
    else s=idle; fi
    r=$(printf '%s\n' "$txt" | grep -aoE 'READY TO MERGE #?[0-9]* ?[0-9a-f]{7,40}' | tail -1)
    if [ -n "$r" ] && [ "$r" != "${ready[$pane]}" ]; then
      ready[$pane]=$r; echo "$name ($pane): $r"
    fi
    if [ "$s" != "${state[$pane]}" ]; then
      prev=${state[$pane]}; state[$pane]=$s
      if [ -n "$prev" ] && [ "$s" != busy ]; then
        last=$(printf '%s\n' "$txt" | grep -a '●' | tail -1 | tr -s ' ' | head -c 220)
        echo "$name ($pane): $prev -> $s | $last"
      fi
    fi
  done < <(tmux list-panes -a -F '#{pane_id} #{pane_current_path}' 2>/dev/null)
  for p in "${!state[@]}"; do
    case "$seen " in *" $p "*) ;; *) echo "pane $p gone"; unset "state[$p]" "ready[$p]" ;; esac
  done
  sleep "$every"
done
