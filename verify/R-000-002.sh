#!/usr/bin/env bash
# R-000-002 — no forbidden strings in tracked files.
# Exclusions: SPEC.md and verify/ are allowed to name the forbidden
# strings inside grep-check contexts.
R_ID="R-000-002"
source "$(dirname "$0")/lib/common.sh"

cd "$REPO_ROOT"

# Forbidden patterns (regex, ORed)
FORBIDDEN='clicker-proxy|farza-0cb|mrpvynsdsn|com\.humansongs|fable-5|CCLINE_HEYCLICKY|eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\.eyJpc3MiOiJzdXBhYmFzZQ'

# Also flag any real ek_ ephemeral prefix that would only appear in a leaked capture
FORBIDDEN="$FORBIDDEN|ek_[a-f0-9]{20}"

# Scan tracked files EXCEPT SPEC.md and verify/
hits=$(git grep -InE "$FORBIDDEN" -- ':!SPEC.md' ':!verify/' 2>/dev/null || true)

if [ -n "$hits" ]; then
    fail "forbidden strings in tracked files:\n$hits"
fi

pass
