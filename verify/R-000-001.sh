#!/usr/bin/env bash
# R-000-001 — repository layout: farzaa fork + AgentBackend + gitignored Config.plist.
R_ID="R-000-001"
source "$(dirname "$0")/lib/common.sh"

require_dir "$REPO_ROOT/leanring-buddy"
# AgentBackend group need not exist yet (created later; the SPEC allows
# absent until R-100-001). Only check what MUST be present now.
require_dir "$REPO_ROOT/leanring-buddy/Assets.xcassets"
require_dir "$REPO_ROOT/leanring-buddy.xcodeproj"

# Config.plist must NOT be tracked
cd "$REPO_ROOT"
if git ls-files leanring-buddy/Config.plist 2>/dev/null | grep -q .; then
    fail "Config.plist is tracked; it MUST be gitignored"
fi

# Config.plist must be ignored (if it exists on disk)
if [ -f leanring-buddy/Config.plist ]; then
    git check-ignore -q leanring-buddy/Config.plist \
        || fail "Config.plist present but not ignored"
fi

pass
