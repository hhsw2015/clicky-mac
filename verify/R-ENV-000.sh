#!/usr/bin/env bash
# R-ENV-000 — environment prerequisites.
# Checks: macOS 14.2+, Xcode CLI tools, curl/jq/python3, Config.plist present,
# reference scripts locally installed.
R_ID="R-ENV-000"
source "$(dirname "$0")/lib/common.sh"

# macOS 14.2+
sw_vers -productVersion | awk -F. '{if($1<14||($1==14&&$2<2))exit 1}' \
    || fail "macOS 14.2+ required"

# Xcode CLI
xcode-select -p >/dev/null 2>&1 || fail "xcode-select -p failed; install CLI tools"

# curl/jq/python3
for bin in curl python3 /usr/libexec/PlistBuddy; do
    command -v "$bin" >/dev/null 2>&1 || [ -x "$bin" ] \
        || fail "missing tool: $bin"
done

# Config.plist (private)
require_file "$REPO_ROOT/leanring-buddy/Config.plist"

# Reference scripts (private)
for f in 01_realtime_voice.py 04_tool_bridge.py higher_model_ask.py; do
    require_file "$REPO_ROOT/references/$f"
done

pass
