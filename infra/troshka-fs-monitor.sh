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
    for f in "$proj_dir"*.qcow2 "$proj_dir"*.raw "$proj_dir"*.iso "$proj_dir".nfs*; do
      [ -f "$f" ] || continue
      links=$(stat -c '%h' "$f" 2>/dev/null)
      sz=$(ls -lh "$f" | awk '{print $5}')
      mod=$(stat -c '%y' "$f" 2>/dev/null | cut -d. -f1)
      tag=""
      if [ "$links" -gt 1 ] 2>/dev/null; then tag=" (hardlink)"; fi
      printf "  %-40s %6s  %s%s\n" "$(basename "$f")" "$sz" "$mod" "$tag"
    done
    for mnt in "$proj_dir"mnt-*/; do
      [ -d "$mnt" ] || continue
      if mountpoint -q "$mnt" 2>/dev/null; then
        mnt_sz=$(df -h "$mnt" | tail -1 | awk '{print $3 "/" $2 " (" $5 ")"}')
        printf "  %-40s %s  (loop-mounted)\n" "$(basename "$mnt")/" "$mnt_sz"
      else
        printf "  %-40s        (unmounted)\n" "$(basename "$mnt")/"
      fi
    done
    echo
  done
  # Containers
  ctr_list=$(podman ps -a --filter "name=troshka-" --format "{{.Names}} {{.State}} {{.Image}} {{.Size}}" 2>/dev/null)
  if [ -n "$ctr_list" ]; then
    ctr_count=$(echo "$ctr_list" | wc -l)
    ctr_running=$(echo "$ctr_list" | grep -c " running " 2>/dev/null || true)
    echo "── Containers ($ctr_running/$ctr_count running) ──"
    echo "$ctr_list" | while read name state image size; do
      icon="○"
      [ "$state" = "running" ] && icon="●"
      printf "  %s %-32s %-8s %s\n" "$icon" "$name" "$state" "$image"
    done
    echo
  fi
  # Podman storage
  podman_root=$(podman info --format '{{.Store.GraphRoot}}' 2>/dev/null)
  if [ -n "$podman_root" ] && [ -d "$podman_root" ]; then
    podman_sz=$(du -sh "$podman_root" 2>/dev/null | cut -f1)
    podman_imgs=$(podman images --filter "reference=*" --format "{{.Repository}}:{{.Tag}}" 2>/dev/null | wc -l)
    echo "── Podman Storage $podman_root ($podman_sz, $podman_imgs images) ──"
    podman images --format "  {{.Repository}}:{{.Tag}}  {{.Size}}" 2>/dev/null
    echo
  fi
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
  tmp_files=$(find $BASE/local/tmp/ $BASE/tmp/ -name "*.qcow2" -o -name "*.raw" -o -name "*.iso" -o -name "*.tar.gz" 2>/dev/null)
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
