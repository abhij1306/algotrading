#!/bin/bash
# Logs memory every 30 s to $HAT_WS/memlog.csv (HAT_WS defaults to /data/workspace); exits (to alert the orchestrator) when
# available memory drops below $1 MB (default 1500).
LIMIT=${1:-1500}
LOG=${HAT_WS:-/data/workspace}/memlog.csv
[ -f "$LOG" ] || echo "time,avail_mb,swap_used_mb,top_proc_rss_mb,top_proc" > "$LOG"
while true; do
  avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  swap=$(awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print int((t-f)/1024)}' /proc/meminfo)
  top=$(ps -eo rss=,comm= --sort=-rss | head -1 | awk '{printf "%d,%s", $1/1024, $2}')
  echo "$(date +%H:%M:%S),$avail,$swap,$top" >> "$LOG"
  if [ "$avail" -lt "$LIMIT" ]; then
    echo "LOW MEMORY: avail=${avail}MB swap_used=${swap}MB"
    ps -eo pid,rss,etime,cmd --sort=-rss | head -6 | cut -c1-200
    exit 0
  fi
  sleep 30
done
