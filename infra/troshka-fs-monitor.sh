#!/bin/bash
# Troshka storage viewer — run on host with: sudo troshka-fs-monitor
# Refreshes every 2 seconds, grouped by project
tput civis 2>/dev/null
trap 'tput cnorm 2>/dev/null; exit' INT TERM
BASE=/var/lib/troshka
while true; do
  output=$(
  echo "=== Troshka Storage — $(date) ==="
  echo
  printf "%-28s %6s %6s %5s  %s\n" Mount Used Free Use% Source
  df -h / $BASE $BASE/* 2>/dev/null | tail -n+2 | awk '!seen[$6]++' | while read fs sz used avail pct mnt; do
    src=""
    case "$fs" in *:*) src="$fs" ;; esac
    printf "%-28s %6s %6s %5s  %s\n" "$mnt" "$used" "$avail" "$pct" "$src"
  done
  echo
  echo "$BASE"
  for proj_dir in $BASE/vms/*/ $BASE/shared/vms/*/ $BASE/local/vms/*/; do
    [ -d "$proj_dir" ] || continue
    total=$(du -sh "$proj_dir" 2>/dev/null | cut -f1)
    short_dir=${proj_dir#$BASE/}
    echo "── Project $short_dir ($total) ──"
    for f in "$proj_dir"*.qcow2 "$proj_dir"*.iso "$proj_dir".nfs*; do
      [ -f "$f" ] || continue
      links=$(stat -c '%h' "$f" 2>/dev/null)
      sz=$(ls -lh "$f" | awk '{print $5}')
      mod=$(stat -c '%y' "$f" 2>/dev/null | cut -d. -f1)
      tag=""
      if [ "$links" -gt 1 ] 2>/dev/null; then tag=" (hardlink)"; fi
      printf "  %-40s %6s  %s%s\n" "$(basename "$f")" "$sz" "$mod" "$tag"
    done
    echo
  done
  IMG_DIR=""
  for d in $BASE/images $BASE/shared/images; do
    [ -d "$d" ] && [ "$(ls -A "$d" 2>/dev/null)" ] && IMG_DIR="$d" && break
  done
  if [ -n "$IMG_DIR" ]; then
    total=$(du -sh "$IMG_DIR" 2>/dev/null | cut -f1)
    short_dir=${IMG_DIR#$BASE/}
    echo "── Image Cache $short_dir/ ($total) ──"
    for f in "$IMG_DIR"/*; do
      [ -f "$f" ] || continue
      printf "  %-40s %6s\n" "$(basename "$f")" "$(ls -lh "$f" | awk '{print $5}')"
    done
    echo
  fi
  if [ -d $BASE/local/cache/patterns ]; then
    total=$(du -sh $BASE/local/cache/patterns 2>/dev/null | cut -f1)
    echo "── Pattern Cache local/cache/patterns/ ($total) ──"
    for d in $BASE/local/cache/patterns/*/; do
      [ -d "$d" ] || continue
      ptotal=$(du -sh "$d" 2>/dev/null | cut -f1)
      short_d=${d#$BASE/}
      echo "  $short_d ($ptotal)"
      for f in "$d"*; do
        [ -f "$f" ] || continue
        printf "    %-36s %6s\n" "$(basename "$f")" "$(ls -lh "$f" | awk '{print $5}')"
      done
    done
    echo
  fi
  # Active flatten/upload temp files
  tmp_files=$(find $BASE/local/tmp/ $BASE/tmp/ -name "*.qcow2" -o -name "*.iso" 2>/dev/null)
  if [ -n "$tmp_files" ]; then
    echo "── Active Temp Files {local/,}tmp/ ──"
    echo "$tmp_files" | while read f; do
      sz=$(ls -lh "$f" | awk '{print $5}')
      mod=$(stat -c '%y' "$f" 2>/dev/null | cut -d. -f1)
      short=$(echo "$f" | sed "s|$BASE/||")
      printf "  %-40s %6s  %s\n" "$short" "$sz" "$mod"
    done
    echo
  fi
  # Active S3 transfers
  s3_pids=$(pgrep -f "aws s3 cp" 2>/dev/null)
  if [ -n "$s3_pids" ]; then
    echo "── S3 Transfers ──"
    for pid in $s3_pids; do
      cmdline=$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)
      src=$(echo "$cmdline" | grep -oP 's3://\S+|/\S+\.qcow2|/\S+\.iso' | head -1)
      dst=$(echo "$cmdline" | grep -oP 's3://\S+|/\S+\.qcow2|/\S+\.iso' | tail -1)
      wb=$(awk '/^write_bytes:/{print $2}' /proc/$pid/io 2>/dev/null)
      rb=$(awk '/^read_bytes:/{print $2}' /proc/$pid/io 2>/dev/null)
      bytes=${wb:-0}
      [ "$bytes" -eq 0 ] 2>/dev/null && bytes=${rb:-0}
      if [ "$bytes" -gt 1073741824 ] 2>/dev/null; then
        gb=$((bytes / 1073741824))
        mb=$(( (bytes % 1073741824) * 10 / 1073741824 ))
        sz="${gb}.${mb} GB"
      elif [ "$bytes" -gt 1048576 ] 2>/dev/null; then
        sz="$((bytes / 1048576)) MB"
      else
        sz="0 MB"
      fi
      src_short=$(basename "$src" 2>/dev/null)
      if echo "$src" | grep -q '^s3://'; then
        dir="↓ download"
      else
        dir="↑ upload"
      fi
      printf "  %-30s %8s  %s\n" "$src_short" "$sz" "$dir"
    done
    echo
  fi
  )
  clear
  echo "$output"
  sleep 2
done
