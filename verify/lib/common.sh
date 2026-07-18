# verify/lib/common.sh — shared helpers for every verify/R-*.sh
# Sourced, not executed.

set -o pipefail

# Repo root (verify/lib is 2 levels down)
_verify_lib="${BASH_SOURCE[0]:-$0}"
REPO_ROOT="$(cd "$(dirname "$_verify_lib")/../.." && pwd)"

# Exit codes
readonly EX_OK=0
readonly EX_FAIL=1
readonly EX_SKIP=77

# ID is set by the caller before sourcing this file, or via first arg.
: "${R_ID:=UNKNOWN}"

# Human-readable trace only when -v flag or VERIFY_VERBOSE=1.
_trace() {
    if [ "${VERIFY_VERBOSE:-0}" = "1" ]; then
        printf '  → %s\n' "$*" >&2
    fi
}

# All verify scripts should output exactly one JSON line to stdout at the end.
_now_ms() { python3 -c 'import time; print(int(time.time()*1000))' 2>/dev/null || echo 0; }
_start_time_ms=$(_now_ms)

_emit() {
    local status="$1"
    local reason="${2:-}"
    local end_ms
    end_ms=$(_now_ms)
    local dur=$(( end_ms - _start_time_ms ))
    local esc
    esc=$(printf '%s' "$reason" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo "\"\"")
    printf '{"id":"%s","status":"%s","duration_ms":%d,"reason":%s}\n' \
        "$R_ID" "$status" "$dur" "$esc"
}

pass() { _emit pass; exit $EX_OK; }
fail() { _emit fail "${1:-unspecified}"; exit $EX_FAIL; }
skip() { _emit skip "${1:-conditional skip}"; exit $EX_SKIP; }

# Assertions
require_file() {
    [ -f "$1" ] || fail "missing file: $1"
    _trace "file present: $1"
}
require_no_file() {
    [ ! -e "$1" ] || fail "unexpected file present: $1"
    _trace "file absent: $1"
}
require_dir() {
    [ -d "$1" ] || fail "missing dir: $1"
}
require_grep_empty() {
    # $1 = regex, $2... = paths
    local pattern="$1"; shift
    local hits
    hits=$(grep -REn "$pattern" "$@" 2>/dev/null | head -20 || true)
    if [ -n "$hits" ]; then
        fail "forbidden pattern '$pattern' found:\n$hits"
    fi
    _trace "clean: '$pattern' not found"
}
require_grep_match() {
    local pattern="$1"; shift
    grep -REn "$pattern" "$@" >/dev/null 2>&1 \
        || fail "expected pattern '$pattern' not found"
    _trace "matched: '$pattern'"
}
require_git_tracked() {
    (cd "$REPO_ROOT" && git ls-files --error-unmatch "$1" >/dev/null 2>&1) \
        || fail "not tracked: $1"
}
require_git_ignored() {
    (cd "$REPO_ROOT" && git check-ignore -q "$1" 2>/dev/null) \
        || fail "not ignored: $1"
}

# Proxy helpers — skip if config missing
proxy_url() {
    local plist="$REPO_ROOT/leanring-buddy/Config.plist"
    [ -f "$plist" ] || return 1
    /usr/libexec/PlistBuddy -c "Print :ProxyBaseURL" "$plist" 2>/dev/null
}
proxy_reachable() {
    local url
    url=$(proxy_url) || return 1
    [ -n "$url" ] || return 1
    curl -sk --max-time 5 --connect-timeout 3 -o /dev/null -w "%{http_code}" "$url" 2>/dev/null \
        | grep -qE '^(200|301|302|401|403|404)$'
}
require_proxy() {
    proxy_reachable || skip "proxy unreachable or Config.plist missing"
}
