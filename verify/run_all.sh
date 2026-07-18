#!/usr/bin/env bash
# verify/run_all.sh — walks every R-*.sh in ID order.
# Exit 0 if all pass or skip; exit 1 if any fail.

set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

pass=0; fail=0; skip=0; fails=()
for script in "$HERE"/R-*.sh; do
    [ -f "$script" ] || continue
    id=$(basename "$script" .sh)
    line=$(bash "$script" 2>/dev/null | tail -n1)
    status=$(printf '%s' "$line" | python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print(d.get("status","?"))' 2>/dev/null || echo "?")
    case "$status" in
        pass) pass=$((pass+1)); printf '\033[32m✓\033[0m %s\n' "$id" ;;
        skip) skip=$((skip+1)); printf '\033[33m~\033[0m %s (skipped)\n' "$id" ;;
        fail) fail=$((fail+1)); fails+=("$id"); printf '\033[31m✗\033[0m %s\n' "$id"
              reason=$(printf '%s' "$line" | python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print(d.get("reason",""))' 2>/dev/null || echo "")
              [ -n "$reason" ] && printf '    %s\n' "$reason" ;;
        *)    fail=$((fail+1)); fails+=("$id"); printf '\033[31m?\033[0m %s (bad output)\n' "$id" ;;
    esac
done

printf '\n\033[1msummary\033[0m: %d pass, %d skip, %d fail\n' "$pass" "$skip" "$fail"
if [ "$fail" -gt 0 ]; then
    printf 'failed: %s\n' "${fails[*]}"
    exit 1
fi
exit 0
