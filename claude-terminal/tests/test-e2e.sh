#!/bin/bash
# End-to-end test for Claude Terminal v2.3.0 features
# Run from the repo root or claude-terminal directory

set -e

PASS=0
FAIL=0
SKIP=0

pass() { echo "  ✅ PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  ⏭️  SKIP: $1"; SKIP=$((SKIP + 1)); }

# Find the right base directory
BASE_DIR="."
[ -d "claude-terminal" ] && BASE_DIR="claude-terminal"

echo "======================================"
echo "Claude Terminal v2.3.0 E2E Tests"
echo "======================================"
echo ""

# Test 1: Bypass permissions flag in run.sh
echo "Test 1: Bypass permissions in run.sh"
if grep -q 'dangerously-skip-permissions' "$BASE_DIR/run.sh" 2>/dev/null; then
    COUNT=$(grep -c 'dangerously-skip-permissions' "$BASE_DIR/run.sh")
    pass "run.sh contains --dangerously-skip-permissions ($COUNT occurrences)"
else
    fail "run.sh missing --dangerously-skip-permissions"
fi

# Test 2: Bypass permissions in session picker
echo "Test 2: Bypass permissions in session picker"
PICKER="$BASE_DIR/scripts/claude-session-picker.sh"
if [ -f "$PICKER" ]; then
    COUNT=$(grep -c 'dangerously-skip-permissions' "$PICKER" || echo 0)
    if [ "$COUNT" -ge 4 ]; then
        pass "Session picker has $COUNT instances of --dangerously-skip-permissions"
    else
        fail "Session picker only has $COUNT instances (expected >= 4)"
    fi
else
    skip "Session picker not found"
fi

# Test 3: Agent teams env var
echo "Test 3: Agent teams environment variable"
if grep -q 'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS' "$BASE_DIR/run.sh" 2>/dev/null; then
    pass "Agent teams env var is set in run.sh"
else
    fail "Agent teams env var not found in run.sh"
fi

# Test 4: API server syntax check
echo "Test 4: API server syntax"
API_FILE="$BASE_DIR/scripts/api-server.js"
if [ -f "$API_FILE" ]; then
    if node --check "$API_FILE" 2>/dev/null; then
        pass "api-server.js has valid syntax"
    else
        fail "api-server.js has syntax errors"
    fi
else
    skip "api-server.js not found"
fi

# Test 5: API server unit tests
echo "Test 5: API server unit tests"
TEST_FILE="$BASE_DIR/tests/test-api-server.js"
if [ -f "$TEST_FILE" ]; then
    if (cd "$BASE_DIR" && node --test tests/test-api-server.js) 2>/dev/null; then
        pass "API server tests pass"
    else
        fail "API server tests failed"
    fi
else
    skip "API server test file not found"
fi

# Test 6: Custom integration files exist
echo "Test 6: Custom integration files"
COMP_DIR="$BASE_DIR/custom_components/claude_terminal"
if [ -d "$COMP_DIR" ]; then
    EXPECTED_FILES="__init__.py config_flow.py conversation.py api.py const.py manifest.json strings.json"
    ALL_PRESENT=true
    for f in $EXPECTED_FILES; do
        if [ ! -f "$COMP_DIR/$f" ]; then
            fail "Missing: $COMP_DIR/$f"
            ALL_PRESENT=false
        fi
    done
    if [ "$ALL_PRESENT" = true ]; then
        pass "All custom integration files present"
    fi
else
    skip "Custom components directory not found"
fi

# Test 7: Python syntax check
echo "Test 7: Python syntax check"
if [ -d "$COMP_DIR" ]; then
    SYNTAX_OK=true
    for f in "$COMP_DIR"/*.py; do
        if ! python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
            fail "Syntax error in $f"
            SYNTAX_OK=false
        fi
    done
    if [ "$SYNTAX_OK" = true ]; then
        pass "All Python files have valid syntax"
    fi
else
    skip "Custom components directory not found"
fi

# Test 8: Manifest version matches config.yaml
echo "Test 8: Version consistency"
MANIFEST="$COMP_DIR/manifest.json"
CONFIG_FILE="$BASE_DIR/config.yaml"
if [ -f "$MANIFEST" ] && [ -f "$CONFIG_FILE" ]; then
    MANIFEST_VERSION=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['version'])" 2>/dev/null)
    CONFIG_VERSION=$(python3 -c "import re; line=open('$CONFIG_FILE').read(); m=re.search(r'^version:\s*[\"\\x27]?([^\"\\x27\\s]+)', line, re.M); print(m.group(1) if m else '')" 2>/dev/null)
    if [ "$MANIFEST_VERSION" = "$CONFIG_VERSION" ]; then
        pass "Versions match: manifest=$MANIFEST_VERSION, config=$CONFIG_VERSION"
    else
        fail "Version mismatch: manifest=$MANIFEST_VERSION, config=$CONFIG_VERSION"
    fi
else
    skip "Manifest or config.yaml not found"
fi

# Test 9: Dockerfile includes custom_components
echo "Test 9: Dockerfile includes custom_components"
if grep -q 'COPY custom_components/' "$BASE_DIR/Dockerfile" 2>/dev/null; then
    pass "Dockerfile copies custom_components"
else
    fail "Dockerfile missing COPY custom_components/"
fi

# Test 10: run.sh has install_custom_integration and start_api_server
echo "Test 10: Startup flow includes new functions"
if grep -q 'install_custom_integration' "$BASE_DIR/run.sh" && grep -q 'start_api_server' "$BASE_DIR/run.sh"; then
    pass "run.sh includes install_custom_integration and start_api_server"
else
    fail "run.sh missing new startup functions"
fi

# Test 11: claude -p mode works (if claude is available)
echo "Test 11: claude -p mode"
if command -v claude &>/dev/null; then
    RESULT=$(claude -p "Say just the word test" --dangerously-skip-permissions --output-format json 2>/dev/null)
    if echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'result' in d" 2>/dev/null; then
        pass "claude -p returns valid JSON with result field"
    else
        fail "claude -p output is not valid JSON or missing result field"
    fi
else
    skip "claude CLI not available"
fi

# Summary
echo ""
echo "======================================"
echo "Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "======================================"

[ "$FAIL" -gt 0 ] && exit 1
exit 0
